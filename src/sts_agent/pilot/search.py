"""One combat decision: a search, then a race between the options it cannot tell apart.

The engine's own root pick treats every option within a fixed value band of the best
(0.02, about 6 HP of 80) as equal and lets hand-written rules choose among them: play
the free card, spend the energy, take the most visited. The pilot spends search instead:
each close option gets independent searches with every simulation forced through it,
and the best average wins. A decision the search already resolves costs nothing extra.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .profiles import Profile

CORES = os.cpu_count() or 1
EXACT_RULES = {"lost_position", "illegal_fallback"}   # engine picks that are not a judgement between values
_pool: ThreadPoolExecutor | None = None


@dataclass
class Decision:
    action: dict                 # the engine-dialect action to apply
    rule: str                    # "engine:<its rule>", "race" (the race changed the pick) or "race=engine"
    rows: list = field(default_factory=list)       # the main search's per-option rows
    raced: list = field(default_factory=list)      # [{"action", "score", "visits"}] when a race ran
    sims: int = 0                # simulations spent
    seconds: float = 0.0
    p_win: float | None = None
    hp: float | None = None      # expected HP at the end of the fight through the chosen option
    result: dict | None = None   # the engine's result with the pilot's pick crowned (see ``crown``)


def c_puct_for(sims, threads) -> float:
    """The engine authors' exploration constant for a per-tree budget (their measured rule)."""
    per_tree = sims // max(1, threads)
    return 0.8 if per_tree < 500 else 1.41 if per_tree < 1000 else 2.5


def _knobs(profile: Profile, carry=(None, None)) -> dict:
    """The search settings both the main search and the re-searches share.

    ``carry`` is the caller's (HP, potion) win worth for this fight (Mentor sets one on the
    run's last boss, where nothing carries forward); a profile that sets its own wins.
    """
    out = {"hidden_info": "beliefdraw", "dominance_pruning": profile.dominance_pruning,
           "draw_widening_c": 2.0 if profile.draw_widening_c is None else profile.draw_widening_c}
    for name in ("endturn_widening_c", "endturn_widening_alpha", "rollout_policy"):
        if getattr(profile, name) is not None:
            out[name] = getattr(profile, name)
    for name, given in zip(("win_hp_carry", "win_potion_carry"), carry):
        value = getattr(profile, name) if getattr(profile, name) is not None else given
        if value is not None:
            out[name] = value
    return out


def crown(result: dict, action: dict, rule: str, reason: str) -> None:
    """Make ``action`` the pick of an engine result, in the fields its consumers read."""
    rows = result.get("actions") or []
    chosen = next((r for r in rows if r["action"] == action), None)
    if chosen is None or chosen.get("is_best"):
        return
    was = next((r for r in rows if r.get("is_best")), None)
    rest = sorted((r for r in rows if r is not chosen), key=lambda r: r.get("pick_rank") if r.get("pick_rank") is not None else 1 << 30)
    for rank, row in enumerate([chosen] + rest):
        row["is_best"], row["pick_rank"] = rank == 0, rank
    result["pick"] = {"rule": rule, "reason": reason,
                      "gap_q": (chosen["q"] - was["q"]) if was else None, "gap_z": None,
                      "gap_hp": (chosen["mean_final_hp"] - was["mean_final_hp"])
                      if was and chosen.get("mean_final_hp") is not None and was.get("mean_final_hp") is not None else None}
    if (result.get("pv") or [None])[0] != action:
        result["pv"] = [action]


def _workers() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=CORES, thread_name_prefix="pilot-race")
    return _pool


def decide(rs, state, profile: Profile, seed=0, carry=(None, None)) -> Decision:
    """Choose the action to play in ``state`` (an engine ``CombatState`` with a decision open)."""
    started = time.perf_counter()
    threads = max(1, min(profile.threads, CORES))
    knobs = _knobs(profile, carry)
    main = dict(knobs, sims=profile.sims, threads=threads, seed=seed,
                c_puct=profile.c_puct if profile.c_puct is not None else c_puct_for(profile.sims, threads))
    if profile.leaf_playouts is not None:
        main["leaf_playouts"] = profile.leaf_playouts
    result = rs.mcts_recommend(state, **main)
    rows = result.get("actions") or []
    if not rows:
        raise ValueError("the search returned no action for this state")
    best = next((r for r in rows if r.get("is_best")), rows[0])
    engine_rule = (result.get("pick") or {}).get("rule") or "argmax"
    out = Decision(action=best["action"], rule=f"engine:{engine_rule}", rows=rows, sims=profile.sims,
                   p_win=best.get("p_win"), hp=best.get("mean_final_hp"), result=result)
    if profile.pick == "race" and len(rows) > 1 and engine_rule not in EXACT_RULES:
        _race(rs, state, profile, knobs, seed, rows, best, out)
        if out.rule == "race":
            crown(result, out.action, "pilot_race", "the best of the close moves after searching each on its own")
    out.seconds = time.perf_counter() - started
    return out


def _race(rs, state, profile, knobs, seed, rows, best, out: Decision) -> None:
    top = max(r["q"] for r in rows)
    close = sorted((r for r in rows if r["q"] >= top - profile.race_band), key=lambda r: -r["q"])[:profile.race_width]
    if all(r is not best for r in close):
        close.append(best)       # the engine's pick always runs, so a rule-made pick is tested, not assumed
    if len(close) < 2:
        out.action, out.rule = close[0]["action"], "race=engine" if close[0] is best else "race"
        out.p_win, out.hp = close[0].get("p_win"), close[0].get("mean_final_hp")
        return
    forced = dict(knobs, sims=profile.race_sims,
                  c_puct=profile.c_puct if profile.c_puct is not None else c_puct_for(profile.race_sims, 1))
    jobs = [(i, _workers().submit(rs.mcts_score_action_forced, state, row["action"],
                                  seed=(seed * 1000003 + i * 101 + rep + 1) & 0x7FFFFFFF, **forced))
            for i, row in enumerate(close) for rep in range(max(1, profile.race_reps))]
    value = [row["q"] * row["visits"] for row in close]
    visits = [row["visits"] for row in close]
    hp_sum = [0.0] * len(close)
    hp_n = [0] * len(close)
    for i, job in jobs:
        extra = job.result()
        if extra:
            value[i] += extra["q"] * extra["visits"]
            visits[i] += extra["visits"]
            if extra.get("mean_final_hp") is not None:
                hp_sum[i] += extra["mean_final_hp"] * (extra.get("hp_n") or 0)
                hp_n[i] += extra.get("hp_n") or 0
    score = [v / n if n else float("-inf") for v, n in zip(value, visits)]
    win = max(range(len(close)), key=lambda i: (score[i], close[i] is best))
    out.raced = [{"action": row["action"], "score": round(score[i], 4), "visits": visits[i]} for i, row in enumerate(close)]
    out.action = close[win]["action"]
    out.rule = "race=engine" if close[win] is best else "race"
    out.sims += len(jobs) * profile.race_sims
    out.p_win = close[win].get("p_win")
    out.hp = hp_sum[win] / hp_n[win] if hp_n[win] else close[win].get("mean_final_hp")
