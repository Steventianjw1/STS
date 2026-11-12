"""What-if tools: two bounded, read-only calculations the model may request once per decision.

Everything the harness can decide is relevant is pushed into the frame. These tools cover
the open-ended remainder, where the question depends on a hypothesis only the model has:

* ``deck_after``       - deck composition before/after hypothetical additions and removals.
* ``route_projection`` - the stored route's remaining nodes re-projected from hypothetical HP
                         values (for example resting versus smithing before the next fights).

A tool never touches the game, never selects an action and never raises: bad arguments come
back as ``{"status": "error", ...}`` and the model must then choose from the frame as it is.
"""
from __future__ import annotations

import random

from ..route_paths import SIMULATIONS, simulate
from ..route_risk import agent_loss_scale, loss_draws

MAX_TOOL_ARG_ITEMS = 6
_DECK_SCREENS = ("card_reward", "shop", "card_select", "event", "rest_site", "relic_select", "treasure")


def _card_of(candidate: dict) -> dict | None:
    """The card a candidate would add to the deck, or None."""
    d = candidate.get("details") or {}
    if candidate.get("kind") == "select_card_reward" and d.get("name"):
        return {"name": d["name"], "type": d.get("type"), "cost": d.get("cost"), "rarity": d.get("rarity")}
    if candidate.get("kind") == "shop_purchase" and d.get("card_name"):
        return {"name": d["card_name"], "type": d.get("card_type"), "cost": d.get("card_cost"),
                "rarity": d.get("card_rarity")}
    return None


def _summary(cards: list) -> dict:
    """``cards``: one dict per physical card."""
    n = len(cards)
    by_type, costs = {}, {"0": 0, "1": 0, "2": 0, "3+": 0, "X": 0, "none_or_unknown": 0}
    numeric = []
    for c in cards:
        by_type[c.get("type") or "unknown"] = by_type.get(c.get("type") or "unknown", 0) + 1
        cost = str(c.get("cost") or "").strip().upper()
        if cost.isdigit():
            numeric.append(int(cost))
            costs[cost if int(cost) < 3 else "3+"] += 1
        else:
            costs["X" if cost == "X" else "none_or_unknown"] += 1
    return {"cards": n, "by_type": dict(sorted(by_type.items())),
            "basic_cards": sum(c.get("rarity") == "Basic" for c in cards),
            "curse_and_status_cards": sum(c.get("type") in ("Curse", "Status") for c in cards),
            "cost_counts": costs, "mean_cost_of_numeric_costs": round(sum(numeric) / len(numeric), 2) if numeric else None,
            "turns_to_draw_whole_deck_at_5_per_turn": round(n / 5, 1) if n else None,
            "chance_one_given_card_is_in_a_5_card_opening_hand_pct": round(min(1.0, 5 / n) * 100, 1) if n else None}


