"""Spire Mentor's advisor with the pilot deciding combat.

The mod starts its advisor through ``SimAdvisor.cmd`` when that file sits next to
SpireMentor.dll (its own developer hook, ahead of the packaged exe). ``install.sync``
writes that file to start this module, which runs Mentor's advisor from its published
source (bridge, state mirror, coach and logs all unchanged) against the installed engine
and replaces one thing: the combat search behind each recommendation, and behind each
card choice a fight opens (which card to exhaust, which generated card to take), which
Mentor answers with that same search. Mentor's Autobattle then plays the pilot's pick,
so the agent's own Autobattle bridge needs no change.

Any failure inside the pilot falls back to Mentor's own search for that decision; a
fight is never left without advice.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import types
from pathlib import Path

from . import engine
from .profiles import default_profile, load_tuned
from .search import decide

SOURCE_ENV = "STS_AGENT_MENTOR_SOURCE"      # folder holding Mentor's realgame_advisor.py
GAME_DIR_ENV = "STS_AGENT_GAME_DIR"         # pinned_game_dir
ROOM_TIER = {"Boss": "Boss", "Elite": "Elite"}

logger = logging.getLogger("sts_agent.pilot")


def install(advisor, rs) -> None:
    """Route the advisor's combat search, for a move and for a card choice inside a fight, through the pilot."""
    table, tuned = engine.encounters(rs), load_tuned()
    core = types.SimpleNamespace(mcts_recommend=rs.mcts_recommend, mcts_score_action_forced=rs.mcts_score_action_forced,
                                 state_to_dict=rs.state_to_dict)
    turn = threading.local()
    fights: dict = {}
    mentor_advice, mentor_screen = advisor.compute_combat_advice, advisor.compute_screen_advice

    def profile_of(state):
        wire = rs.state_to_dict(state)
        key = getattr(turn, "fight", None)
        if wire.get("round") == 1 or key not in fights:   # the roster is whole on round 1; later it may have changed
            found = engine.find_encounter(table, [e.get("kind") for e in wire.get("encounter") or []], turn.room)
            if found is not None:
                fights[key] = found["id"]
        encounter = fights.get(key)
        entry = next((e for e in table if e["id"] == encounter), None)
        tier = ROOM_TIER.get(turn.room) or (entry["tier"] if entry else "Normal")
        act = entry["act"] if entry else key[0]
        return encounter, tier, tuned.get(encounter) or default_profile(tier, act)

    def enter(state):
        run = state.get("run_state") or {}
        turn.room = state.get("room_type")
        turn.fight = (state.get("act") or run.get("act"), state.get("floor") or run.get("floor"), turn.room)

    def combat_advice(state, cfg, sims, mirror=None):
        enter(state)
        turn.main = True                      # the next search is this decision's; later ones only draw the turn plan
        try:
            return mentor_advice(state, cfg, sims, mirror)
        finally:
            turn.main = False

    def screen_advice(state, *args, **kwargs):
        if state.get("combat") is None:       # a deck screen between fights: not the pilot's
            return mentor_screen(state, *args, **kwargs)
        enter(state)
        turn.choice = True                    # every search in here ranks a card choice of this fight, one per card picked
        try:
            return mentor_screen(state, *args, **kwargs)
        finally:
            turn.choice = False

    def recommend(state, **kwargs):
        if not getattr(turn, "main", False) and not getattr(turn, "choice", False):
            return core.mcts_recommend(state, **kwargs)
        turn.main = False
        try:
            encounter, tier, profile = profile_of(state)
            d = decide(core, state, profile, seed=kwargs.get("seed") or 0,
                       carry=(kwargs.get("win_hp_carry"), kwargs.get("win_potion_carry")), tier=tier)
            logger.info("pilot %s: %s %s  (%d sims, %.2fs, win %s)", encounter or turn.room, d.rule, d.action, d.sims, d.seconds,
                        "?" if d.win_chance is None else f"{d.win_chance:.0%}")
            return d.result
        except Exception:
            logger.exception("pilot search failed; this decision uses Mentor's own search")
            return core.mcts_recommend(state, **kwargs)

    advisor.compute_combat_advice = combat_advice
    advisor.compute_screen_advice = screen_advice
    rs.mcts_recommend = recommend


def main(argv=None) -> int:
    source = os.environ.get(SOURCE_ENV)
    if not source or not (Path(source) / "realgame_advisor.py").is_file():
        print(f"pilot advisor: {SOURCE_ENV} must name the folder of Mentor's realgame_advisor.py (got {source!r})", file=sys.stderr)
        return 2
    game_dir = os.environ.get(GAME_DIR_ENV) or "data/game"
    rs = engine.load(game_dir)                # the installed engine; Mentor's source imports the same module
    sys.path.insert(0, source)
    import runtime_paths                      # Mentor's: where its data lives
    bundle = engine.engine_dir(game_dir).parent
    runtime_paths.data_root = lambda: bundle  # the installed localization and nets, which match the installed engine
    import realgame_advisor
    install(realgame_advisor, rs)
    return realgame_advisor.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
