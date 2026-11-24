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

from .profiles import HP_WORTH, LOSS_VALUE, Profile, holds_potions, playouts_for, win_chance

CORES = os.cpu_count() or 1
EXACT_RULES = {"lost_position", "illegal_fallback"}   # engine picks that are not a judgement between values
SAFE_WIN = 0.9968    # a held potion waits while the best other move wins the fight at least this often (``profiles.win_chance``)
GIVE_UP = ("exhaust_from_hand",)   # card choices that take the chosen card out of the fight
PLAYOUT_STEPS = 600  # a played-out fight is cut off after this many moves
PLAYOUT_SE = 1.0     # a played-out move replaces the race's pick when it is ahead by this many standard errors
_pool: ThreadPoolExecutor | None = None


@dataclass
class Decision:
    action: dict                 # the engine-dialect action to apply
    rule: str                    # "engine:<its rule>", "race" (the race changed the pick), "race=engine", "relic" (a relic
                                 # decided between equal moves), "choice" (the least useful of equal cards was given up)
                                 # or "hold" (a potion was kept)
    rows: list = field(default_factory=list)       # the main search's per-option rows
    raced: list = field(default_factory=list)      # [{"action", "score", "visits"}] when a race ran
    played: list = field(default_factory=list)     # [{"action", "value", "wins", "worlds"}] when close moves were played out
    sims: int = 0                # simulations spent
    seconds: float = 0.0
    p_win: float | None = None   # the search's own: the share of its playouts through the chosen option that were won
    win_chance: float | None = None      # what that means for the fight (``profiles.win_chance``)
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


def _hold(rows, tier) -> list:
    """The drinks to keep back: every one, while the best other move wins the fight anyway.

    Win chances are the fight's real ones (``profiles.win_chance``), not the search's own share of won
    playouts. A row without a win chance is never held: the hold acts only on what the search measured.
    """
    others = [r for r in rows if not _is_drink(r)]
    if not others or len(others) == len(rows):
        return []
    ref = min(others, key=lambda r: r.get("pick_rank") if r.get("pick_rank") is not None else 1 << 30)
    if ref.get("p_win") is None or win_chance(ref["p_win"], tier) < SAFE_WIN:
        return []
    return [r for r in rows if _is_drink(r) and r.get("p_win") is not None]


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


def play_out(rs, state, action, world, sims, knobs) -> tuple:
    """One way the rest of the fight can go after ``action``: (won, HP at the end, max HP).

    The fight continues in a possible world (``sample_world``: the hidden draw order dealt anew, every
    roll reseeded from ``world``) with a small search of ``sims`` deciding every later move. The engine's
    own playouts finish a fight with a fast policy that plays far below the search and so understate
    how often the fight is won; a searching player is slower and closer to how the fight will be played.
    """
    fight = state.sample_world(world)
    fight.apply_step(action)
    search = dict(knobs, sims=sims, threads=1, c_puct=c_puct_for(sims, 1))
    for step in range(PLAYOUT_STEPS):
        if fight.terminal_value()[0]:
            break
        legal = fight.legal_actions()
        if not legal:
            break
        if len(legal) == 1:
            fight.apply_step(legal[0])
            continue
        rows = rs.mcts_recommend(fight, seed=(world * 7919 + step) & 0x7FFFFFFF, **search)["actions"]
        fight.apply_step(next((r for r in rows if r.get("is_best")), rows[0])["action"])
    over, value = fight.terminal_value()
    player = rs.state_to_dict(fight)["player"]
    return bool(over and value > 0), int(player.get("hp") or 0), int(player.get("max_hp") or 0)


