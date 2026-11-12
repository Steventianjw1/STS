"""Layer C: enumerate visible map paths to the boss and project HP/gold along each.

Plain Python evidence, never a choice: dominated paths are dropped, and up to N
non-dominated paths are returned with labels saying what each is best at, plus at
least one path for every legal next node. No combined score is computed.
Projections sample layer-B fight losses (scaled by how this run's fights of the same
group compared with the pooled expectation), add back the pooled HP regained on the
same floor, and assume the current max HP; they ignore deck, relics, potions and
events' other effects.
"""
from __future__ import annotations

import hashlib
import json
import random
import re

from .elite_readiness import pool_gap_share
from .route_risk import SCALE_GROUPS, agent_loss_scale, loss_draws, node_class

FIGHTS = ("monster", "elite")
MEASURES = (  # (key, direction) used for dominance; None values are skipped consistently
    ("expected_boss_arrival_hp_pct", 1), ("p10_min_hp_pct", 1), ("death_risk_before_boss_pct", -1),
    ("death_risk_before_next_rest_pct", -1),
    ("elites", 1), ("fights", 1), ("rest_sites", 1), ("shops", 1), ("expert_profile_distance", -1))
SIMULATIONS = 200          # replays per path; fewer on very large maps (see plan_paths)


def map_type(value):
    """Mod map types (e.g. ``RestSite``) -> run-history names (``rest_site``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip()).lower().replace(" ", "_")
    return {"rest": "rest_site", "campfire": "rest_site", "merchant": "shop", "event": "unknown",
            "unknown_room": "unknown", "chest": "treasure"}.get(value, value)


def _key(point):
    if isinstance(point, dict):
        return (point.get("col"), point.get("row"))
    if isinstance(point, (list, tuple)) and len(point) == 2:
        return (point[0], point[1])
    return None


def enumerate_routes(details, *, max_routes):
    """Distinct node-type sequences from each next option to the boss.

    Returns ([(option, [(type, col, row), ...])], truncated, unmapped_types). Paths with
    identical type sequences after the same first node are merged (one representative).
    """
    nodes, children = {}, {}
    for n in details.get("nodes") or []:
        k = _key(n)
        if k and None not in k:
            nodes[k] = map_type(n.get("type"))
            children[k] = [c for c in (_key(c) for c in n.get("children") or []) if c and None not in c]
    bosses = [b for b in details.get("bosses") or [] if isinstance(b, dict)]
    boss_keys = {_key(b) for b in bosses}
    memo, truncated = {}, [False]

    def tails(k, depth=0):
        if k in memo:
            return memo[k]
        own = (nodes.get(k) or ("boss" if k in boss_keys else None), k[0], k[1])
        nxt = [c for c in children.get(k, []) if c in nodes or c in boss_keys]
        if not nxt or depth > 60:
            result = {(own[0],): [own]}
        else:
            result = {}
            for c in nxt:
                for seq, path in tails(c, depth + 1).items():
                    if len(result) >= max_routes:
                        truncated[0] = True
                        break
                    result.setdefault((own[0],) + seq, [own] + path)
        memo[k] = result
        return result

    routes = []
    for opt in details.get("next_options") or []:
        k = _key(opt)
        if not k or None in k or (opt.get("index") is None and not opt.get("preview")):
            continue
        nodes.setdefault(k, map_type(opt.get("type")))
        for path in tails(k).values():
            if path[-1][0] != "boss" and bosses:
                b = bosses[0]
                path = path + [("boss", b.get("col"), b.get("row"))]
            routes.append((opt, path))
    unmapped = sorted({t for _, p in routes for t, _, _ in p
                       if t not in ("monster", "elite", "boss", "unknown", "rest_site", "shop", "treasure")}, key=str)
    return routes, truncated[0], unmapped


def _band(hp_pct):
    return "below_50" if hp_pct < 50 else "50_to_75" if hp_pct < 75 else "75_plus"


def _heal_here(hp_pct, profile):
    """Simulated rest choice: HEAL when the experts' HEAL share in this arrival band is >= 50%."""
    band = _band(hp_pct)
    for group in ("wins", "all"):
        shares = (((profile or {}).get("rest_choices_by_arrival_hp") or {}).get(group) or {}).get(band) or {}
        if shares.get("choices"):
            return shares["share_pct"].get("HEAL", 0) >= 50, f"{group}:{band}"
    return True, "no_rest_data_assumed_heal"


