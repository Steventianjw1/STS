"""Layer B: pooled HP loss per node class for one character and act variant.

Measured from indexed runs as % of max HP: mean, 90th percentile and death rate for
early hallway, later hallway, elite, boss and ? nodes. Rest-site HEAL amounts, gold
changes and shop spending are measured the same way; no game constants are assumed.
"""
from __future__ import annotations

from .route_profile import _pct

CLASSES = ("early_hallway", "later_hallway", "elite", "boss", "unknown")


def _q(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _summary(values):
    return {"n": len(values), "mean": round(sum(values) / len(values), 1) if values else None}


def early_fight_count(runs, act_id, weak_ids):
    """Largest K such that hallway fights 1..K are each mostly (>=50%) weak encounters."""
    if not weak_ids:
        return None
    by_ordinal = {}
    for run in runs:
        ordinal = 0
        for f in run["floors"]:
            if f["act_id"] == act_id and f["room_type"] == "monster" and f["encounter_id"]:
                ordinal += 1
                counts = by_ordinal.setdefault(ordinal, [0, 0])
                counts[0] += f["encounter_id"] in weak_ids
                counts[1] += 1
    k = 0
    while k + 1 in by_ordinal and by_ordinal[k + 1][0] * 2 >= by_ordinal[k + 1][1]:
        k += 1
    return k if by_ordinal else None


def node_class(map_point_type, hallway_ordinal, early_k):
    """Damage class of a map node; ``hallway_ordinal`` counts hallway fights in this act including this one."""
    if map_point_type == "monster":
        if early_k is None:
            return "later_hallway"  # no split: both classes hold the pooled hallway sample
        return "early_hallway" if hallway_ordinal <= early_k else "later_hallway"
    return map_point_type if map_point_type in ("elite", "boss", "unknown") else None


def build_risk_model(runs, act_id, *, weak_ids, min_samples, fallback=None):
    """``runs``: RunHistoryIndex.route_rows output. ``fallback``: per-class encounter-stats rows or None."""
    kept = [r for r in runs if not r["abandoned"]]
    early_k = early_fight_count(kept, act_id, weak_ids)
    samples = {c: [] for c in CLASSES}
    deaths = {c: 0 for c in CLASSES}
    heal, gold, spend = [], {}, []
    for run in kept:
        last_floor = run["floors"][-1]["floor"]
        prev, ordinal = None, 0
        for f in run["floors"]:
            if f["act_id"] != act_id:
                prev = f
                continue
            if f["room_type"] == "monster":
                ordinal += 1
            kind = f["map_point_type"]
            cls = node_class(kind, ordinal, early_k) if kind != "monster" or f["room_type"] == "monster" else None
            pct = _pct(f["damage_taken"], f["max_hp"])
            if cls and pct is not None:
                samples[cls].append(pct)
                deaths[cls] += int(run["win"] == 0 and f["floor"] == last_floor)
            if prev is not None and prev["gold"] is not None and f["gold"] is not None:
                delta = f["gold"] - prev["gold"]
                if f["room_type"] == "shop":
                    spend.append(-delta)
                elif kind:
                    gold.setdefault(kind, []).append(delta)
            if ("HEAL" in f["rest_choices"] and prev is not None and prev["hp"] is not None
                    and f["hp"] is not None and f["max_hp"] and f["hp"] < f["max_hp"]):
                heal.append(max(0.0, (f["hp"] - prev["hp"]) / f["max_hp"] * 100))  # uncapped heals only
            prev = f
    if early_k is None:  # pooled hallway sample serves both hallway classes
        samples["early_hallway"] = list(samples["later_hallway"])
        deaths["early_hallway"] = deaths["later_hallway"]
    classes = {}
    for cls in CLASSES:
        values, n = samples[cls], len(samples[cls])
        row = {"n": n, "mean_pct": round(sum(values) / n, 1) if n else None,
               "p90_pct": round(_q(values, 0.9), 1) if n >= min_samples else None,
               "death_rate_pct": round(deaths[cls] / n * 100, 2) if n else None,
               "source": "indexed_runs", "sparse": n < min_samples}
        alt = (fallback or {}).get(cls)
        if n < min_samples and alt and alt.get("n"):
            row.update({"mean_pct": alt["mean_pct"], "death_rate_pct": alt["death_rate_pct"],
                        "source": "encounter_stats", "fallback_n": alt["n"], "own_n": n})
        if row["mean_pct"] is None:
            row["source"] = "unavailable"
        classes[cls] = row
    return {
        "classes": classes,
        "early_hallway_fights": early_k,
        "early_split": ("weak_encounter_share" if early_k is not None
                        else "unavailable: early/later hallway pooled together"),
        "rest_heal_pct": {**_summary(heal), "basis": "HEAL choices that did not reach max HP"},
        "gold_change_by_node": {k: _summary(v) for k, v in sorted(gold.items())},
        "shop_spend": _summary(spend),
    }
