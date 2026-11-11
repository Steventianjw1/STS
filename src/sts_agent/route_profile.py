"""Act route profile from indexed expert runs: what selected players did, wins next to all.

Read-only, observational and descriptive. It never ranks map nodes or acts.
"""
from __future__ import annotations

import json
from collections import Counter

HP_BANDS = (("below_50", 0, 50), ("50_to_75", 50, 75), ("75_plus", 75, float("inf")))
CHECKPOINT_FLOORS = (5, 10)
NON_CHOICE_TYPES = {"ancient", "boss"}   # not a route choice when finding the first node
TOP_SEQUENCES = 5


def _quantiles(values):
    """Median and interquartile range (linear interpolation); None when empty."""
    values = sorted(values)
    if not values:
        return None

    def q(p):
        k = (len(values) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)
    return {"n": len(values), "median": q(0.5), "p25": q(0.25), "p75": q(0.75)}


def _shares(counter, total):
    return {k: round(v / total * 100, 1) for k, v in counter.most_common()} if total else {}


def _pct(hp, max_hp):
    return hp / max_hp * 100 if hp is not None and max_hp else None


def _act_view(run, act_id):
    """Floors of this act plus each floor's arrival HP%/gold (the previous recorded floor)."""
    rows, prev = [], None
    for f in run["floors"]:
        if f["act_id"] == act_id:
            rows.append({**f, "arrival_hp_pct": _pct(prev["hp"], prev["max_hp"]) if prev else None,
                         "arrival_gold": prev["gold"] if prev else None})
        prev = f
    return rows


def _group(views):
    """Statistics for one group of runs (each item: this act's floor rows)."""
    entered = len(views)
    finished = [v for v in views if any(f["map_point_type"] == "boss" for f in v)]
    result = {"runs_entering_act": entered, "runs_reaching_boss": len(finished)}

    # First chosen node and opening sequence (runs that entered the act).
    first, seqs = Counter(), Counter()
    for v in views:
        types = [f["map_point_type"] for f in v if f["map_point_type"] and f["map_point_type"] not in NON_CHOICE_TYPES]
        if types:
            first[types[0]] += 1
        if len(types) >= 3:
            seqs[tuple(types[:3])] += 1
    result["first_node"] = {"n": sum(first.values()), "share_pct": _shares(first, sum(first.values()))}
    total_seq = sum(seqs.values())
    result["opening_three_nodes"] = {"n": total_seq, "top": [
        {"sequence": list(k), "n": n, "pct": round(n / total_seq * 100, 1)} for k, n in seqs.most_common(TOP_SEQUENCES)]}

    # Per-act node counts, over runs that reached the boss (complete act routes).
    types = sorted({f["map_point_type"] for v in finished for f in v
                    if f["map_point_type"] and f["map_point_type"] not in NON_CHOICE_TYPES})
    result["node_counts_per_act"] = {t: _quantiles([sum(f["map_point_type"] == t for f in v) for v in finished])
                                     for t in types}
    resolved = Counter(f["room_type"] or "unrecorded" for v in finished for f in v if f["map_point_type"] == "unknown")
    result["unknown_node_resolved_as"] = {"n": sum(resolved.values()),
                                          "share_pct": _shares(resolved, sum(resolved.values()))}

    # First elite and hallway fights before it (complete act routes).
    elite_floor, hallway, no_elite = [], [], 0
    for v in finished:
        idx = next((i for i, f in enumerate(v) if f["map_point_type"] == "elite"), None)
        if idx is None:
            no_elite += 1
            continue
        elite_floor.append(v[idx]["act_floor"])
        hallway.append(sum(f["room_type"] == "monster" for f in v[:idx]))
    result["first_elite"] = {"no_elite_pct": round(no_elite / len(finished) * 100, 1) if finished else None,
                             "act_floor": _quantiles(elite_floor), "hallway_fights_before": _quantiles(hallway)}

    # Shops: first visit floor (complete act routes) and gold on arrival (every shop visit).
    first_shop, no_shop, gold = [], 0, []
    for v in finished:
        visits = [f for f in v if f["room_type"] == "shop"]
        if visits:
            first_shop.append(visits[0]["act_floor"])
        else:
            no_shop += 1
    for v in views:
        gold += [f["arrival_gold"] for f in v if f["room_type"] == "shop" and f["arrival_gold"] is not None]
    result["shops"] = {"no_shop_pct": round(no_shop / len(finished) * 100, 1) if finished else None,
                       "first_shop_act_floor": _quantiles(first_shop), "arrival_gold_per_visit": _quantiles(gold)}

    # Rest-site choices by HP% on arrival (every recorded choice).
    bands = {name: Counter() for name, _, _ in HP_BANDS}
    unbanded = 0
    for v in views:
        for f in v:
            if not f["rest_choices"]:
                continue
            hp = f["arrival_hp_pct"]
            name = next((n for n, lo, hi in HP_BANDS if hp is not None and lo <= hp < hi), None)
            if name is None:
                unbanded += 1
                continue
            bands[name].update(f["rest_choices"])
    result["rest_choices_by_arrival_hp"] = {
        name: {"choices": sum(c.values()), "share_pct": _shares(c, sum(c.values()))} for name, c in bands.items()}
    result["rest_choices_by_arrival_hp"]["visits_with_unknown_hp"] = unbanded

    # HP% checkpoints (every run present at the checkpoint).
    checks = {}
    for n in CHECKPOINT_FLOORS:
        checks[f"after_act_floor_{n}"] = _quantiles(
            [p for v in views for f in v if f["act_floor"] == n for p in [_pct(f["hp"], f["max_hp"])] if p is not None])
    boss = [next(f["arrival_hp_pct"] for f in v if f["map_point_type"] == "boss") for v in finished]
    checks["boss_arrival"] = _quantiles([p for p in boss if p is not None])
    result["hp_pct_checkpoints"] = checks
    return result


def build_route_profile(runs, act_id, *, min_samples):
    """``runs``: RunHistoryIndex.route_rows output. Returns {"all", "wins", ...} per statistic group."""
    kept = [r for r in runs if not r["abandoned"]]
    views = {r["hash"]: _act_view(r, act_id) for r in kept}
    all_views = [views[r["hash"]] for r in kept]
    win_views = [views[r["hash"]] for r in kept if r["win"] == 1]
    entered = len(all_views)
    # A loss whose last recorded floor is in this act (including its boss).
    died = sum(1 for r in kept if r["win"] == 0 and r["floors"][-1]["act_id"] == act_id)
    groups = {"all": _group(all_views), "wins": _group(win_views)}
    sections = [k for k in groups["all"] if k not in ("runs_entering_act", "runs_reaching_boss")]
    return {
        "samples": {"runs_entering_act": {"all": entered, "wins": len(win_views)},
                    "runs_reaching_boss": {g: groups[g]["runs_reaching_boss"] for g in groups},
                    "lost_in_this_act_pct": round(died / entered * 100, 1) if entered else None,
                    "excluded_abandoned_runs": len(runs) - len(kept),
                    "unknown_outcome_runs": sum(r["win"] is None for r in kept),
                    "sparse": len(win_views) < min_samples},
        **{s: {"all": groups["all"][s], "wins": groups["wins"][s]} for s in sections},
        "contributing_players": len({r["username"] for r in kept}),
        "route_issues": sorted({i for r in kept for i in json.loads(r["issues"])}),
    }