def decide(rs, state, profile: Profile, seed=0, carry=(None, None), tier=None) -> Decision:
    """Choose the action to play in ``state`` (an engine ``CombatState`` with a decision open).

    ``tier`` is the fight's ("Weak", "Normal", "Elite", "Boss"): it sets the rules a profile leaves
    open (whether potions are held, whether close moves are played out) and how the search's win
    chance reads as a real one. Without it no such rule applies.
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
        if profile.hold_potions if profile.hold_potions is not None else holds_potions(tier):
            held = _hold(rows, tier)
            if held:
                open_rows = [r for r in rows if all(r is not h for h in held)]
                if any(best is h for h in held):
                    best = min(open_rows, key=lambda r: r.get("pick_rank") if r.get("pick_rank") is not None else 1 << 30)
                    out.action, out.rule, out.p_win, out.hp = best["action"], "hold", best.get("p_win"), best.get("mean_final_hp")
        if profile.pick == "race" and len(open_rows) > 1:
            worlds = profile.playouts if profile.playouts is not None else playouts_for(tier)
            _race(rs, state, profile, knobs, seed, open_rows, best, out, worlds)
        if out.rule == "race":
            crown(result, out.action, "pilot_race", "the best of the close moves after searching each on its own")
        elif out.rule == "relic":
            crown(result, out.action, "pilot_relic", "of the moves the search holds equal, the one a held relic gains from")
        elif out.rule == "playout":
            crown(result, out.action, "pilot_playout", "the best of the close moves when the fight is played to its end")
        elif out.rule == "choice":
            crown(result, out.action, "pilot_choice", "of the cards the search holds equal, the least useful is given up")
        elif out.rule == "hold":
            crown(result, out.action, "pilot_hold", "the potion is kept: the fight is not in doubt without it")
    out.win_chance = win_chance(out.p_win, tier) if out.p_win is not None else None
    out.seconds = time.perf_counter() - started
    return out


def _race(rs, state, profile, knobs, seed, rows, best, out: Decision, worlds=0) -> None:
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
    by_playout = False
    if worlds > 0:
        played = _play_outs(rs, state, profile, knobs, seed, close, score, win, worlds, out)
        by_playout, win = played != win, played
    out.raced = [{"action": row["action"], "score": round(score[i], 4), "visits": visits[i]} for i, row in enumerate(close)]
    out.action = close[win]["action"]
    out.rule = (("playout" if by_playout else "choice" if by_worth else "relic" if by_relic else "race") if close[win] is not best
                else "hold" if out.rule == "hold" else "race=engine")
    out.sims += len(jobs) * profile.race_sims
    out.p_win = close[win].get("p_win")
    out.hp = hp_sum[win] / hp_n[win] if hp_n[win] else close[win].get("mean_final_hp")


def _play_outs(rs, state, profile, knobs, seed, close, score, win, worlds, out: Decision) -> int:
    """Play the race's close moves out and return the index of the one to play (``win`` unless another is ahead).

    The race scores a move by the engine's playouts, which a fast policy finishes; here each of the
    closest moves is followed through the same possible worlds by a searching player (``play_out``),
    so the luck is the same for every move and only the move differs. The race's pick stands unless
    another move is ahead of it by ``PLAYOUT_SE`` standard errors of the paired difference.
    """
    near = sorted((i for i in range(len(close)) if score[i] >= score[win] - profile.playout_band), key=lambda i: (i != win, -score[i]))
    near = near[:max(2, profile.playout_width)]
    if len(near) < 2:
        return win
    carry = knobs.get("win_hp_carry", 1.0)
    seeds = [(seed * 2654435761 + k * 40503 + 1) & 0x7FFFFFFF for k in range(worlds)]
    jobs = {i: [_workers().submit(play_out, rs, state, close[i]["action"], world, profile.playout_sims, knobs) for world in seeds] for i in near}
    value, wins = {}, {}
    for i, row in jobs.items():
        ends = [job.result() for job in row]
        value[i] = [1.0 + HP_WORTH * carry * hp / max(1, top) if won else LOSS_VALUE for won, hp, top in ends]
        wins[i] = sum(won for won, _, _ in ends)
    out.played = [{"action": close[i]["action"], "value": round(sum(value[i]) / worlds, 4), "wins": wins[i], "worlds": worlds} for i in near]
    out.sims += len(near) * worlds * profile.playout_sims      # per decision of each played-out fight; a floor, not a count
    best, lead = win, 0.0
    for i in near[1:]:
        gap = [a - b for a, b in zip(value[i], value[win])]
        mean = sum(gap) / worlds
        spread = (sum((g - mean) ** 2 for g in gap) / max(1, worlds - 1) / worlds) ** 0.5
        if mean > lead and mean > PLAYOUT_SE * spread:
            best, lead = i, mean
    return best
