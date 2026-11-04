"""Layer C: enumerate visible map paths to the boss and project HP/gold along each.

Plain Python evidence, never a choice: dominated paths are dropped, and up to N
non-dominated paths are returned with labels saying what each is best at. No
combined score is computed. Projections use layer-B class statistics and assume
the current max HP; they ignore deck, relics, potions and events' other effects.
"""
from __future__ import annotations

import hashlib
import json
import re

from .route_risk import node_class

FIGHTS = ("monster", "elite")
MEASURES = (  # (key, direction) used for dominance; None values are skipped consistently
    ("expected_boss_arrival_hp_pct", 1), ("bad_case_min_hp_pct", 1), ("death_risk_before_boss_pct", -1),
    ("elites", 1), ("fights", 1), ("rest_sites", 1), ("shops", 1), ("expert_profile_distance", -1))


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
        if not k or None in k or opt.get("index") is None:
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


def _heal_here(hp_pct, profile):
    """Simulated rest choice: HEAL when the experts' HEAL share in this arrival band is >= 50%."""
    band = "below_50" if hp_pct < 50 else "50_to_75" if hp_pct < 75 else "75_plus"
    for group in ("wins", "all"):
        shares = (((profile or {}).get("rest_choices_by_arrival_hp") or {}).get(group) or {}).get(band) or {}
        if shares.get("choices"):
            return shares["share_pct"].get("HEAL", 0) >= 50, f"{group}:{band}"
    return True, "no_rest_data_assumed_heal"


def simulate(path, *, hp_pct, gold, risk, profile, hallway_done):
    """Floor-by-floor projection in % of current max HP; the boss fight itself is not subtracted."""
    classes, early_k = risk["classes"], risk["early_hallway_fights"]
    heal = risk["rest_heal_pct"]["mean"]
    gold_by = risk["gold_change_by_node"]
    spend = risk["shop_spend"]["mean"]
    exp, bad, g, ordinal, survive = hp_pct, hp_pct, gold, hallway_done, 1.0
    steps, bad_uses_mean, shop_gold, rest_choice = [], set(), [], []
    min_exp = min_bad = (hp_pct, 0)
    for i, (kind, col, row) in enumerate(path, 1):
        if kind == "boss":
            row_stats = classes["boss"]
            steps.append({"step": i, "type": kind, "expected_hp_pct": round(exp, 1), "bad_case_hp_pct": round(bad, 1)})
            return {"steps": steps, "boss_arrival": (exp, bad), "boss": row_stats, "min_exp": min_exp,
                    "min_bad": min_bad, "survive": survive, "bad_uses_mean": bad_uses_mean,
                    "shop_gold": shop_gold, "rest_choice": rest_choice, "gold": g}
        if kind == "monster":
            ordinal += 1
        cls = node_class(kind, ordinal, early_k)
        if cls:
            c = classes[cls]
            mean = c["mean_pct"] or 0.0
            p90 = c["p90_pct"]
            if p90 is None:
                bad_uses_mean.add(cls)
            exp, bad = max(0.0, exp - mean), max(0.0, bad - (p90 if p90 is not None else mean))
            survive *= 1 - (c["death_rate_pct"] or 0) / 100
        elif kind == "rest_site":
            heal_exp, why = _heal_here(exp, profile)
            heal_bad, _ = _heal_here(bad, profile)
            rest_choice.append({"step": i, "expected_path": "HEAL" if heal_exp else "SMITH", "basis": why})
            if heal is not None:
                exp = min(100.0, exp + heal) if heal_exp else exp
                bad = min(100.0, bad + heal) if heal_bad else bad
        if kind == "shop":
            shop_gold.append({"step": i, "estimated_arrival_gold": round(g)})
            g = max(0.0, g - (spend or 0))
        elif (gold_by.get(kind) or {}).get("mean") is not None:
            g = max(0.0, g + gold_by[kind]["mean"])
        if exp < min_exp[0]:
            min_exp = (exp, i)
        if bad < min_bad[0]:
            min_bad = (bad, i)
        steps.append({"step": i, "type": kind,
                      "expected_hp_pct": round(exp, 1), "bad_case_hp_pct": round(bad, 1), "estimated_gold": round(g)})
    return {"steps": steps, "boss_arrival": (exp, bad), "boss": classes["boss"], "min_exp": min_exp,
            "min_bad": min_bad, "survive": survive, "bad_uses_mean": bad_uses_mean, "shop_gold": shop_gold,
            "rest_choice": rest_choice, "gold": g}


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