def _quantile(values, p):
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def simulate(path, *, hp_pct, gold, risk, profile, hallway_done, draws=None, scale=1.0, uniforms=None):
    """Projection in % of current max HP; the boss fight itself is not subtracted.

    Two views of the same path. The mean trajectory subtracts each fight's class mean
    (times ``scale``) and is NOT clamped: a value at or below 0 means the average case
    already runs out of HP. The sampled view replays the path once per row of
    ``uniforms`` drawing each fight's loss from ``draws`` (observed losses), stops a
    replay when HP reaches 0, and reports how often that happens before the next rest
    site and before the boss. Without ``draws`` for a class its mean is used.

    ``scale`` is one number or ``{"hallway": x, "elite_boss": y}`` (see SCALE_GROUPS).
    A survived fight then regains the class's pooled same-floor ``recovery_pct``; the
    low point of a step is taken before that, the step's reported HP after it.
    """
    classes, early_k = risk["classes"], risk["early_hallway_fights"]
    heal = risk["rest_heal_pct"]["mean"]
    gold_by = risk["gold_change_by_node"]
    spend = risk["shop_spend"]["mean"]
    heals = {b: _heal_here(v, profile)[0] for b, v in (("below_50", 0), ("50_to_75", 60), ("75_plus", 90))}
    exp, g, ordinal = hp_pct, gold, hallway_done
    steps, shop_gold, rest_choice, plan, no_shape = [], [], [], [], set()
    min_exp = (hp_pct, 0)
    first_rest = next((i for i, (k, _, _) in enumerate(path, 1) if k == "rest_site"), None)
    for i, (kind, col, row) in enumerate(path, 1):
        if kind == "boss":
            steps.append({"step": i, "type": kind, "expected_hp_pct": round(exp, 1)})
            plan.append(("boss", None, 0.0, 1.0, 0.0))
            break
        if kind == "monster":
            ordinal += 1
        cls = node_class(kind, ordinal, early_k)
        if cls:
            factor = scale.get(SCALE_GROUPS.get(cls, "hallway"), 1.0) if isinstance(scale, dict) else scale
            mean = (classes[cls]["mean_pct"] or 0.0) * factor
            regain = classes[cls].get("recovery_pct") or 0.0
            d = (draws or {}).get(cls)
            if d is None:
                no_shape.add(cls)
            plan.append(("fight", d, mean, factor, regain))
            exp -= mean
            if exp < min_exp[0]:
                min_exp = (exp, i)
            if exp > 0:
                exp = min(100.0, exp + regain)
        elif kind == "rest_site":
            heal_exp, why = _heal_here(exp, profile)
            rest_choice.append({"step": i, "expected_path": "HEAL" if heal_exp else "SMITH", "basis": why})
            if heal is not None and heal_exp:
                exp = min(100.0, exp + heal)
            plan.append(("rest", None, 0.0, 1.0, 0.0))
        else:
            plan.append(("other", None, 0.0, 1.0, 0.0))
        if kind == "shop":
            shop_gold.append({"step": i, "estimated_arrival_gold": round(g)})
            g = max(0.0, g - (spend or 0))
        elif (gold_by.get(kind) or {}).get("mean") is not None:
            g = max(0.0, g + gold_by[kind]["mean"])
        if exp < min_exp[0]:
            min_exp = (exp, i)
        steps.append({"step": i, "type": kind, "expected_hp_pct": round(exp, 1), "estimated_gold": round(g)})
    mins, arrivals, rest_arrivals, dead, dead_before_rest = [], [], [], 0, 0
    for us in uniforms or []:
        hp = low = hp_pct
        died = False
        for i, (what, d, mean, factor, regain) in enumerate(plan, 1):
            if what == "fight":
                hp -= d[int(us[i - 1] * len(d))] * factor if d else mean
                low = min(low, hp)
                if hp <= 0:
                    died = True
                    dead += 1
                    dead_before_rest += first_rest is None or i < first_rest
                    break
                hp = min(100.0, hp + regain)
            elif what == "rest":
                if i == first_rest:
                    rest_arrivals.append(hp)
                if heal is not None and heals[_band(hp)]:
                    hp = min(100.0, hp + heal)
        mins.append(low)
        arrivals.append(hp)   # a replay that ran out of HP keeps its (non-positive) HP at that point
        if died and first_rest is not None and len(rest_arrivals) < len(mins):
            rest_arrivals.append(hp)
    n = len(mins)
    fights_before_rest = sum(step[0] == "fight" for step in (plan[:first_rest - 1] if first_rest else plan))
    return {"steps": steps, "boss_arrival": exp, "boss": classes["boss"], "min_exp": min_exp,
            "shop_gold": shop_gold, "rest_choice": rest_choice, "gold": g, "no_shape": no_shape,
            "first_rest": first_rest, "fights_before_rest": fights_before_rest,
            "elite_before_rest": any(k == "elite" for k, _, _ in (path[:first_rest - 1] if first_rest else path)),
            "expected_rest_arrival": (steps[first_rest - 2]["expected_hp_pct"] if first_rest and first_rest > 1
                                      else round(hp_pct, 1) if first_rest else None),
            "death_before_boss": dead / n * 100 if n else None,
            "death_before_rest": dead_before_rest / n * 100 if n else None,
            "p10_min": _quantile(mins, 0.1) if n else None,
            "p10_boss_arrival": _quantile(arrivals, 0.1) if n else None,
            "p10_rest_arrival": _quantile(rest_arrivals, 0.1) if rest_arrivals else None}


