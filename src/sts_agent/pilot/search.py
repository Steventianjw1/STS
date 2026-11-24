"""One combat decision: a search, then a race between the options it cannot tell apart.

The engine's own root pick treats every option within a fixed value band of the best
(0.02, about 6 HP of 80) as equal and lets hand-written rules choose among them: play
the free card, spend the energy, take the most visited. The pilot spends search instead:
each close option gets independent searches with every simulation forced through it,
and the best average wins. A decision the search already resolves costs nothing extra.

Two things the engine's pick leaves to chance are decided here as well. A potion is kept
while the fight is not in doubt (``_hold``), because the engine's price of a kept potion
is smaller than the noise of its estimates. And when a fight asks for one card to give up
(an exhaust target) and the race scores several within ``Profile.choice_tie``, the least
useful of them goes (``_keep_worth``): the search scores two copies of one card as far
apart as that, so among such cards its own pick is chance.

Relics need no rule of their own inside a fight: the engine simulates each of them, so a
move that is better for a relic already scores better. ``Profile.relic_tie`` is the one
lever beyond that: raced moves closer than it count as equal and the one a held relic
gains from is played (``_relic_gain``: a counter moved toward its payout, a once-a-fight
effect used, a card made free), which banks the counter a relic carries into the next
fight. It is off by default; ``profiles`` records what it measured.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .profiles import Profile

CORES = os.cpu_count() or 1
EXACT_RULES = {"lost_position", "illegal_fallback"}   # engine picks that are not a judgement between values
SAFE_WIN = 0.90      # a held potion waits while the best other move wins at least this often...
DRINK_GAIN = 0.05    # ...unless drinking it adds this much win chance
GIVE_UP = ("exhaust_from_hand",)   # card choices that take the chosen card out of the fight
_pool: ThreadPoolExecutor | None = None


@dataclass
class Decision:
    action: dict                 # the engine-dialect action to apply
    rule: str                    # "engine:<its rule>", "race" (the race changed the pick), "race=engine", "relic" (a relic
                                 # decided between equal moves), "choice" (the least useful of equal cards was given up)
                                 # or "hold" (a potion was kept)
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


def _is_drink(row) -> bool:
    return row["action"].get("action") in ("drink_potion", "potion")


def _hold(rows) -> list:
    """The drinks to keep back: the best other move wins anyway and the drink adds little to that.

    A row without a win chance is never held: the hold acts only on what the search measured.
    """
    others = [r for r in rows if not _is_drink(r)]
    if not others or len(others) == len(rows):
        return []
    ref = min(others, key=lambda r: r.get("pick_rank") if r.get("pick_rank") is not None else 1 << 30)
    if ref.get("p_win") is None or ref["p_win"] < SAFE_WIN:
        return []
    return [r for r in rows if _is_drink(r) and r.get("p_win") is not None and r["p_win"] - ref["p_win"] < DRINK_GAIN]


def _relic_gain(rs, state, rows) -> list:
    """How many held relics each option feeds: counters it raises, plus one when it makes a hand card free.

    A card play is stepped on a copy of the state and only the relics' counters and the hand's changed
    costs are read from it, nothing hidden (the next draw, an enemy roll). Ending the turn and drinking
    feed none; neither does an option that cannot be stepped.
    """
    def mark(wire):
        return ({r.get("id"): r.get("counter") or 0 for r in wire["player"].get("relics") or []},
                sum(1 for c in wire["deck"].get("hand") or [] if c.get("cost_mods")))
    counters, freed = mark(rs.state_to_dict(state))
    out = []
    for row in rows:
        gain = 0
        if row["action"].get("action") == "play":
            try:
                copy = state.clone()
                copy.apply_step(row["action"])
                after, freed_after = mark(rs.state_to_dict(copy))
                gain = sum(1 for rid, value in after.items() if value > counters.get(rid, 0)) + (freed_after > freed)
            except Exception:
                gain = 0
        out.append(gain)
    return out


def _keep_worth(state, rows) -> list | None:
    """For a choice of one card to give up (``GIVE_UP``): how much each option's card is worth keeping.

    0 for a Status or Curse, 1 for a starter Strike or Defend, 2 for an upgraded one, 3 for any other
    card. ``None`` when the open decision is not such a choice or an option does not name one card.
    """
    try:
        choice = state.pending_choice()
    except Exception:
        return None
    if not choice or choice.get("kind") not in GIVE_UP or (choice.get("max_select") or 1) != 1:
        return None
    cards = {c.get("index"): c for c in choice.get("cards") or []}
    out = []
    for row in rows:
        picked = row["action"].get("selection") or [row["action"].get("index")]
        card = cards.get(picked[0]) if len(picked) == 1 else None
        if card is None:
            return None
        starter = str(card.get("id") or "").startswith(("STRIKE_", "DEFEND_"))
        out.append(0 if card.get("type") in ("Status", "Curse") else (2 if card.get("upgraded") else 1) if starter else 3)
    return out


def decide(rs, state, profile: Profile, seed=0, carry=(None, None), hold=False) -> Decision:
    """Choose the action to play in ``state`` (an engine ``CombatState`` with a decision open).

    ``hold`` is the fight's potion rule (``profiles.holds_potions`` of its tier); a profile that
    sets ``hold_potions`` wins.
    """
    started = time.perf_counter()
    threads = max(1, min(profile.threads, CORES))
    knobs = _knobs(profile, carry)
    main = dict(knobs, sims=profile.sims, threads=threads, seed=seed,
                c_puct=profile.c_puct if profile.c_puct is not None else c_puct_for(profile.sims, threads))
    if profile.leaf_playouts is not None:
        main["leaf_playouts"] = profile.leaf_playouts
    if profile.matched_luck:
        main["common_random_numbers"] = True
    result = rs.mcts_recommend(state, **main)
    rows = result.get("actions") or []
    if not rows:
        raise ValueError("the search returned no action for this state")
    best = next((r for r in rows if r.get("is_best")), rows[0])
    engine_rule = (result.get("pick") or {}).get("rule") or "argmax"
    out = Decision(action=best["action"], rule=f"engine:{engine_rule}", rows=rows, sims=profile.sims,
                   p_win=best.get("p_win"), hp=best.get("mean_final_hp"), result=result)
    if engine_rule not in EXACT_RULES:
        open_rows = rows
        if profile.hold_potions if profile.hold_potions is not None else hold:
            held = _hold(rows)
            if held:
                open_rows = [r for r in rows if all(r is not h for h in held)]
                if any(best is h for h in held):
                    best = min(open_rows, key=lambda r: r.get("pick_rank") if r.get("pick_rank") is not None else 1 << 30)
                    out.action, out.rule, out.p_win, out.hp = best["action"], "hold", best.get("p_win"), best.get("mean_final_hp")
        if profile.pick == "race" and len(open_rows) > 1:
            _race(rs, state, profile, knobs, seed, open_rows, best, out)
        if out.rule == "race":
            crown(result, out.action, "pilot_race", "the best of the close moves after searching each on its own")
        elif out.rule == "relic":
            crown(result, out.action, "pilot_relic", "of the moves the search holds equal, the one a held relic gains from")
        elif out.rule == "choice":
            crown(result, out.action, "pilot_choice", "of the cards the search holds equal, the least useful is given up")
        elif out.rule == "hold":
            crown(result, out.action, "pilot_hold", "the potion is kept: the fight is not in doubt without it")
    out.seconds = time.perf_counter() - started
    return out


def _race(rs, state, profile, knobs, seed, rows, best, out: Decision) -> None:
    top = max(r["q"] for r in rows)
    close = sorted((r for r in rows if r["q"] >= top - profile.race_band), key=lambda r: -r["q"])[:profile.race_width]
    if all(r is not best for r in close):
        close.append(best)       # the engine's pick always runs, so a rule-made pick is tested, not assumed
    if len(close) < 2:
        if out.rule != "hold":
            out.rule = "race=engine"
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
    by_relic = by_worth = False
    near = [i for i in range(len(close)) if score[i] >= score[win] - profile.relic_tie]
    if profile.relic_tie > 0 and len(near) > 1:        # equal as far as the search can tell: the relics decide
        gain = dict(zip(near, _relic_gain(rs, state, [close[i] for i in near])))
        fed = max(near, key=lambda i: (gain[i], score[i], close[i] is best))
        by_relic, win = gain[fed] > gain[win], fed if gain[fed] > gain[win] else win
    near = [i for i in range(len(close)) if score[i] >= score[win] - profile.choice_tie]
    if profile.choice_tie > 0 and len(near) > 1:       # cards the search holds equal: give up the least useful
        worth = _keep_worth(state, [close[i] for i in near])
        if worth is not None:
            worth = dict(zip(near, worth))
            least = min(near, key=lambda i: (worth[i], -score[i], close[i] is not best))
            by_worth, win = worth[least] < worth[win], least if worth[least] < worth[win] else win
    out.raced = [{"action": row["action"], "score": round(score[i], 4), "visits": visits[i]} for i, row in enumerate(close)]
    out.action = close[win]["action"]
    out.rule = (("choice" if by_worth else "relic" if by_relic else "race") if close[win] is not best
                else "hold" if out.rule == "hold" else "race=engine")
    out.sims += len(jobs) * profile.race_sims
    out.p_win = close[win].get("p_win")
    out.hp = hp_sum[win] / hp_n[win] if hp_n[win] else close[win].get("mean_final_hp")