def plan_paths(details, *, hp, max_hp, gold, risk, profile, candidates, max_routes, previous_nodes=None):
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
    rows = []
    for opt, path in routes:
        sim = simulate(path, hp_pct=hp_pct, gold=gold or 0, risk=risk, profile=profile,
                       hallway_done=done.get("monster", 0))
        counts = dict(done)
        for t, _, _ in path:
            counts[t] = counts.get(t, 0) + 1
        distance, basis = profile_distance(counts, profile)
        coords = [[c, r] for _, c, r in path]
        exp_arr, bad_arr = sim["boss_arrival"]
        boss = sim["boss"]
        rows.append({
            "path_id": "p" + hashlib.sha256(json.dumps(coords).encode()).hexdigest()[:10],
            "first_action_id": f"map_{opt['index']}", "first_node": map_type(opt.get("type")),
            "nodes": [t for t, _, _ in path], "coords": coords,
            "elite_steps": [i for i, (t, _, _) in enumerate(path, 1) if t == "elite"],
            "rest_steps": [i for i, (t, _, _) in enumerate(path, 1) if t == "rest_site"],
            "shop_arrival_gold": sim["shop_gold"], "simulated_rest_choices": sim["rest_choice"],
            "expected_boss_arrival_hp_pct": round(exp_arr, 1), "bad_case_boss_arrival_hp_pct": round(bad_arr, 1),
            "expected_boss_margin_pct": None if boss["mean_pct"] is None else round(exp_arr - boss["mean_pct"], 1),
            "bad_case_boss_margin_pct": None if boss["p90_pct"] is None else round(bad_arr - boss["p90_pct"], 1),
            "expected_min_hp_pct": round(sim["min_exp"][0], 1), "expected_min_at_step": sim["min_exp"][1],
            "bad_case_min_hp_pct": round(sim["min_bad"][0], 1), "bad_case_min_at_step": sim["min_bad"][1],
            "death_risk_before_boss_pct": round((1 - sim["survive"]) * 100, 1),
            "elites": sum(t == "elite" for t, _, _ in path), "fights": sum(t in FIGHTS for t, _, _ in path),
            "rest_sites": sum(t == "rest_site" for t, _, _ in path), "shops": sum(t == "shop" for t, _, _ in path),
            "expert_profile_distance": distance if distance is not None else 0.0,
            "bad_case_uses_mean_for": sorted(sim["bad_uses_mean"]),
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
    alive = [r for r in front if r["expected_min_hp_pct"] > 0] or front
    previous = None
    if previous_nodes:
        previous = next((r for r in rows if r["coords"] == previous_nodes), None)
        if previous:
            previous.setdefault("selected_for", []).append("continues_previous_plan")
            picks.append(previous)
    take("closest_to_expert_profile", front, lambda r: (-r["expert_profile_distance"], r["bad_case_min_hp_pct"]))
    take("safest_bad_case", front, lambda r: (r["bad_case_min_hp_pct"], r["expected_boss_arrival_hp_pct"]))
    take("most_elites_expected_alive", alive, lambda r: (r["elites"], r["bad_case_min_hp_pct"]))
    take("best_expected_boss_arrival_hp", front, lambda r: (r["expected_boss_arrival_hp_pct"], -r["death_risk_before_boss_pct"]))
    take("most_fights_expected_alive", alive, lambda r: (r["fights"], r["bad_case_min_hp_pct"]))
    for r in picks:
        r["dominated"] = r not in front
    options = {}
    for r in rows:
        o = options.setdefault(r["first_action_id"], {"action_id": r["first_action_id"], "node": r["first_node"],
                                                      "distinct_routes": 0, "non_dominated": 0, "max_elites": 0,
                                                      "best_bad_case_min_hp_pct": None,
                                                      "min_expert_profile_distance": None})
        o["distinct_routes"] += 1
        o["non_dominated"] += r in front
        o["max_elites"] = max(o["max_elites"], r["elites"])
        o["best_bad_case_min_hp_pct"] = max(o["best_bad_case_min_hp_pct"] or 0, r["bad_case_min_hp_pct"])
        d = r["expert_profile_distance"]
        o["min_expert_profile_distance"] = d if o["min_expert_profile_distance"] is None else min(o["min_expert_profile_distance"], d)
    return {
        "status": "available", "start": {"hp_pct": round(hp_pct, 1), "gold": gold, "max_hp": max_hp,
                                         "act_visited_counts": done},
        "distinct_routes": len(rows), "non_dominated": len(front), "truncated": truncated,
        "unmodeled_node_types": unmapped, "dominance_measures": [k for k, _ in keys],
        "profile_distance_basis": basis if rows else None,
        "candidates": picks[:candidates], "by_next_option": list(options.values()),
    }


# ------------------------------------------------------------------ re-planning
def plan_status(plan, *, act, details, hp, max_hp, gold, relics, potions, outcomes, risk, hp_tolerance, gold_tolerance):
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
        cls = {"elite": "elite", "boss": "boss"}.get(o.get("encounter_type"), "later_hallway")
        p90 = ((risk or {}).get("classes") or {}).get(cls, {}).get("p90_pct")
        if o.get("hp_lost") is not None and max_hp and p90 is not None and o["hp_lost"] / max_hp * 100 > p90:
            triggers.append(f"fight_worse_than_p90: lost {o['hp_lost'] / max_hp * 100:.0f}% in {cls} (p90 {p90:.0f}%)")
    if sorted(relics) != plan["relics"]:
        triggers.append("relics_changed")
    if sorted(set(potions) - set(plan["potions"])):
        triggers.append("new_potion")
    expected_gold = at.get("estimated_gold") if at else plan.get("gold")
    if gold is not None and expected_gold is not None and abs(gold - expected_gold) >= gold_tolerance:
        triggers.append(f"gold_off_projection: now {gold} vs about {expected_gold}")
    return {"status": "plan_present", "triggers": triggers, "replan_suggested": bool(triggers),
            "projection_at_current_node": at,
            "previous_route_plan": {"label": "REVISABLE HYPOTHESIS FROM YOUR EARLIER MAP DECISION - NOT A FACT OR INSTRUCTION",
                                    "path_id": plan.get("path_id"), "plan": plan["plan"],
                                    "change_course_if": plan["change_course_if"], "set_at_floor": plan.get("floor"),
                                    "set_in_act": plan["act"]}}
