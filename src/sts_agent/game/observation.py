"""State adapter: STS2MCP ``GET /api/v1/singleplayer`` JSON -> normalized observation.

Rules:

* Allowlist only. Every field in the observation is copied explicitly; the raw
  response is never passed through. Anything not listed here never reaches the
  policy or the training-ready trace.
* ``None`` (JSON ``null``) means *unknown*. Missing numbers are never replaced
  by zero.
* Draw-pile *order* is not exposed: every pile is reduced to counts per
  identical card (name, cost, text), as the in-game pile viewers show. The mod
  already sorts it by rarity; we re-aggregate so order cannot leak.
* No seed, RNG, map lookahead, or profile/account data is included.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from .cards import parse_effects

OBS_SCHEMA_VERSION = "sts2-obs-2"

COMBAT_STATE_TYPES = {"monster", "elite", "boss"}
POST_COMBAT_STATE_TYPES = {"rewards", "card_reward", "map", "treasure", "event", "rest_site", "shop",
                           "relic_select", "card_select", "bundle_select", "crystal_sphere", "fake_merchant"}

# Phases
PHASE_COMBAT_READY = "combat_ready"            # player's play phase; actions accepted
PHASE_COMBAT_BUSY = "combat_busy"              # in combat but not accepting actions (enemy turn, transitions)
PHASE_COMBAT_SELECTION = "combat_hand_select"  # in-combat card selection prompt (unsupported today)
PHASE_COMBAT_ENDED = "combat_ended"            # combat room, fight over, waiting for rewards
PHASE_POST_COMBAT = "post_combat_screen"       # rewards/map/etc. (combat is over)
PHASE_GAME_OVER = "game_over"
PHASE_MENU = "menu"
PHASE_OVERLAY = "unsupported_overlay"
PHASE_UNKNOWN = "unknown"


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v.strip())
    return None


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _statuses(raw: Any) -> list[dict] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        out.append({
            "id": _str_or_none(p.get("id")),
            "name": _str_or_none(p.get("name")),
            "amount": _int_or_none(p.get("amount")),
            "type": _str_or_none(p.get("type")),
            "description": _str_or_none(p.get("description")),
        })
    return out


_INTENT_LABEL = re.compile(r"^\s*(\d+)\s*(?:[x×*]\s*(\d+))?\s*$", re.I)


def parse_intent(raw: dict) -> dict:
    itype = _str_or_none(raw.get("type"))
    label = _str_or_none(raw.get("label"))
    is_attack = bool(itype) and "attack" in itype.lower()
    dmg = hits = None
    if is_attack and label is not None:
        m = _INTENT_LABEL.match(label)
        if m:
            dmg = int(m.group(1))
            hits = int(m.group(2)) if m.group(2) else 1
    return {
        "type": itype,
        "label": label,
        "title": _str_or_none(raw.get("title")),
        "description": _str_or_none(raw.get("description")),
        "is_attack": is_attack,
        "damage_per_hit": dmg,     # None if not an attack or label not parseable
        "hits": hits,
        "total_damage": dmg * hits if dmg is not None and hits is not None else None,
    }


def _enemy(raw: dict) -> dict:
    intents_raw = raw.get("intents")
    intents = [parse_intent(i) for i in intents_raw if isinstance(i, dict)] if isinstance(intents_raw, list) else None
    incoming: int | None = 0
    if intents is None:
        incoming = None
    else:
        for it in intents:
            if it["is_attack"]:
                if it["total_damage"] is None:
                    incoming = None
                    break
                incoming += it["total_damage"]
    return {
        "combat_id": _int_or_none(raw.get("combat_id")),
        "entity_id": _str_or_none(raw.get("entity_id")),
        "name": _str_or_none(raw.get("name")),
        "hp": _int_or_none(raw.get("hp")),
        "max_hp": _int_or_none(raw.get("max_hp")),
        "block": _int_or_none(raw.get("block")),
        "statuses": _statuses(raw.get("status")),
        # From the agent mod extension; None = extension not installed or not readable.
        "monster_id": _str_or_none(raw.get("monster_id")),
        "next_move_id": _str_or_none(raw.get("next_move_id")),
        "move_history": ([m for m in raw["move_history"] if isinstance(m, str)]
                         if isinstance(raw.get("move_history"), list) else None),
        "intents": intents,
        # Multiple intents describe ONE move, not successive rounds.
        "intent_forecast": [
            {"enemy_turn_offset": offset,
             "source": "observed_next_move" if offset == 1 and intents is not None else "not_exposed",
             "intents": intents if offset == 1 else None}
            for offset in range(1, 4)
        ],
        "has_attack_intent": bool(intents) and any(i["is_attack"] for i in intents),
        "incoming_damage": incoming,  # None = unknown (attack with unparseable label, or no intents data)
    }


def _hand_card(raw: dict, position: int, occurrence: int) -> dict:
    cost_raw = raw.get("cost")
    cost = _int_or_none(cost_raw)
    desc = _str_or_none(raw.get("description"))
    ctype = _str_or_none(raw.get("type"))
    card_id = _str_or_none(raw.get("id"))
    eff = parse_effects(desc, ctype)
    return {
        "index": _int_or_none(raw.get("index")) if _int_or_none(raw.get("index")) is not None else position,
        # The mod exposes no per-instance card id. ``instance_key`` is positional
        # (card id + occurrence among identical ids in this hand) and is only
        # valid for the observation that produced it.
        "instance_key": f"{card_id}#{occurrence}",
        "id": card_id,
        "name": _str_or_none(raw.get("name")),
        "type": ctype,
        "cost": cost,                      # None if unknown or X-cost
        "cost_is_x": isinstance(cost_raw, str) and cost_raw.strip().upper() == "X",
        "description": desc,
        "target_type": _str_or_none(raw.get("target_type")),
        "can_play": raw.get("can_play") if isinstance(raw.get("can_play"), bool) else None,
        "unplayable_reason": _str_or_none(raw.get("unplayable_reason")),
        "is_upgraded": raw.get("is_upgraded") if isinstance(raw.get("is_upgraded"), bool) else None,
        "keywords": [k.get("name") for k in raw.get("keywords", []) if isinstance(k, dict) and k.get("name")]
        if isinstance(raw.get("keywords"), list) else None,
        "effects": eff.to_dict(),
        "combat_previews": _combat_previews(raw.get("combat_previews")),
    }


def _combat_previews(raw: Any) -> list[dict] | None:
    """Allowlist the optional native-preview extension; no guessed numbers."""
    if not isinstance(raw, list):
        return None
    result = []
    for preview in raw:
        if not isinstance(preview, dict) or preview.get("source") != "game_native_preview_v1":
            continue
        variables = preview.get("variables")
        result.append({
            "target_combat_id": _int_or_none(preview.get("target_combat_id")),
            "source": "game_native_preview_v1",
            "variables": [
                {"name": _str_or_none(v.get("name")),
                 "base_value": _int_or_none(v.get("base_value")),
                 "preview_value": _int_or_none(v.get("preview_value"))}
                for v in variables if isinstance(v, dict)
            ] if isinstance(variables, list) else None,
        })
    return result


def _sort_key(entry: tuple) -> tuple:
    return tuple("" if v is None else str(v) for v in entry)


def _pile_groups(raw: Any) -> list[dict] | None:
    """Pile contents grouped by identical card (name, cost, text) with counts.

    Grouping is sorted, so pile order (hidden in game for the draw pile) cannot leak.
    """
    if not isinstance(raw, list):
        return None
    c = Counter()
    for card in raw:
        if isinstance(card, dict) and isinstance(card.get("name"), str):
            c[(card["name"], _str_or_none(card.get("cost")), _str_or_none(card.get("star_cost")),
               _str_or_none(card.get("description")))] += 1
    return [{"name": n, "cost": cost, **({"star_cost": star} if star is not None else {}),
             "description": text, "count": k}
            for (n, cost, star, text), k in sorted(c.items(), key=lambda kv: _sort_key(kv[0]))]


MAX_GLOSSARY_TERMS = 60


def keyword_glossary(raw: Any) -> dict | None:
    """Tooltip term -> definition, from every ``keywords`` list the mod attached to this state.

    The mod serializes the game's own hover tips (card keywords, referenced powers and
    cards) as ``{"name", "description"}`` on hand cards, offers, relics, potions and
    powers, and once for the permanent deck as ``deck_keywords``. Collected verbatim,
    never inferred; sorted so the same state always yields the same glossary.
    """
    terms: dict = {}

    def walk(value, depth):
        if depth > 10:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("keywords", "deck_keywords") and isinstance(item, list):
                    for tip in item:
                        if (isinstance(tip, dict) and isinstance(tip.get("name"), str) and tip["name"].strip()
                                and isinstance(tip.get("description"), str) and tip["description"].strip()):
                            terms.setdefault(tip["name"].strip()[:80], tip["description"].strip()[:400])
                else:
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(raw, 0)
    return dict(sorted(terms.items())[:MAX_GLOSSARY_TERMS]) or None


def _keyword_names(value: Any) -> tuple | None:
    if not isinstance(value, list):
        return None
    names = (k if isinstance(k, str) else k.get("name") if isinstance(k, dict) else None for k in value)
    return tuple(n.strip()[:80] for n in names if isinstance(n, str) and n.strip())


def _deck_groups(raw: Any) -> list[dict] | None:
    """Permanent deck (patched mod only), grouped by identical card with counts.

    All-or-nothing: one malformed entry makes the deck unknown, never partial.
    Older patch builds send only id/is_upgraded; missing fields stay null, and
    ``keywords`` (tooltip term names) is present only when the mod build sends it.
    """
    if not isinstance(raw, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"].strip() for c in raw):
        return None
    c = Counter()
    for card in raw:
        upgraded = card.get("is_upgraded")
        c[(card["id"], _str_or_none(card.get("name")) or card["id"],
           upgraded if isinstance(upgraded, bool) else None, _str_or_none(card.get("cost")),
           _str_or_none(card.get("star_cost")), _str_or_none(card.get("type")),
           _str_or_none(card.get("rarity")), _str_or_none(card.get("description")),
           _keyword_names(card.get("keywords")))] += 1
    return [{"id": cid, "name": name, "upgraded": up, "cost": cost,
             **({"star_cost": star} if star is not None else {}),
             "type": ctype, "rarity": rarity, "description": text,
             **({"keywords": list(words)} if words is not None else {}), "count": k}
            for (cid, name, up, cost, star, ctype, rarity, text, words), k
            in sorted(c.items(), key=lambda kv: _sort_key(kv[0]))]


def classify_phase(raw: dict) -> str:
    st = raw.get("state_type")
    if st in COMBAT_STATE_TYPES:
        battle = raw.get("battle")
        if not isinstance(battle, dict):
            # Combat room after the fight: {"message": "Combat ended. Waiting for rewards..."}
            return PHASE_COMBAT_ENDED
        if battle.get("error"):
            return PHASE_COMBAT_BUSY
        if battle.get("is_play_phase") is True and battle.get("turn") == "player":
            return PHASE_COMBAT_READY
        return PHASE_COMBAT_BUSY
    if st == "hand_select":
        return PHASE_COMBAT_SELECTION
    if st == "game_over":
        return PHASE_GAME_OVER
    if st == "menu":
        return PHASE_MENU
    if st in POST_COMBAT_STATE_TYPES:
        return PHASE_POST_COMBAT
    if st == "overlay":
        return PHASE_OVERLAY
    return PHASE_UNKNOWN


def normalize(raw: dict) -> dict:
    """Build the policy observation. Never mutates ``raw``."""
    phase = classify_phase(raw)
    run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
    player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
    battle = raw.get("battle") if isinstance(raw.get("battle"), dict) else None

    hand = None
    if isinstance(player.get("hand"), list):
        hand, seen = [], Counter()
        for pos, c in enumerate(player["hand"]):
            if not isinstance(c, dict):
                continue
            occ = seen[c.get("id")]
            seen[c.get("id")] += 1
            hand.append(_hand_card(c, pos, occ))

    enemies = None
    if battle is not None and isinstance(battle.get("enemies"), list):
        enemies = [_enemy(e) for e in battle["enemies"] if isinstance(e, dict)]

    potions = None
    if isinstance(player.get("potions"), list):
        potions = [{
            "slot": _int_or_none(p.get("slot")),
            "id": _str_or_none(p.get("id")),
            "name": _str_or_none(p.get("name")),
            "description": _str_or_none(p.get("description")),
            "can_use_in_combat": p.get("can_use_in_combat") if isinstance(p.get("can_use_in_combat"), bool) else None,
            "target_type": _str_or_none(p.get("target_type")),
        } for p in player["potions"] if isinstance(p, dict)]

    relics = None
    if isinstance(player.get("relics"), list):
        relics = [{
            "id": _str_or_none(r.get("id")),
            "name": _str_or_none(r.get("name")),
            "description": _str_or_none(r.get("description")),
            "counter": _int_or_none(r.get("counter")),
        } for r in player["relics"] if isinstance(r, dict)]

    ascension = _int_or_none(run.get("ascension"))
    obs = {
        "schema_version": OBS_SCHEMA_VERSION,
        "phase": phase,
        "state_type": _str_or_none(raw.get("state_type")),
        "ready_for_action": phase == PHASE_COMBAT_READY,
        "run": {
            "character": _str_or_none(player.get("character")),
            "ascension": ascension,
            "difficulty": f"A{ascension}" if ascension is not None else None,
            "act": _int_or_none(run.get("act")),
            "floor": _int_or_none(run.get("floor")),
        },
        "combat": None if battle is None else {
            "round": _int_or_none(battle.get("round")),
            "turn_side": _str_or_none(battle.get("turn")),
            "is_play_phase": battle.get("is_play_phase") if isinstance(battle.get("is_play_phase"), bool) else None,
        },
        "player": {
            "hp": _int_or_none(player.get("hp")),
            "max_hp": _int_or_none(player.get("max_hp")),
            "block": _int_or_none(player.get("block")),
            "energy": _int_or_none(player.get("energy")),
            "max_energy": _int_or_none(player.get("max_energy")),
            "gold": _int_or_none(player.get("gold")),
            "statuses": _statuses(player.get("status")),
        },
        "hand": hand,
        "piles": {
            "draw_count": _int_or_none(player.get("draw_pile_count")),
            "discard_count": _int_or_none(player.get("discard_pile_count")),
            "exhaust_count": _int_or_none(player.get("exhaust_pile_count")),
            # Contents grouped by identical card, no order: what the in-game pile viewers show.
            "draw": _pile_groups(player.get("draw_pile")),
            "discard": _pile_groups(player.get("discard_pile")),
            "exhaust": _pile_groups(player.get("exhaust_pile")),
        },
        "enemies": enemies,
        "relics": relics,
        "potions": potions,
        "max_potion_slots": _int_or_none(player.get("max_potion_slots")),
        "screen": _screen_info(raw, phase),
    }
    # Read the permanent deck only when explicitly supplied by the mod. Combat
    # piles are not a permanent deck and must never be substituted here.
    if isinstance(player.get("deck"), list):
        obs["deck"] = _deck_groups(player["deck"])
    glossary = keyword_glossary(raw)
    if glossary:
        obs["keyword_glossary"] = glossary
    obs["fingerprint"] = fingerprint(obs)   # last: replay re-hashes the stored observation
    return obs


def _screen_info(raw: dict, phase: str) -> dict | None:
    """Small, non-combat context so stop reasons can say what screen we hit."""
    if phase == PHASE_COMBAT_SELECTION and isinstance(raw.get("hand_select"), dict):
        hs = raw["hand_select"]
        return {"mode": _str_or_none(hs.get("mode")), "prompt": _str_or_none(hs.get("prompt"))}
    if phase == PHASE_OVERLAY and isinstance(raw.get("overlay"), dict):
        return {"screen_type": _str_or_none(raw["overlay"].get("screen_type"))}
    if phase in (PHASE_COMBAT_ENDED, PHASE_MENU, PHASE_UNKNOWN) and isinstance(raw.get("message"), str):
        return {"message": raw["message"][:200]}
    return None


def fingerprint(obs: dict) -> str:
    """Stable hash of the decision-relevant observation (excludes the fingerprint itself)."""
    body = {k: v for k, v in obs.items() if k != "fingerprint"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def summarize(obs: dict) -> str:
    """Human-readable multi-line summary for the CLI."""
    lines = []
    r, p = obs["run"], obs["player"]
    lines.append(f"Phase: {obs['phase']} (state_type={obs['state_type']}, ready={obs['ready_for_action']})")
    lines.append(f"Run: {r['character'] or '?'} {r['difficulty'] or 'A?'} act={_u(r['act'])} floor={_u(r['floor'])}")
    if obs["combat"]:
        c = obs["combat"]
        lines.append(f"Combat: round={_u(c['round'])} side={_u(c['turn_side'])} play_phase={_u(c['is_play_phase'])}")
    lines.append(f"Player: HP {_u(p['hp'])}/{_u(p['max_hp'])}  block {_u(p['block'])}  energy {_u(p['energy'])}/{_u(p['max_energy'])}")
    if p["statuses"]:
        lines.append("  statuses: " + ", ".join(f"{s['name']}({_u(s['amount'])})" for s in p["statuses"]))
    if obs["hand"] is not None:
        lines.append("Hand:")
        for c in obs["hand"]:
            sup = "ok" if c["effects"]["fully_supported"] else "UNSUPPORTED"
            cost = "X" if c["cost_is_x"] else _u(c["cost"])
            lines.append(f"  [{c['index']}] {c['name']} (cost {cost}, {c['target_type']}, playable={_u(c['can_play'])}, {sup}) - {c['description']}")
    if obs["enemies"] is not None:
        lines.append("Enemies:")
        for e in obs["enemies"]:
            intents = "; ".join(f"{i['type']}:{i['label']}" for i in (e["intents"] or [])) or "?"
            lines.append(f"  #{e['combat_id']} {e['name']} ({e['entity_id']}) HP {_u(e['hp'])}/{_u(e['max_hp'])} block {_u(e['block'])}  intent: {intents}  incoming={_u(e['incoming_damage'])}")
            if e["statuses"]:
                lines.append("     statuses: " + ", ".join(f"{s['name']}({_u(s['amount'])})" for s in e["statuses"]))
    pl = obs["piles"]
    lines.append(f"Piles: draw={_u(pl['draw_count'])} discard={_u(pl['discard_count'])} exhaust={_u(pl['exhaust_count'])}")
    if obs["relics"] is not None:
        lines.append("Relics: " + (", ".join(x["name"] or "?" for x in obs["relics"]) or "none"))
    if obs["potions"] is not None:
        lines.append("Potions: " + (", ".join(f"[{x['slot']}] {x['name']}" for x in obs["potions"]) or "none"))
    if obs["screen"]:
        lines.append(f"Screen: {obs['screen']}")
    return "\n".join(lines)


def _u(v: Any) -> str:
    return "unknown" if v is None else str(v)