class WhatIfTools:
    def __init__(self, coordinator):
        self.c = coordinator

    # ------------------------------------------------------------ availability
    def _route_inputs(self, frame):
        run, player = frame.facts.get("run") or {}, frame.facts.get("player") or {}
        model = getattr(self.c.expert_stats, "last_route_model", None)
        # The frame's own route_context already says which rooms of the stored plan are still ahead.
        stored = (frame.facts.get("route_context") or {}).get("stored_route_plan") or {}
        remaining = [str(r.get("type")) for r in stored.get("remaining_rooms") or []]
        if (not model or model.get("act") != run.get("act") or not remaining or not player.get("max_hp")
                or frame.screen.get("kind") == "map" or "combat" in frame.facts):
            return None
        return model, remaining, player

    def available(self, frame) -> list[dict]:
        kind = frame.screen.get("kind")
        tools = []
        if kind in _DECK_SCREENS and not frame.screen.get("during_combat") and isinstance(frame.facts.get("deck"), list):
            addable = [c["action_id"] for c in frame.candidates if _card_of(c)]
            tools.append({
                "name": "deck_after",
                "description": "Deck composition before and after hypothetical changes: size, cards by type, cost "
                               "counts, basics, curses, draw-cycle length. Arithmetic on the permanent deck only; "
                               "it does not judge card quality.",
                "args": {"add": f"optional list of action_ids that add a card (offered here: {addable})",
                         "remove": "optional list of card names currently in facts.deck (one copy each)"}})
        if self._route_inputs(frame):
            tools.append({
                "name": "route_projection",
                "description": "Re-project the remaining nodes of your stored route from hypothetical current HP "
                               "values, e.g. HP after resting versus HP after smithing: expected and worst-tenth "
                               "boss-arrival HP and rough death risk before the next rest and before the boss. "
                               "Same pooled model and limits as the map screen's paths.",
                "args": {"hp_values": "list of 1 to 3 integer HP values between 1 and your max HP"}})
        return tools

    # --------------------------------------------------------------------- run
    def run(self, name, args, frame) -> dict:
        try:
            if name not in {t["name"] for t in self.available(frame)}:
                return {"tool": str(name)[:40], "status": "error", "error": "tool is not available on this screen"}
            if not isinstance(args, dict):
                return {"tool": name, "status": "error", "error": "args must be an object"}
            return {"tool": name, **(self._deck_after(args, frame) if name == "deck_after"
                                     else self._route_projection(args, frame))}
        except Exception as exc:   # a calculation must never stop the decision
            return {"tool": str(name)[:40], "status": "error", "error": f"internal_error:{type(exc).__name__}"}

    def _deck_after(self, args, frame) -> dict:
        add, remove = args.get("add") or [], args.get("remove") or []
        if (not isinstance(add, list) or not isinstance(remove, list) or len(add) + len(remove) > MAX_TOOL_ARG_ITEMS
                or not all(isinstance(x, str) for x in add + remove)):
            return {"status": "error", "error": f"add and remove must be lists of strings, {MAX_TOOL_ARG_ITEMS} entries in total at most"}
        cards = [{"name": g.get("name"), "type": g.get("type"), "cost": g.get("cost"), "rarity": g.get("rarity")}
                 for g in frame.facts["deck"] for _ in range(g.get("count") or 1)]
        before, added, removed = _summary(cards), [], []
        for aid in add:
            card = _card_of(frame.candidate(aid) or {})
            if card is None:
                return {"status": "error", "error": f"{aid[:60]!r} is not an offered action that adds a card"}
            cards.append(card)
            added.append(card["name"])
        for name in remove:
            hit = next((c for c in cards if str(c["name"]).lower() == name.strip().lower()), None)
            if hit is None:
                return {"status": "error", "error": f"no card named {name[:60]!r} in the deck"}
            cards.remove(hit)
            removed.append(hit["name"])
        return {"status": "ok", "added": added, "removed": removed, "before": before, "after": _summary(cards),
                "copies_after": {n: sum(c["name"] == n for c in cards) for n in added},
                "limits": "Counts only. Draw-cycle numbers assume 5 cards drawn per turn and no draw, discard, "
                          "exhaust or retain effects; whether a removal is legal is decided by the game, not here."}

    def _route_projection(self, args, frame) -> dict:
        model, remaining, player = self._route_inputs(frame)
        values, max_hp = args.get("hp_values"), player["max_hp"]
        if (not isinstance(values, list) or not 1 <= len(values) <= 3
                or not all(type(v) is int and 1 <= v <= max_hp for v in values)):
            return {"status": "error", "error": f"hp_values must be 1 to 3 integers between 1 and {max_hp}"}
        risk, act = model["risk"], (frame.facts.get("run") or {}).get("act")
        scale = agent_loss_scale(self.c.route_outcomes)
        factors = {group: row["value"] for group, row in scale["by_group"].items()}   # as on the map screen
        rng = random.Random(20261112)   # fixed, as on the map screen: the same question gives the same numbers
        uniforms = [[rng.random() for _ in remaining] for _ in range(SIMULATIONS)]
        path = [(kind, None, None) for kind in remaining]
        rows = []
        for hp in values:
            sim = simulate(path, hp_pct=hp / max_hp * 100, gold=player.get("gold") or 0, risk=risk,
                           profile=model.get("profile"), hallway_done=self.c.hallway_done.get(act, 0),
                           draws=loss_draws(risk), scale=factors, uniforms=uniforms)
            rows.append({"hp": hp, "hp_pct": round(hp / max_hp * 100, 1),
                         "expected_boss_arrival_hp_pct": round(sim["boss_arrival"], 1),
                         "p10_boss_arrival_hp_pct": _r(sim["p10_boss_arrival"]),
                         "expected_min_hp_pct": round(sim["min_exp"][0], 1),
                         "fights_before_next_rest": sim["fights_before_rest"],
                         "death_risk_before_next_rest_pct": _r(sim["death_before_rest"]),
                         "death_risk_before_boss_pct": _r(sim["death_before_boss"])})
        return {"status": "ok", "remaining_nodes": remaining, "agent_loss_scale_by_group": factors, "projections": rows,
                "limits": "Pooled fight losses from other players' runs times this run's loss scale for that kind of "
                          "fight, with the pooled same-floor recovery added back; the boss fight "
                          "itself is not subtracted; deck, relics, potions and upgrades are not modeled, so the HP "
                          "value of a smith upgrade is not in these numbers. Rough estimates, not calibrated "
                          "probabilities. The remaining nodes come from your stored route plan and may be stale."}


def _r(value):
    return None if value is None else round(value, 1)