def profile_distance(counts, profile):
    """Sum over node types of |count - expert median| / max(IQR, 1), whole act (visited + path)."""
    for group in ("wins", "all"):
        medians = ((profile or {}).get("node_counts_per_act") or {}).get(group) or {}
        if medians:
            total = 0.0
            for t, q in medians.items():
                if q:
                    total += abs(counts.get(t, 0) - q["median"]) / max(q["p75"] - q["p25"], 1.0)
            return round(total, 2), group
    return None, None


def _dominates(a, b, keys):
    better = False
    for k, d in keys:
        if (a[k] - b[k]) * d < 0:
            return False
        better |= (a[k] - b[k]) * d > 0
    return better


def _elite_preparation(path):
    """Chances to fix the deck before each elite on a path: card-reward fights, ? nodes, shops, rests."""
    out, seen = [], {"fights": 0, "unknown": 0, "shops": 0, "rest_sites": 0}
    for i, (t, _, _) in enumerate(path, 1):
        if t == "elite":
            out.append({"step": i, **{k + "_before": v for k, v in seen.items()}})
        key = {"monster": "fights", "elite": "fights", "unknown": "unknown", "shop": "shops", "rest_site": "rest_sites"}.get(t)
        if key:
            seen[key] += 1
    return out


def _gap_summary(pool):
    return sorted({f"{g['need']} ({e['name']})" for e in pool["elites"] for g in e.get("gaps") or []})


