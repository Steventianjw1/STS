"""Observational first-upgrade rates and timing from indexed Spire Codex runs."""
from __future__ import annotations

from statistics import median

from .expert_stats import _id, _withhold, evidence_strength


def _summary(rows, origin, min_samples):
    upgraded = [r for r in rows if r["upgrade_floor"] is not None]
    n, total = len(upgraded), len(rows)
    result = {"status": "available" if total else "unavailable", "numerator": n, "denominator": total,
              "rate_pct": round(100 * n / total, 2) if total else None,
              "contributing_players": len({r["username"] for r in rows}),
              "contributing_runs": len({r["run_hash"] for r in rows})}
    _withhold(result, min_samples)
    timing_strength = evidence_strength(n, len({r["username"] for r in upgraded}), min_samples)
    delays = [r["upgrade_floor"] - (r["born"] if origin is None else origin) for r in upgraded]
    result["timing"] = {"basis": "floors_after_acquisition" if origin is None else "floors_from_current_floor",
                        "conditional_on": "a first upgrade being recorded before removal or run end",
                        "samples": n, "evidence_strength": timing_strength,
                        "median_floors": median(delays) if timing_strength == "adequate" else None,
                        "mean_floors": round(sum(delays) / n, 2) if n and timing_strength == "adequate" else None}
    return result


def build_upgrade_evidence(view, rows, *, min_samples):
    """Keep action identifiers and observed upgrade state beside each card's evidence."""
    floor = (view.facts.get("run") or {}).get("floor")
    deck = view.facts.get("deck")
    result = {"tool": "get_card_upgrade_priority", "schema_version": 1,
              "source": "https://spire-codex.com/api/runs/shared/{run_hash}",
              "status": "unavailable", "cards": [],
              "label": "OBSERVED FIRST-UPGRADE FREQUENCY AND TIMING; advisory, not a prediction",
              "scope": {"game_mode": "standard", "players": 1, "deck_conditioned": False,
                        "cohort_restricted": True, "current_floor": floor},
              "limitations": [
                  "Bounded expert run history; rates are recorded outcomes, not calibrated future probabilities.",
                  "Removal, death and run end stop observation; missing or ambiguous lifetimes are excluded.",
                  "All permanent upgrade sources are pooled, including smithing, events, ancients and random upgrades.",
                  "Only first upgrades of copies acquired unupgraded are modeled; already-upgraded acquisitions are excluded.",
                  "Timing is conditional on an upgrade occurring; a high rate does not prove an upgrade's value.",
                  "Within-floor acquisition/upgrade ordering and effects of the current deck or relics are not modeled."]}
    if view.kind in ("card_reward", "shop"):
        entries = []
        for c in view.candidates:
            d = c["details"]
            cid = d.get("id") if c["kind"] == "select_card_reward" else d.get("card_id") if c["kind"] == "shop_purchase" else None
            if cid:
                entries.append({"id": cid, "is_upgraded": d.get("is_upgraded"), "action_id": c["action_id"]})
    else:
        entries = deck if isinstance(deck, list) else []
        if not isinstance(deck, list):
            result["reason"] = "observed_permanent_deck_unknown"
    by_id = {}
    for row in rows:
        by_id.setdefault(row["card_id"], []).append(row)
    for entry in entries:
        cid = _id(entry.get("id"))
        upgraded = entry.get("upgraded", entry.get("is_upgraded"))
        card = {"id": cid, "count": entry.get("count", 1), "upgraded": upgraded,
                "action_ids": [entry["action_id"]] if "action_id" in entry else [c["action_id"] for c in view.candidates
                               if c["kind"] in ("select_card", "select_card_reward", "shop_purchase")
                               and _id(c["details"].get("id") or c["details"].get("card_id")) == cid
                               and (upgraded is None or c["details"].get("is_upgraded") is None
                                    or c["details"]["is_upgraded"] == upgraded)]}
        if card["upgraded"] is True:
            card.update({"status": "not_applicable", "reason": "already_upgraded; first_upgrade_history_only"})
        elif cid:
            history = by_id.get(cid, [])
            picked = [r for r in history if r["picked"]]
            card["after_pick"] = _summary(picked, None, min_samples)
            if view.kind not in ("card_reward", "shop"):
                card["after_acquisition"] = _summary(history, None, min_samples)
                if type(floor) is int and floor >= 1:
                    remaining = [r for r in history if r["born"] < floor <= r["end_floor"]
                                 and (r["upgrade_floor"] is None or r["upgrade_floor"] >= floor)]
                    card["from_current_floor"] = _summary(remaining, floor, min_samples)
            card["status"] = (card["after_pick"]["status"] if view.kind in ("card_reward", "shop")
                              else "available" if history else "unavailable")
        else:
            card.update({"status": "unavailable", "reason": "unmatched_card_id"})
        result["cards"].append(card)
    available = sum(c["status"] == "available" for c in result["cards"])
    if available:
        result["status"] = "available" if all(c["status"] != "unavailable" for c in result["cards"]) else "partial"
    return result
