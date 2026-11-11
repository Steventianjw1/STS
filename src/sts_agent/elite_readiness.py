"""Elite readiness: Spire Codex elite threats vs. the observed permanent deck.

Facts come from the Spire Codex catalogs (encounters, monsters, powers, cards) and
community outcomes (/api/runs/encounter-builds). Gaps are explicit, documented rules
over those facts, never a strength score. Unknown data stays unknown.
"""
from __future__ import annotations

import re

# From the Spire Codex ascension catalog: 8 "Tough Enemies", 9 "Deadly Enemies".
HP_ASCENSION_LEVEL = 8
DAMAGE_ASCENSION_LEVEL = 9
BUILDS_PER_SIDE = 2


def _cid(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:CARD|MONSTER|POWER|ENCOUNTER)\.", "", value.strip().upper())
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", value) else None


def _bonus(card, field, upgraded):
    base = card.get(field)
    if type(base) not in (int, float):
        return None
    if upgraded:
        m = re.fullmatch(r"\+\s*(\d+)", str((card.get("upgrade") or {}).get(field, "")).strip())
        if m:
            base += int(m.group(1))
    return base


def card_traits(card, upgraded=False):
    """What a card can do according to its catalog fields (never its rules text)."""
    damage, block = _bonus(card, "damage", upgraded), _bonus(card, "block", upgraded)
    target, kind, rarity = card.get("target"), card.get("type"), card.get("rarity")
    return {
        "aoe_attack": target == "AllEnemies" and bool(damage),
        "random_target_attack": target == "RandomEnemy" and bool(damage),
        "block": block or 0,
        "non_basic_block": bool(block) and rarity != "Basic",
        "non_basic_attack": kind == "Attack" and bool(damage) and rarity != "Basic",
        "power": kind == "Power",
    }


def deck_coverage(deck, cards):
    """Compare observed deck groups with the card catalog."""
    if deck is None:
        return None
    out = {"cards": sum(entry.get("count", 1) for entry in deck), "unknown_card_ids": [],
           "aoe_attacks": [], "random_target_attacks": [],
           "non_basic_block_cards": [], "non_basic_attacks": [], "powers": [], "max_card_block": 0}
    for entry in deck:
        cid = _cid(entry.get("id"))
        card = cards.get(cid)
        if card is None:
            out["unknown_card_ids"].append(entry.get("id"))
            continue
        t = card_traits(card, entry.get("upgraded", entry.get("is_upgraded")) is True)
        for key, flag in (("aoe_attacks", "aoe_attack"), ("random_target_attacks", "random_target_attack"),
                          ("non_basic_block_cards", "non_basic_block"), ("non_basic_attacks", "non_basic_attack"),
                          ("powers", "power")):
            if t[flag]:
                out[key].append(cid)
        out["max_card_block"] = max(out["max_card_block"], t["block"])
    for key in ("aoe_attacks", "random_target_attacks", "non_basic_block_cards", "non_basic_attacks", "powers"):
        out[key] = sorted(set(out[key]))
    return out


def _move_total(move, ascension):
    d = move.get("damage")
    if not isinstance(d, dict):
        return None
    per = d.get("ascension") if ascension >= DAMAGE_ASCENSION_LEVEL and d.get("ascension") is not None else d.get("normal")
    if type(per) not in (int, float):
        return None
    hits = d.get("hit_count") if type(d.get("hit_count")) is int and d.get("hit_count") > 0 else 1
    return per * hits


def _mentions_strength(value):
    if isinstance(value, dict):
        return any(_mentions_strength(v) for v in value.values()) or _cid(value.get("power_id")) == "STRENGTH"
    if isinstance(value, list):
        return any(_mentions_strength(v) for v in value)
    return False


def threat_profile(encounter, monsters, powers, ascension):
    """Catalog facts about one elite encounter. Missing catalog values stay None."""
    members = [m for m in encounter.get("monsters") or [] if isinstance(m, dict)]
    found = [(m, monsters.get(_cid(m.get("id")))) for m in members]
    summons, scaling, hp_total, attack_turn, biggest = [], [], 0, 0, []
    hp_known = attack_known = bool(found)
    for ref, mon in found:
        if mon is None:
            hp_known = attack_known = False
            continue
        hp = mon.get("max_hp_ascension") if ascension >= HP_ASCENSION_LEVEL and mon.get("max_hp_ascension") else mon.get("max_hp")
        if type(hp) in (int, float):
            hp_total += hp
        else:
            hp_known = False
        moves = [mv for mv in mon.get("moves") or [] if isinstance(mv, dict)]
        totals = [t for t in (_move_total(mv, ascension) for mv in moves) if t is not None]
        if totals:
            attack_turn += max(totals)
            biggest.append({"monster": mon.get("name"), "largest_attack": max(totals)})
        if any("Summon" in str(mv.get("intent", "")) for mv in moves):
            summons.append(f"{mon.get('name')}: Summon move")
        for p in mon.get("innate_powers") or []:
            text = str((powers.get(_cid(p.get("power_id"))) or {}).get("description", ""))
            if "summon" in text.lower():
                summons.append(f"{mon.get('name')}: innate {p.get('power_id')}")
        if any("Buff" in str(mv.get("intent", "")) and _mentions_strength(mv) for mv in moves):
            scaling.append(mon.get("name"))
    return {
        "encounter_id": _cid(encounter.get("id")), "name": encounter.get("name"),
        "enemies": len(members), "summons": summons, "gains_strength": scaling,
        "total_hp": hp_total if hp_known else None,
        "max_attack_turn_upper_bound": attack_turn if attack_known and biggest else None,
        "largest_attacks": biggest,
        "values_basis": f"ascension values from A{HP_ASCENSION_LEVEL} (HP) / A{DAMAGE_ASCENSION_LEVEL} (damage)",
    }