def plan_paths(details, *, hp, max_hp, gold, risk, profile, candidates, max_routes, previous_nodes=None,
               elite_pool=None, outcomes=None):
    if not max_hp or hp is None:
        return {"status": "unavailable", "reason": "current_hp_unknown"}
    routes, truncated, unmapped = enumerate_routes(details, max_routes=max_routes)
    if not routes:
        return {"status": "unavailable", "reason": "no_enumerable_paths"}
    visited = [map_type(v.get("type")) for v in details.get("visited") or [] if isinstance(v, dict)]
    done = {}
    for t in visited:
        done[t] = done.get(t, 0) + 1
    hp_pct = hp / max_hp * 100
    scale = agent_loss_scale(outcomes)
    factors = {g: row["value"] for g, row in scale["by_group"].items()}
    draws = loss_draws(risk)
    sims = max(40, min(SIMULATIONS, 40000 // len(routes)))
    rng = random.Random(20261112)          # fixed: the same map and state always project the same numbers
    longest = max(len(path) for _, path in routes)
    uniforms = [[rng.random() for _ in range(longest)] for _ in range(sims)]   # shared by all paths
    rows = []
    for opt, path in routes:
        sim = simulate(path, hp_pct=hp_pct, gold=gold or 0, risk=risk, profile=profile,
                       hallway_done=done.get("monster", 0), draws=draws, scale=factors, uniforms=uniforms)
        counts = dict(done)
        for t, _, _ in path:
            counts[t] = counts.get(t, 0) + 1
        distance, basis = profile_distance(counts, profile)
        coords = [[c, r] for _, c, r in path]
        boss = sim["boss"]
        rows.append({
            "path_id": "p" + hashlib.sha256(json.dumps(coords).encode()).hexdigest()[:10],
            "first_action_id": f"map_{opt['index']}" if opt.get("index") is not None else None,
            "first_node": map_type(opt.get("type")), "first_coords": coords[0],
            "nodes": [t for t, _, _ in path], "coords": coords,
            "elite_steps": [i for i, (t, _, _) in enumerate(path, 1) if t == "elite"],
            "rest_steps": [i for i, (t, _, _) in enumerate(path, 1) if t == "rest_site"],
            "next_rest": {"step": sim["first_rest"], "fights_before": sim["fights_before_rest"],
                          "elite_before": sim["elite_before_rest"],
                          "expected_arrival_hp_pct": sim["expected_rest_arrival"],
                          "p10_arrival_hp_pct": _r(sim["p10_rest_arrival"]),
                          "death_risk_pct": _r(sim["death_before_rest"])},
            "fights_before_next_rest": sim["fights_before_rest"],
            "death_risk_before_next_rest_pct": _r(sim["death_before_rest"]),
            "shop_arrival_gold": sim["shop_gold"], "simulated_rest_choices": sim["rest_choice"],
            "expected_boss_arrival_hp_pct": round(sim["boss_arrival"], 1),
            "p10_boss_arrival_hp_pct": _r(sim["p10_boss_arrival"]),
            "expected_boss_margin_pct": None if boss["mean_pct"] is None else round(
                sim["boss_arrival"] - boss["mean_pct"] * factors["elite_boss"], 1),
            "expected_min_hp_pct": round(sim["min_exp"][0], 1), "expected_min_at_step": sim["min_exp"][1],
            "p10_min_hp_pct": _r(sim["p10_min"]),
            "death_risk_before_boss_pct": _r(sim["death_before_boss"]),
            "elites": sum(t == "elite" for t, _, _ in path), "fights": sum(t in FIGHTS for t, _, _ in path),
            "rest_sites": sum(t == "rest_site" for t, _, _ in path), "shops": sum(t == "shop" for t, _, _ in path),
            "expert_profile_distance": distance if distance is not None else 0.0,
            "elite_preparation": _elite_preparation(path),
            "mean_used_instead_of_samples_for": sorted(sim["no_shape"]),
            "trajectory": sim["steps"],
        })
    keys = [(k, d) for k, d in MEASURES if all(r[k] is not None for r in rows)]
    front = [r for r in rows if not any(_dominates(o, r, keys) for o in rows if o is not r)]
    picks = []

    def take(label, pool, key):
        if pool:
            best = max(pool, key=key)
            best.setdefault("selected_for", []).append(label)
            if best not in picks:
                picks.append(best)
    alive = [r for r in front if r["expected_min_hp_pct"] > 0]
    previous = None
    if previous_nodes:
        previous = next((r for r in rows if r["coords"] == previous_nodes), None)
        if previous:
            previous.setdefault("selected_for", []).append("continues_previous_plan")
            picks.append(previous)
    take("closest_to_expert_profile", front, lambda r: (-r["expert_profile_distance"], -r["death_risk_before_boss_pct"]))
    take("lowest_death_risk_before_boss", front, lambda r: (-r["death_risk_before_boss_pct"], r["p10_min_hp_pct"]))
    take("safest_to_next_rest", front, lambda r: (-r["death_risk_before_next_rest_pct"], -r["fights_before_next_rest"],
                                                  r["p10_min_hp_pct"]))
    take("most_elites_expected_alive", alive, lambda r: (r["elites"], -r["death_risk_before_boss_pct"]))
    take("best_expected_boss_arrival_hp", front, lambda r: (r["expected_boss_arrival_hp_pct"], -r["death_risk_before_boss_pct"]))
    take("most_fights_expected_alive", alive, lambda r: (r["fights"], -r["death_risk_before_boss_pct"]))
    gap_share = pool_gap_share(elite_pool) if elite_pool else None
    if gap_share:  # the deck lacks something some act elites demand: show the elite-light and best-prepared routes
        take("fewest_elites_given_deck_gaps", front, lambda r: (-r["elites"], -r["death_risk_before_boss_pct"]))
        prep = [r for r in alive if r["elite_preparation"]]

        def first_prep(r):
            e = r["elite_preparation"][0]
            return (e["fights_before"] + e["shops_before"] + e["unknown_before"], -r["death_risk_before_boss_pct"])
        take("most_preparation_before_first_elite", prep, first_prep)
    shown = picks[:candidates]
    if candidates:  # every legal next node gets at least one detailed path, so openings can be compared
        for first in dict.fromkeys(r["first_action_id"] or str(r["first_coords"]) for r in rows):
            if not any((r["first_action_id"] or str(r["first_coords"])) == first for r in shown):
                pool = [r for r in rows if (r["first_action_id"] or str(r["first_coords"])) == first]
                best = max(pool, key=lambda r: (-r["death_risk_before_boss_pct"], r["p10_min_hp_pct"]))
                best.setdefault("selected_for", []).append("lowest_death_risk_for_this_next_node")
                shown.append(best)
    for r in shown:
        r["dominated"] = r not in front
    options = {}
    for r in rows:
        o = options.setdefault(r["first_action_id"] or str(r["first_coords"]), {
            "action_id": r["first_action_id"], "coords": r["first_coords"], "node": r["first_node"],
            "distinct_routes": 0, "non_dominated": 0, "max_elites": 0, "routes_expected_alive": 0,
            "lowest_death_risk_before_boss_pct": None, "lowest_death_risk_before_next_rest_pct": None,
            "fewest_fights_before_next_rest": None, "best_p10_min_hp_pct": None,
            "min_expert_profile_distance": None})
        o["distinct_routes"] += 1
        o["non_dominated"] += r in front
        o["routes_expected_alive"] += r["expected_min_hp_pct"] > 0
        o["max_elites"] = max(o["max_elites"], r["elites"])
        for key, source, pick in (("lowest_death_risk_before_boss_pct", "death_risk_before_boss_pct", min),
                                  ("lowest_death_risk_before_next_rest_pct", "death_risk_before_next_rest_pct", min),
                                  ("fewest_fights_before_next_rest", "fights_before_next_rest", min),
                                  ("best_p10_min_hp_pct", "p10_min_hp_pct", max),
                                  ("min_expert_profile_distance", "expert_profile_distance", min)):
            if r[source] is not None:
                o[key] = r[source] if o[key] is None else pick(o[key], r[source])
    return {
        "status": "available", "start": {"hp_pct": round(hp_pct, 1), "gold": gold, "max_hp": max_hp,
                                         "act_visited_counts": done},
        "distinct_routes": len(rows), "non_dominated": len(front), "truncated": truncated,
        "unmodeled_node_types": unmapped, "dominance_measures": [k for k, _ in keys],
        "profile_distance_basis": basis if rows else None,
        "simulation": {"replays_per_path": sims, "agent_loss_scale": scale,
                       "loss_source": "observed per-class HP removed per fight (hp_risk), resampled and scaled by "
                                      "the fight's group; a survived fight then regains that class's recovery_pct",
                       "negative_values": "HP below 0 is kept: it says how far short the path falls"},
        "candidates": shown, "by_next_option": list(options.values()),
        "outlook": _outlook(rows), "route_ranges": _ranges(rows),
        "current_act_elite_gap_share": gap_share,
        "current_act_elite_gaps": _gap_summary(elite_pool) if elite_pool and gap_share else [],
    }


def _r(value):
    return None if value is None else round(value, 1)


def _outlook(rows):
    """Whole-map view over every distinct route (not only the shown candidates)."""
    alive = [r for r in rows if r["expected_min_hp_pct"] > 0]
    shops = [r["shop_arrival_gold"][0]["estimated_arrival_gold"] for r in rows if r["shop_arrival_gold"]]
    return {"lowest_death_risk_before_boss_pct": min(r["death_risk_before_boss_pct"] for r in rows),
            "lowest_death_risk_before_next_rest_pct": min(r["death_risk_before_next_rest_pct"] for r in rows),
            "best_p10_min_hp_pct": max(r["p10_min_hp_pct"] for r in rows),
            "best_expected_boss_arrival_hp_pct": max(r["expected_boss_arrival_hp_pct"] for r in rows),
            "max_elites_expected_alive": max((r["elites"] for r in alive), default=None),
            "routes_expected_alive_pct": round(len(alive) / len(rows) * 100, 1),
            "routes_death_risk_under_25_pct": round(sum(r["death_risk_before_boss_pct"] < 25 for r in rows)
                                                    / len(rows) * 100, 1),
            "best_first_shop_arrival_gold": max(shops, default=None)}


def _ranges(rows):
    def span(values):
        values = [v for v in values if v is not None]
        return {"min": min(values), "max": max(values)} if values else None
    first = [r["elite_preparation"][0] for r in rows if r["elite_preparation"]]
    return {"elites": span(r["elites"] for r in rows), "rest_sites": span(r["rest_sites"] for r in rows),
            "shops": span(r["shops"] for r in rows), "fights": span(r["fights"] for r in rows),
            "fights_before_next_rest": span(r["fights_before_next_rest"] for r in rows),
            "first_elite_step": span(e["step"] for e in first),
            "fights_before_first_elite": span(e["fights_before"] for e in first),
            "routes_without_elites_pct": round(sum(not r["elites"] for r in rows) / len(rows) * 100, 1)}


# ------------------------------------------------------------------ re-planning
def plan_status(plan, *, act, details, hp, max_hp, gold, relics, potions, outcomes, risk, hp_tolerance, gold_tolerance,
                elite_pool=None, elite_steps=3):
    """Python-detected reasons to revisit a stored route plan. Never changes the plan."""
    if not plan:
        return {"status": "no_previous_plan", "triggers": ["no_plan_yet"], "replan_suggested": True}
    triggers, at = [], None
    if plan["act"] != act:
        triggers.append("new_act")
    here = _key(details.get("current_position"))
    coords = [tuple(c) for c in plan.get("coords") or []]
    if coords and plan["act"] == act:
        if here in coords:
            at = plan["trajectory"][coords.index(here)]
        elif here is not None and here != tuple(plan.get("start") or ()):
            triggers.append("left_planned_path")
    if at and max_hp and hp is not None:
        now = hp / max_hp * 100
        if abs(now - at["expected_hp_pct"]) > hp_tolerance:
            triggers.append(f"hp_off_projection: now {now:.0f}% vs expected {at['expected_hp_pct']:.0f}% at this node")
    elif plan.get("hp_pct") is not None and max_hp and hp is not None and not coords:
        if abs(hp / max_hp * 100 - plan["hp_pct"]) > hp_tolerance:
            triggers.append("hp_changed_since_plan")
    for o in outcomes[plan["outcomes_seen"]:]:
        cls = o.get("node_class") or {"elite": "elite", "boss": "boss"}.get(o.get("encounter_type"), "later_hallway")
        p90 = ((risk or {}).get("classes") or {}).get(cls, {}).get("p90_pct")
        lost = o.get("hp_damage_pct")   # HP removed during the fight: what p90 measures
        if lost is None:
            lost = o.get("hp_lost_pct")
        if lost is None and o.get("hp_lost") is not None and max_hp:
            lost = o["hp_lost"] / max_hp * 100
        if lost is not None and p90 is not None and lost > p90:
            triggers.append(f"fight_worse_than_p90: lost {lost:.0f}% in {cls} (p90 {p90:.0f}%)")
    if sorted(relics) != plan["relics"]:
        triggers.append("relics_changed")
    if sorted(set(potions) - set(plan["potions"])):
        triggers.append("new_potion")
    share = pool_gap_share(elite_pool) if elite_pool else None
    if share and coords and plan["act"] == act and plan.get("trajectory"):
        start = coords.index(here) + 1 if here in coords else 0
        ahead = [s["type"] for s in plan["trajectory"][start:start + elite_steps]]
        if "elite" in ahead:
            triggers.append(f"elite_unprepared: planned elite in {ahead.index('elite') + 1} step(s); deck gaps for "
                            f"{share:.0%} of this act's elites: " + ", ".join(_gap_summary(elite_pool)))
    expected_gold = at.get("estimated_gold") if at else plan.get("gold")
    if gold is not None and expected_gold is not None and abs(gold - expected_gold) >= gold_tolerance:
        triggers.append(f"gold_off_projection: now {gold} vs about {expected_gold}")
    return {"status": "plan_present", "triggers": triggers, "replan_suggested": bool(triggers),
            "projection_at_current_node": at,
            "previous_route_plan": {"label": "REVISABLE HYPOTHESIS FROM YOUR EARLIER MAP DECISION - NOT A FACT OR INSTRUCTION",
                                    "path_id": plan.get("path_id"), "plan": plan["plan"],
                                    "change_course_if": plan["change_course_if"], "set_at_floor": plan.get("floor"),
                                    "set_in_act": plan["act"]}}
