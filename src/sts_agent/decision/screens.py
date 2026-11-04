"""Screen adapters: raw STS2MCP state -> allowlisted facts + supported action candidates.

Every action kind built here was checked against the STS2MCP source at commit
55e0648 (``McpMod.Actions.cs`` / ``McpMod.StateBuilder.cs``). Nothing here has
been validated against a live game. Candidate ``action_id`` values and payloads
are produced only by this module; the model only picks an id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..observation import PHASE_COMBAT_ENDED, PHASE_COMBAT_READY, normalize
from ..combat_preview import action_preview
from .models import sha

SOURCE_INSPECTED = "source_inspected"
SYNTHETIC_ONLY = "synthetic_only"
UNSUPPORTED = "unsupported"
MOD_REVISION = "55e064850a68f3b4cde7e5fd525bf9b2dec4e885"

# kind -> capability. ``live_validated`` is a separate flag and is False everywhere.
CAPABILITIES: dict[str, dict] = {
    "combat": {"status": SOURCE_INSPECTED, "actions": ["play_card", "end_turn", "use_potion", "discard_potion"],
               "notes": "play_card only enqueues; resolution is observed. Targets use numeric combat_id."},
    "hand_select": {"status": SOURCE_INSPECTED, "actions": ["combat_select_card", "combat_confirm_selection"],
                    "notes": "Index = selectable hand holders (same list in state and action)."},
    "rewards": {"status": SOURCE_INSPECTED, "actions": ["claim_reward", "proceed", "discard_potion"],
                "notes": "Index = enabled reward buttons (same filter in state and action)."},
    "card_reward": {"status": SOURCE_INSPECTED, "actions": ["select_card_reward", "skip_card_reward"],
                    "notes": "Index = card holders sorted by position (same in state and action)."},
    "map": {"status": SOURCE_INSPECTED, "actions": ["choose_map_node"],
            "notes": "Index = travelable nodes ordered by column (same in state and action)."},
    "event": {"status": SOURCE_INSPECTED, "actions": ["choose_event_option", "advance_dialogue"],
              "notes": "Index = event option buttons (same list in state and action)."},
    "rest_site": {"status": SOURCE_INSPECTED, "actions": ["choose_rest_option", "proceed"],
                  "notes": "State lists room options; action indexes UI buttons. Same order ASSUMED, not proven."},
    "shop": {"status": SOURCE_INSPECTED, "actions": ["shop_purchase", "proceed"],
             "notes": "State concatenates card/relic/potion/removal entries; action indexes inventory.AllEntries. "
                      "Same order ASSUMED; the coordinator stops if a different item changes after a purchase."},
    "card_select": {"status": SOURCE_INSPECTED, "actions": ["select_card", "confirm_selection", "cancel_selection"],
                    "notes": "Grid screens do not expose which cards are currently selected."},
    "relic_select": {"status": SOURCE_INSPECTED, "actions": ["select_relic", "skip_relic_selection"],
                     "notes": "Index = relic holders (same list in state and action)."},
    "treasure": {"status": SOURCE_INSPECTED, "actions": ["claim_treasure_relic", "proceed"],
                 "notes": "The mod opens the chest as a side effect of reading state."},
    "bundle_select": {"status": UNSUPPORTED, "actions": [], "notes": "Actions exist in the mod; adapter not implemented."},
    "crystal_sphere": {"status": UNSUPPORTED, "actions": [], "notes": "Minigame actions exist in the mod; adapter not implemented."},
    "fake_merchant": {"status": UNSUPPORTED, "actions": [], "notes": "Shop-like event; adapter not implemented."},
    "overlay": {"status": UNSUPPORTED, "actions": [], "notes": "Unrecognized overlay; the mod itself reports manual interaction."},
    "menu": {"status": UNSUPPORTED, "actions": [], "notes": "Menus, popups and run start are out of scope."},
    "unknown": {"status": UNSUPPORTED, "actions": [], "notes": "Unrecognized state."},
}

COMBAT_STATE_TYPES = {"monster", "elite", "boss"}
NO_TARGET_CARD_TYPES = {"None", "Self", "AllEnemies"}  # mod passes target=null; verified in ExecutePlayCard


@dataclass
class ScreenView:
    kind: str
    state_type: str | None
    ready: bool
    in_combat: bool
    terminal: str | None = None          # "defeat" | "run_over_unconfirmed"
    unsupported: str | None = None       # missing capability description
    facts: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    bindings: dict = field(default_factory=dict)
    limitations: list = field(default_factory=list)
    encounter_key: str | None = None
    combat_round: int | None = None
    shop_stock: list | None = None
    fingerprint: str = ""

    @property
    def capability(self) -> dict:
        return CAPABILITIES.get(self.kind, CAPABILITIES["unknown"])


def _s(v, n=1000):
    return v[:n] if isinstance(v, str) else None


def _i(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class _Builder:
    def __init__(self):
        self.candidates, self.bindings = [], {}

    def add(self, action_id, kind, description, details, payload, resolution="change"):
        self.candidates.append({"action_id": action_id, "kind": kind, "description": description,
                                "details": details})
        self.bindings[action_id] = {"payload": payload, "resolution": resolution}


def _common_facts(obs: dict) -> dict:
    facts = {"run": obs["run"], "player": obs["player"], "relics": obs["relics"], "potions": obs["potions"],
             "max_potion_slots": obs["max_potion_slots"]}
    if "deck" in obs:
        facts["deck"] = obs["deck"]
    return facts


def _combat_facts(obs: dict) -> dict:
    f = _common_facts(obs)
    f.update({"combat": obs["combat"], "hand": obs["hand"], "piles": obs["piles"], "enemies": obs["enemies"]})
    return f


def _discard_potions(b: _Builder, obs: dict):
    for p in obs.get("potions") or []:
        if p.get("slot") is None:
            continue
        b.add(f"discard_potion_{p['slot']}", "discard_potion",
              f"Discard potion '{p['name']}' from slot {p['slot']} (frees the slot; potion is lost)",
              {"slot": p["slot"], "potion": p["name"]}, {"action": "discard_potion", "slot": p["slot"]})


def build_view(raw: dict) -> ScreenView:
    st = raw.get("state_type") if isinstance(raw.get("state_type"), str) else None
    obs = normalize(raw)
    run = obs["run"]
    floor_key = f"a{run['act']}f{run['floor']}"

    if st in COMBAT_STATE_TYPES:
        if obs["phase"] == PHASE_COMBAT_ENDED:
            v = ScreenView("combat_ended", st, False, False, facts=_common_facts(obs))
        else:
            v = _combat_view(obs, st)
        v.encounter_key = f"{floor_key}:combat"
    elif st == "hand_select":
        v = _hand_select_view(raw, obs)
        v.encounter_key = f"{floor_key}:combat"
    elif st == "game_over":
        hp = obs["player"]["hp"]
        v = ScreenView("game_over", st, False, False, facts=_common_facts(obs),
                       terminal="defeat" if hp is None or hp <= 0 else "run_over_unconfirmed")
    elif st in _ROOM_BUILDERS:
        v = _ROOM_BUILDERS[st](raw, obs)
        v.encounter_key = f"{floor_key}:room"
    else:  # menu / popup / anything unrecognized
        v = ScreenView("menu" if st == "menu" else "unknown", st, False, False, facts=_common_facts(obs))
    cap = CAPABILITIES.get(v.kind)
    if cap and cap["status"] == UNSUPPORTED:
        v.unsupported = f"{v.kind}: {cap['notes']}"
    v.fingerprint = "view-" + sha({"st": st, "kind": v.kind, "ready": v.ready, "facts": v.facts,
                                   "candidates": v.candidates, "bindings": v.bindings,
                                   "terminal": v.terminal, "unsupported": v.unsupported})
    return v


# -- combat --------------------------------------------------------------------

def _combat_view(obs: dict, st: str) -> ScreenView:
    ready = obs["phase"] == PHASE_COMBAT_READY
    v = ScreenView("combat", st, ready, True, facts=_combat_facts(obs),
                   combat_round=(obs["combat"] or {}).get("round"))
    v.limitations.append("Card 'effects' fields are a partial text-parser hint; the description is authoritative.")
    v.limitations.extend([
        "Enemy intents are simultaneous parts of the next enemy move, not successive rounds. "
        "intent_forecast offsets 2 and 3 are unknown: the API exposes no three-turn lookahead.",
        "effect_preview contains game-native current-state damage/block variables for each target. "
        "Player and target status/relic modifiers are already included; do not apply them twice. "
        "This is not a simulation: triggers, status changes between hits, replay, and later actions can change outcomes. "
        "Totals and HP loss marked if_unchanged are conditional, not guaranteed. Re-evaluate after every action.",
    ])
    if any(c.get("combat_previews") is None for c in obs["hand"] or []):
        v.limitations.append("Native combat previews unavailable for some cards. Install the agent preview mod extension; "
                             "untargeted descriptions alone do not establish target-specific damage/block.")
    if not ready:
        return v
    b = _Builder()
    enemies = [e for e in obs["enemies"] or [] if e.get("combat_id") is not None and (e.get("hp") is None or e["hp"] > 0)]
    enemies.sort(key=lambda e: e["combat_id"])
    for card in obs["hand"] or []:
        idx, name, tt = card["index"], card["name"], card["target_type"]
        if card["can_play"] is not True:
            if card["can_play"] is None:
                v.limitations.append(f"Hand card [{idx}] {name}: playability unknown; not offered.")
            continue
        cost = "X" if card["cost_is_x"] else card["cost"]
        details = {"hand_index": idx, "card": name, "cost": cost, "target_type": tt,
                   "description": card["description"]}
        if tt == "AnyEnemy":
            for e in enemies:
                b.add(f"play_{idx}_t{e['combat_id']}", "play_card",
                      f"Play {name} (cost {cost}) targeting {e['name']} #{e['combat_id']} (HP {e['hp']}/{e['max_hp']}, block {e['block']})",
                      {**details, "target_combat_id": e["combat_id"],
                       "effect_preview": action_preview(card, enemies, e)},
                      {"action": "play_card", "card_index": idx, "target": str(e["combat_id"])})
            if not enemies:
                v.limitations.append(f"Hand card [{idx}] {name}: needs an enemy target but none is targetable.")
        elif tt in NO_TARGET_CARD_TYPES:
            b.add(f"play_{idx}", "play_card", f"Play {name} (cost {cost}, {tt})",
                  {**details, "effect_preview": action_preview(card, enemies)},
                  {"action": "play_card", "card_index": idx})
        else:
            v.limitations.append(f"Hand card [{idx}] {name}: target type {tt!r} has no verified execution semantics; not offered.")
    for p in obs["potions"] or []:
        if p.get("slot") is None:
            continue
        if p.get("can_use_in_combat") is not True:
            continue
        base = {"slot": p["slot"], "potion": p["name"], "target_type": p["target_type"], "description": p["description"]}
        if p["target_type"] == "AnyEnemy":
            for e in enemies:
                b.add(f"potion_{p['slot']}_t{e['combat_id']}", "use_potion",
                      f"Use potion '{p['name']}' on {e['name']} #{e['combat_id']}", {**base, "target_combat_id": e["combat_id"]},
                      {"action": "use_potion", "slot": p["slot"], "target": str(e["combat_id"])})
        else:
            b.add(f"potion_{p['slot']}", "use_potion", f"Use potion '{p['name']}' ({p['target_type']})", base,
                  {"action": "use_potion", "slot": p["slot"]})
    _discard_potions(b, obs)
    b.add("end_turn", "end_turn", "End the turn", {}, {"action": "end_turn"}, resolution="end_turn")
    v.candidates, v.bindings = b.candidates, b.bindings
    return v


def _hand_select_view(raw: dict, obs: dict) -> ScreenView:
    hs = raw.get("hand_select") if isinstance(raw.get("hand_select"), dict) else {}
    cards = [c for c in hs.get("cards") or [] if isinstance(c, dict) and _i(c.get("index")) is not None]
    selected = [{"index": _i(c.get("index")), "name": _s(c.get("name"), 120)} for c in hs.get("selected_cards") or []
                if isinstance(c, dict)]
    facts = _combat_facts(obs)
    facts["selection"] = {"mode": _s(hs.get("mode"), 40), "prompt": _s(hs.get("prompt"), 300),
                          "selectable_cards": [{"index": c["index"], "name": _s(c.get("name"), 120),
                                                "type": _s(c.get("type"), 40), "cost": _s(c.get("cost"), 8),
                                                "description": _s(c.get("description"))} for c in cards],
                          "selected_cards": selected,
                          "can_confirm": hs.get("can_confirm") if isinstance(hs.get("can_confirm"), bool) else None}
    v = ScreenView("hand_select", "hand_select", True, True, facts=facts,
                   combat_round=(obs["combat"] or {}).get("round"))
    b = _Builder()
    for c in cards:
        b.add(f"hand_select_{c['index']}", "combat_select_card",
              f"Select {c.get('name')} for: {hs.get('prompt') or 'the current prompt'}",
              {"index": c["index"], "card": c.get("name")}, {"action": "combat_select_card", "card_index": c["index"]})
    if hs.get("can_confirm") is True:
        b.add("hand_select_confirm", "combat_confirm_selection", "Confirm the current hand selection", {},
              {"action": "combat_confirm_selection"})
    v.candidates, v.bindings = b.candidates, b.bindings
    return v


# -- rooms -----------------------------------------------------------------------

def _room(kind, raw, obs, screen_facts, ready=True) -> tuple[ScreenView, _Builder]:
    f = _common_facts(obs)
    f["screen_details"] = screen_facts
    return ScreenView(kind, raw.get("state_type"), ready, False, facts=f), _Builder()


def _finish(v: ScreenView, b: _Builder, obs: dict, potions=True) -> ScreenView:
    if potions and v.ready:
        _discard_potions(b, obs)
        if obs.get("potions"):
            v.limitations.append("Potion use outside combat is not offered: the API does not distinguish "
                                 "combat-only from any-time potions.")
    v.candidates, v.bindings = b.candidates, b.bindings
    return v


def _rewards(raw, obs):
    r = raw.get("rewards") if isinstance(raw.get("rewards"), dict) else {}
    items = [{"index": _i(x.get("index")), "type": _s(x.get("type"), 40), "description": _s(x.get("description")),
              "gold_amount": _i(x.get("gold_amount")), "potion_name": _s(x.get("potion_name"), 120),
              "potion_description": _s(x.get("potion_description"))}
             for x in r.get("items") or [] if isinstance(x, dict)]
    v, b = _room("rewards", raw, obs, {"items": items, "can_proceed": r.get("can_proceed")})
    for x in items:
        if x["index"] is None:
            continue
        b.add(f"reward_{x['index']}", "claim_reward", f"Claim reward: {x['description'] or x['type']}", x,
              {"action": "claim_reward", "index": x["index"]})
    if r.get("can_proceed") is True:
        b.add("proceed", "proceed", "Leave the rewards screen (unclaimed rewards are left behind)", {}, {"action": "proceed"})
    return _finish(v, b, obs)


def _card_info(c):
    return {"index": _i(c.get("index")), "id": _s(c.get("id"), 80), "name": _s(c.get("name"), 120),
            "type": _s(c.get("type"), 40), "cost": _s(c.get("cost"), 8), "rarity": _s(c.get("rarity"), 40),
            "is_upgraded": c.get("is_upgraded") if isinstance(c.get("is_upgraded"), bool) else None,
            "description": _s(c.get("description"))}


def _card_reward(raw, obs):
    r = raw.get("card_reward") if isinstance(raw.get("card_reward"), dict) else {}
    cards = [_card_info(c) for c in r.get("cards") or [] if isinstance(c, dict)]
    v, b = _room("card_reward", raw, obs, {"cards": cards, "can_skip": r.get("can_skip")})
    for c in cards:
        if c["index"] is not None:
            b.add(f"take_card_{c['index']}", "select_card_reward", f"Add {c['name']} to the deck", c,
                  {"action": "select_card_reward", "card_index": c["index"]})
    if r.get("can_skip") is True:
        b.add("skip_card_reward", "skip_card_reward", "Skip this card reward", {}, {"action": "skip_card_reward"})
    return _finish(v, b, obs, potions=False)


def _pt(p):
    return {"col": _i(p.get("col")), "row": _i(p.get("row")), "type": _s(p.get("type"), 40)} if isinstance(p, dict) else None


def _map_details(m):
    opts = []
    for o in m.get("next_options") or []:
        if isinstance(o, dict):
            opts.append({**_pt(o), "index": _i(o.get("index")),
                         "leads_to": [_pt(c) for c in o.get("leads_to") or []]})
    nodes = [{**_pt(n), "children": [c for c in n.get("children") or [] if isinstance(c, list)]}
             for n in m.get("nodes") or [] if isinstance(n, dict)]
    bosses = [{"id": _s(x.get("id"), 120), "name": _s(x.get("name"), 120), "col": _i(x.get("col")), "row": _i(x.get("row"))}
              for x in m.get("bosses") or [] if isinstance(x, dict)]
    return {"current_position": _pt(m.get("current_position")),
            "visited": [_pt(x) for x in m.get("visited") or []],
            "next_options": opts, "nodes": nodes, "bosses": bosses}


def _preview_map(m):
    """Map seen from a non-map screen: first nodes come from the graph, not travel buttons.

    Starts from the current position, else the parentless starting point. Indices are
    None because these nodes cannot be travelled to from the current screen.
    """
    details = _map_details(m)
    nodes = {(n["col"], n["row"]): n for n in details["nodes"]}
    here = details["current_position"]
    start = (here["col"], here["row"]) if here else None
    if start not in nodes:
        children = {tuple(c) for n in details["nodes"] for c in n["children"] if len(c) == 2}
        roots = [k for k in nodes if k not in children]
        start = min(roots, key=lambda k: (k[1] if k[1] is not None else 0)) if len(roots) >= 1 else None
    first = [nodes[tuple(c)] for c in (nodes[start]["children"] if start in nodes else []) if tuple(c) in nodes]
    details["next_options"] = [{"col": n["col"], "row": n["row"], "type": n["type"], "index": None,
                                "leads_to": [], "preview": True} for n in sorted(first, key=lambda n: n["col"] or 0)]
    details["preview_from"] = list(start) if start else None
    return details


def _map(raw, obs):
    m = raw.get("map") if isinstance(raw.get("map"), dict) else {}
    details = _map_details(m)
    opts = details["next_options"]
    v, b = _room("map", raw, obs, details, ready=bool(opts))
    for o in opts:
        if o["index"] is not None:
            b.add(f"map_{o['index']}", "choose_map_node",
                  f"Travel to {o['type']} at column {o['col']}, row {o['row']}", o,
                  {"action": "choose_map_node", "index": o["index"]})
    return _finish(v, b, obs)


def _event(raw, obs):
    e = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    opts = [{"index": _i(o.get("index")), "title": _s(o.get("title"), 200), "description": _s(o.get("description")),
             "is_locked": o.get("is_locked") is True, "is_proceed": o.get("is_proceed") is True,
             "was_chosen": o.get("was_chosen") is True, "relic_name": _s(o.get("relic_name"), 120),
             "relic_description": _s(o.get("relic_description"))}
            for o in e.get("options") or [] if isinstance(o, dict)]
    # Preserve stable option IDs when a mod build exposes them. Existing builds
    # provide titles only; the statistics adapter then requires an exact match.
    for option, raw_option in zip(opts, (o for o in e.get("options") or [] if isinstance(o, dict))):
        option_id = _s(raw_option.get("option_id"), 120)
        if option_id:
            option["option_id"] = option_id
    in_dialogue = e.get("in_dialogue") is True
    details = {"event_id": _s(e.get("event_id"), 80), "event_name": _s(e.get("event_name"), 120),
               "is_ancient": e.get("is_ancient") if isinstance(e.get("is_ancient"), bool) else None,
               "in_dialogue": in_dialogue, "body": _s(e.get("body"), 2000), "options": opts}
    if details["is_ancient"] and isinstance(e.get("agent_map"), dict):
        details["map"] = _preview_map(e["agent_map"])  # patched mod only; read-only planning view
    v, b = _room("event", raw, obs, details, ready=in_dialogue or bool(opts))
    if in_dialogue:
        b.add("advance_dialogue", "advance_dialogue", "Advance the dialogue", {}, {"action": "advance_dialogue"})
    else:
        for o in opts:
            if o["index"] is None or o["is_locked"]:
                continue
            b.add(f"event_{o['index']}", "choose_event_option", f"Choose: {o['title']}", o,
                  {"action": "choose_event_option", "index": o["index"]})
    return _finish(v, b, obs)


def _rest(raw, obs):
    r = raw.get("rest_site") if isinstance(raw.get("rest_site"), dict) else {}
    opts = [{"index": _i(o.get("index")), "id": _s(o.get("id"), 40), "name": _s(o.get("name"), 120),
             "description": _s(o.get("description")), "is_enabled": o.get("is_enabled") is True}
            for o in r.get("options") or [] if isinstance(o, dict)]
    v, b = _room("rest_site", raw, obs, {"options": opts, "can_proceed": r.get("can_proceed")})
    for o in opts:
        if o["index"] is not None and o["is_enabled"]:
            b.add(f"rest_{o['index']}", "choose_rest_option", f"Rest site: {o['name']}", o,
                  {"action": "choose_rest_option", "index": o["index"]})
    if r.get("can_proceed") is True:
        b.add("proceed", "proceed", "Leave the rest site", {}, {"action": "proceed"})
    v.limitations.append(CAPABILITIES["rest_site"]["notes"])
    return _finish(v, b, obs)


_SHOP_KEYS = ("category", "price", "is_stocked", "can_afford", "on_sale", "card_id", "card_name", "card_type",
              "card_cost", "card_rarity", "card_description", "relic_id", "relic_name", "relic_description",
              "potion_id", "potion_name", "potion_description")


def _shop(raw, obs):
    s = raw.get("shop") if isinstance(raw.get("shop"), dict) else {}
    items = []
    for x in s.get("items") or []:
        if isinstance(x, dict):
            it = {"index": _i(x.get("index"))}
            for k in _SHOP_KEYS:
                val = x.get(k)
                it[k] = val if isinstance(val, (int, bool)) or val is None else _s(val)
            items.append(it)
    not_ready = isinstance(s.get("error"), str)
    v, b = _room("shop", raw, obs, {"items": items, "can_proceed": s.get("can_proceed")}, ready=not not_ready)
    for it in items:
        if it["index"] is None or it["is_stocked"] is not True or it["can_afford"] is not True:
            continue
        label = it["card_name"] or it["relic_name"] or it["potion_name"] or it["category"]
        b.add(f"buy_{it['index']}", "shop_purchase", f"Buy {it['category']} '{label}' for {it['price']} gold", it,
              {"action": "shop_purchase", "index": it["index"]}, resolution=f"shop:{it['index']}")
    if s.get("can_proceed") is True:
        b.add("proceed", "proceed", "Leave the shop", {}, {"action": "proceed"})
    v.shop_stock = [(it["index"], it["is_stocked"]) for it in items]
    v.limitations.append(CAPABILITIES["shop"]["notes"])
    return _finish(v, b, obs)


def _card_select(raw, obs):
    cs = raw.get("card_select") if isinstance(raw.get("card_select"), dict) else {}
    stype = _s(cs.get("screen_type"), 60)
    cards = [_card_info(c) for c in cs.get("cards") or [] if isinstance(c, dict)]
    preview = [_card_info(c) for c in cs.get("preview_cards") or [] if isinstance(c, dict)]
    details = {"screen_type": stype, "prompt": _s(cs.get("prompt"), 300), "cards": cards,
               "preview_showing": cs.get("preview_showing") is True, "preview_cards": preview,
               "can_confirm": cs.get("can_confirm"), "can_cancel": cs.get("can_cancel"),
               "can_skip": cs.get("can_skip")}
    v, b = _room("card_select", raw, obs, details)
    choose = stype == "choose"
    if not details["preview_showing"]:
        for c in cards:
            if c["index"] is None:
                continue
            verb = "Choose" if choose else "Toggle selection of"
            b.add(f"select_card_{c['index']}", "select_card", f"{verb} {c['name']}", c,
                  {"action": "select_card", "index": c["index"]}, resolution="change" if choose else "toggle")
    if not choose and cs.get("can_confirm") is True:
        b.add("confirm_selection", "confirm_selection", "Confirm the current selection", {}, {"action": "confirm_selection"})
    if cs.get("can_cancel") is True:
        b.add("cancel_selection", "cancel_selection",
              "Skip this choice" if choose else ("Cancel the preview" if details["preview_showing"] else "Close/cancel this selection"),
              {}, {"action": "cancel_selection"})
    if not choose:
        v.limitations.append("This grid does not expose which cards are currently selected; selecting a selected "
                             "card deselects it. Toggles you sent are listed in recent_history.")
    return _finish(v, b, obs, potions=False)


def _relic_select(raw, obs):
    r = raw.get("relic_select") if isinstance(raw.get("relic_select"), dict) else {}
    relics = [{"index": _i(x.get("index")), "id": _s(x.get("id"), 80), "name": _s(x.get("name"), 120),
               "rarity": _s(x.get("rarity"), 40), "description": _s(x.get("description"))}
              for x in r.get("relics") or [] if isinstance(x, dict)]
    v, b = _room("relic_select", raw, obs, {"prompt": _s(r.get("prompt"), 300), "relics": relics,
                                            "can_skip": r.get("can_skip")})
    for x in relics:
        if x["index"] is not None:
            b.add(f"relic_{x['index']}", "select_relic", f"Take relic {x['name']}", x,
                  {"action": "select_relic", "index": x["index"]})
    if r.get("can_skip") is True:
        b.add("skip_relic", "skip_relic_selection", "Skip the relic choice", {}, {"action": "skip_relic_selection"})
    return _finish(v, b, obs, potions=False)


def _treasure(raw, obs):
    t = raw.get("treasure") if isinstance(raw.get("treasure"), dict) else {}
    relics = [{"index": _i(x.get("index")), "id": _s(x.get("id"), 80), "name": _s(x.get("name"), 120),
               "rarity": _s(x.get("rarity"), 40), "description": _s(x.get("description"))}
              for x in t.get("relics") or [] if isinstance(x, dict)]
    opening = isinstance(t.get("message"), str) and not relics and t.get("can_proceed") is not True
    v, b = _room("treasure", raw, obs, {"relics": relics, "can_proceed": t.get("can_proceed")}, ready=not opening)
    for x in relics:
        if x["index"] is not None:
            b.add(f"treasure_{x['index']}", "claim_treasure_relic", f"Take relic {x['name']}", x,
                  {"action": "claim_treasure_relic", "index": x["index"]})
    if t.get("can_proceed") is True:
        b.add("proceed", "proceed", "Leave the treasure room", {}, {"action": "proceed"})
    return _finish(v, b, obs)


def _unsupported(kind):
    def f(raw, obs):
        return ScreenView(kind, raw.get("state_type"), False, False, facts=_common_facts(obs))
    return f


_ROOM_BUILDERS = {
    "rewards": _rewards, "card_reward": _card_reward, "map": _map, "event": _event, "rest_site": _rest,
    "shop": _shop, "card_select": _card_select, "relic_select": _relic_select, "treasure": _treasure,
    "bundle_select": _unsupported("bundle_select"), "crystal_sphere": _unsupported("crystal_sphere"),
    "fake_merchant": _unsupported("fake_merchant"), "overlay": _unsupported("overlay"),
}


def capability_table() -> list[dict]:
    return [{"screen": k, "status": v["status"], "live_validated": False, "actions": v["actions"], "notes": v["notes"]}
            for k, v in CAPABILITIES.items()]