def gaps(threat, coverage, *, max_hp, block_attack_pct):
    """Explicit rules; each gap names its rule and evidence. ``coverage`` None -> None (unknown)."""
    if coverage is None:
        return None
    out = []
    if threat["enemies"] > 1 or threat["summons"]:
        if not coverage["aoe_attacks"]:
            out.append({"need": "aoe", "rule": "multiple enemies or summons, and no AllEnemies attack in the deck",
                        "partial_cover": coverage["random_target_attacks"]})
    hit = threat["max_attack_turn_upper_bound"]
    heavy = hit is not None and max_hp and hit / max_hp * 100 >= block_attack_pct
    if (heavy or threat["gains_strength"]) and not coverage["non_basic_block_cards"]:
        out.append({"need": "block", "rule": f"largest attack turn >= {block_attack_pct:g}% of max HP or the elite "
                    "gains Strength, and no non-starter block card in the deck",
                    "evidence": {"max_attack_turn": hit, "max_hp": max_hp, "gains_strength": threat["gains_strength"]}})
    if not coverage["non_basic_attacks"] and not coverage["powers"]:
        out.append({"need": "damage_or_scaling", "rule": "no non-starter attack and no Power card in the deck"})
    return out


def builds_summary(payload, character, deck_ids):
    """Lowest and highest death-rate community builds for this character at one encounter."""
    if not isinstance(payload, dict) or payload.get("available") is not True:
        return {"status": "unavailable", "reason": "no_build_data"}
    rows = []
    for b in payload.get("builds") or []:
        if not isinstance(b, dict) or _cid(b.get("character")) != character or type(b.get("death_rate")) not in (int, float):
            continue
        cards = [_cid(c.get("id")) for c in b.get("defining_cards") or [] if isinstance(c, dict) and _cid(c.get("id"))]
        rows.append({"name": b.get("name"), "defining_cards": cards, "death_rate_pct": b["death_rate"],
                     "reached": b.get("reached"), "deaths": b.get("deaths"),
                     "deck_has_defining_cards": sorted(set(cards) & deck_ids) if deck_ids is not None else None})
    if not rows:
        return {"status": "unavailable", "reason": "no_builds_for_character"}
    rows.sort(key=lambda r: r["death_rate_pct"])
    return {"status": "available", "lowest_death_rate": rows[:BUILDS_PER_SIDE],
            "highest_death_rate": rows[-BUILDS_PER_SIDE:][::-1] if len(rows) > BUILDS_PER_SIDE else [],
            "basis": "community builds that reached this fight; correlation, not a counter guarantee"}


def offered_fit(offered_ids, cards, pools):
    """Which elite gaps each offered card would address, by its catalog traits."""
    out = []
    for cid in offered_ids:
        card = cards.get(cid)
        if card is None:
            out.append({"id": cid, "status": "unknown_card"})
            continue
        t = card_traits(card)
        needs = {"aoe": t["aoe_attack"], "block": t["non_basic_block"],
                 "damage_or_scaling": t["non_basic_attack"] or t["power"]}
        fills, builds = [], []
        for pool in pools:
            for e in pool["elites"]:
                for g in e.get("gaps") or []:
                    if needs.get(g["need"]):
                        fills.append({"need": g["need"], "elite": e["name"], "act": pool["act_id"]})
                for b in (e.get("builds") or {}).get("lowest_death_rate") or []:
                    if cid in b["defining_cards"]:
                        builds.append({"elite": e["name"], "act": pool["act_id"], "build": b["name"],
                                       "death_rate_pct": b["death_rate_pct"]})
        out.append({"id": cid, "name": card.get("name"), "traits": {k: v for k, v in t.items() if v},
                    "fills_gaps": fills, "defines_low_death_builds": builds})
    return out


def pool_gap_share(pool):
    """Share of this act's elites with at least one gap (None if the deck is unknown)."""
    elites = pool["elites"]
    if not elites or any(e.get("gaps") is None for e in elites):
        return None
    return round(sum(bool(e["gaps"]) for e in elites) / len(elites), 2)
