"""Layer B: pooled HP loss per node class for one character and act variant.

Measured from indexed runs as % of max HP: mean, 90th percentile and death rate for
early hallway, later hallway, elite, boss and ? nodes. Rest-site HEAL amounts, gold
changes and shop spending are measured the same way; no game constants are assumed.

Loss is the run file's ``damage_taken``: HP removed on the floor (unblocked and
self-inflicted), before any healing. HP regained on the same floor (relics, potions,
rewards) is measured separately as ``recovery_pct`` so a flat heal is added back
rather than hidden inside a multiplier.
"""
from __future__ import annotations

from .route_profile import _pct

CLASSES = ("early_hallway", "later_hallway", "elite", "boss", "unknown")
# Which of this run's fights correct which projected nodes. Small fights say little about big ones.
SCALE_GROUPS = {"early_hallway": "hallway", "later_hallway": "hallway", "unknown": "hallway",
                "elite": "elite_boss", "boss": "elite_boss"}


def _q(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def public_risk(risk):
    """The risk model without its private per-class sample lists (prompt- and trace-safe)."""
    return {**risk, "classes": {k: {f: v for f, v in c.items() if not f.startswith("_")}
                                for k, c in risk["classes"].items()}}


def loss_draws(risk):
    """Per class: a sorted list of HP-loss draws (% of max HP) to sample from, or None when no shape is known.

    A class with its own samples uses them, rescaled to the reported mean when that mean
    came from the encounter-stats fallback. A class with too few samples borrows the pooled
    shape of all observed fights (loss / class mean) scaled to its mean. Nothing is invented:
    with no observed fights at all the class stays None and callers fall back to the mean.
    """
    classes = risk["classes"]
    shape = sorted(v / c["_mean_own"] for c in classes.values() if c.get("_mean_own")
                   for v in c["_samples"])
    out = {}
    for cls, c in classes.items():
        mean, own = c["mean_pct"], c["_samples"]
        if mean is None:
            out[cls] = None
        elif len(own) >= 5 and c.get("_mean_own"):
            out[cls] = [v * mean / c["_mean_own"] for v in own]
        else:
            out[cls] = [r * mean for r in shape] if len(shape) >= 5 else None
    return out


def _shrunk_ratio(pairs, prior_fights, low, high):
    if not pairs:
        return {"value": 1.0, "fights": 0}
    realized, expected = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    prior = prior_fights * expected / len(pairs)
    return {"value": round(min(high, max(low, (realized + prior) / (expected + prior))), 2), "fights": len(pairs),
            "raw_ratio": round(realized / expected, 2), "realized_pct_total": round(realized, 1),
            "expected_pct_total": round(expected, 1)}


def agent_loss_scale(outcomes, *, prior_fights=2.0, low=0.5, high=3.0):
    """How much more (or less) HP this agent loses per fight than the pooled expectation.

    ``outcomes`` carry the realized loss and the class mean that was expected for that fight
    (both % of max HP). The realized loss is ``hp_damage_pct`` (HP removed during the fight,
    the same quantity as the pooled ``damage_taken``); outcomes recorded without it fall back
    to the net ``hp_lost_pct``, which healing makes too small. Ratios are computed separately
    for hallway fights and for elites/bosses, each shrunk toward 1 with ``prior_fights``
    pseudo-fights at that group's average expectation and clipped to [low, high]. A group
    without finished fights stays at 1. The top-level ``value`` pools every fight and is
    reported for reference only; projections use ``by_group``.
    """
    rows = []
    for o in outcomes or []:
        realized, gross = o.get("hp_damage_pct"), True
        if realized is None:
            realized, gross = o.get("hp_lost_pct"), False
        if realized is None or not o.get("expected_pct"):
            continue
        group = SCALE_GROUPS.get(o.get("node_class")) or (
            "elite_boss" if o.get("encounter_type") in ("elite", "boss") else "hallway")
        rows.append((group, realized, o["expected_pct"], gross))
    groups = {g: _shrunk_ratio([(r, e) for grp, r, e, _ in rows if grp == g], prior_fights, low, high)
              for g in ("hallway", "elite_boss")}
    result = {**_shrunk_ratio([(r, e) for _, r, e, _ in rows], prior_fights, low, high), "by_group": groups,
              "applied": "by_group: hallway and ? nodes use hallway; elites and the boss margin use elite_boss. "
                         "The top-level value pools all fights and is not applied.",
              "measured_as": {"hp_removed_during_fight": sum(g for *_, g in rows),
                              "net_change_only_understates": sum(not g for *_, g in rows)}}
    result["basis"] = ("no finished fights with a pooled expectation yet" if not rows else
                       f"this run's HP removed per fight / pooled class mean, per group, shrunk toward 1 with "
                       f"{prior_fights:g} pseudo-fights and clipped to {low:g}..{high:g}")
    return result


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
    recovery = {c: [] for c in CLASSES}
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
                if prev is not None and prev["hp"] is not None and f["hp"]:   # survivors: hp_after > 0
                    # hp_after = hp_before - damage_taken + healed, so this is the HP regained on the floor
                    recovery[cls].append(max(0.0, (f["hp"] - prev["hp"] + f["damage_taken"]) / f["max_hp"] * 100))
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
        recovery["early_hallway"] = list(recovery["later_hallway"])
        deaths["early_hallway"] = deaths["later_hallway"]
    fights = [v for c in ("later_hallway", "elite") + (("early_hallway",) if early_k is not None else ())
              for v in recovery[c]]
    classes = {}
    for cls in CLASSES:
        values, n = samples[cls], len(samples[cls])
        # Too few of its own: a fight class borrows the pooled same-floor recovery of all fights.
        regained = recovery[cls] if len(recovery[cls]) >= 5 or cls == "unknown" else fights
        row = {"n": n, "mean_pct": round(sum(values) / n, 1) if n else None,
               "recovery_pct": round(sum(regained) / len(regained), 1) if regained else None,
               "recovery_n": len(regained),
               "_mean_own": round(sum(values) / n, 3) if n and sum(values) else None,
               "p90_pct": round(_q(values, 0.9), 1) if n >= min_samples else None,
               "death_rate_pct": round(deaths[cls] / n * 100, 2) if n else None,
               "source": "indexed_runs", "sparse": n < min_samples}
        alt = (fallback or {}).get(cls)
        if n < min_samples and alt and alt.get("n"):
            row.update({"mean_pct": alt["mean_pct"], "death_rate_pct": alt["death_rate_pct"],
                        "source": "encounter_stats", "fallback_n": alt["n"], "own_n": n})
        if row["mean_pct"] is None:
            row["source"] = "unavailable"
        row["_samples"] = sorted(values)   # private: used for sampling, removed by public_risk()
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
