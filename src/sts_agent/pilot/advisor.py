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

Mentor up to 0.10 (game build v0.107.1) ran that search from its Python, through one
function of the simulator, and the pilot stands in for that function. From 0.11 (v0.111)
the advisor computes a fight's advice inside the simulator, so the pilot wraps the advice
step instead: Mentor's own code rebuilds the fight, the pilot searches it, and Mentor's own
code turns the pilot's result into the advice the mod plays. Two things differ there: a card
choice a fight opens is answered by Mentor's search, which the pilot can no longer reach,
and Mentor's Instinct net, which would otherwise answer every fight the installed mod
ships a net for, is switched off for this advisor so the pilot's search decides.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import sys
import threading
import types
from pathlib import Path

from ..game.characters import character_key
from . import engine
from .potion_context import context_path, read as read_potion_context
from .profiles import load_tuned, profile_for
from .search import decide

SOURCE_ENV = "STS_AGENT_MENTOR_SOURCE"      # folder holding Mentor's realgame_advisor.py
GAME_DIR_ENV = "STS_AGENT_GAME_DIR"         # pinned_game_dir
ROOM_TIER = {"Boss": "Boss", "Elite": "Elite"}
PROBE_SIMS = 16         # Mentor 0.11: the token search of the call that rebuilds the fight for the pilot

logger = logging.getLogger("sts_agent.pilot")


def install(advisor, rs, potion_context=None) -> None:
    """Route the advisor's combat search, for a move and for a card choice inside a fight, through the pilot."""
    table, tuned = engine.encounters(rs), {}        # tuned: per-encounter profiles by character, read on first use
    core = types.SimpleNamespace(mcts_recommend=rs.mcts_recommend, mcts_score_action_forced=rs.mcts_score_action_forced,
                                 state_to_dict=rs.state_to_dict)
    turn = threading.local()
    fights: dict = {}
    mentor_advice, mentor_screen = advisor.compute_combat_advice, advisor.compute_screen_advice

    if potion_context is not None:
        class PotionTrackingMirror(advisor.AdvisorMirror):
            potion_used = False

            def on_world_action(self, msg):
                if msg.get("decision_type") == "combat_action" and msg.get("potion"):
                    self.potion_used = True
                return super().on_world_action(msg)

        advisor.AdvisorMirror = PotionTrackingMirror

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
        # The engine names the character on the fight itself; an unknown one fights with Ironclad's profiles.
        character = character_key((wire.get("player") or {}).get("class")) or "IRONCLAD"
        if character not in tuned:
            tuned[character] = load_tuned(character=character)
        return encounter, tier, character, profile_for(encounter, tier, act, tuned[character], character)

    def enter(state):
        run = state.get("run_state") or {}
        turn.room = state.get("room_type")
        turn.fight = (state.get("act") or run.get("act"), state.get("floor") or run.get("floor"), turn.room)
        turn.drop_chance_pct, turn.potion_used = None, False

    def combat_advice(state, cfg, sims, mirror=None):
        enter(state)
        context = read_potion_context(potion_context, act=turn.fight[0], floor=turn.fight[1])
        turn.potion_used = getattr(mirror, "potion_used", False)
        starting_count = context.get("starting_potion_count")
        potions = state.get("potions")
        if type(starting_count) is int and isinstance(potions, list):
            chance = context.get("drop_chance_pct")
            turn.drop_chance_pct = chance if type(chance) is int else None
            # A restarted advisor may have missed a potion action already visible on the belt.
            turn.potion_used |= sum(not p.get("empty") for p in potions) < starting_count
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
            encounter, tier, character, profile = profile_of(state)
            d = decide(core, state, profile, seed=kwargs.get("seed") or 0,
                       carry=(kwargs.get("win_hp_carry"), kwargs.get("win_potion_carry")), tier=tier,
                       drop_chance_pct=turn.drop_chance_pct, potion_used=turn.potion_used, character=character)
            logger.info("pilot %s: %s %s  (%d sims, %.2fs, win %s)", encounter or turn.room, d.rule, d.action, d.sims, d.seconds,
                        "?" if d.win_chance is None else f"{d.win_chance:.0%}")
            return d.result
        except Exception:
            logger.exception("pilot search failed; this decision uses Mentor's own search")
            return core.mcts_recommend(state, **kwargs)

    def combat_advice_in_engine(state, cfg, sims, mirror=None):
        """Mentor 0.11: the pilot's search between Mentor's own fight rebuild and its own advice step."""
        enter(state)
        context = read_potion_context(potion_context, act=turn.fight[0], floor=turn.fight[1])
        turn.potion_used = getattr(mirror, "potion_used", False)
        starting_count = context.get("starting_potion_count")
        potions = state.get("potions")
        if type(starting_count) is int and isinstance(potions, list):
            chance = context.get("drop_chance_pct")
            turn.drop_chance_pct = chance if type(chance) is int else None
            turn.potion_used |= sum(not p.get("empty") for p in potions) < starting_count
        try:
            from display_names import _names         # Mentor's: the names its advice text uses
            # Mentor's own function with a token search: it checks the request, keeps its mirror in step and
            # hands back the fight as the simulator rebuilt it.
            _, fight, call_cfg, _ = mentor_advice(state, cfg, PROBE_SIMS, mirror)
            encounter, tier, character, profile = profile_of(fight)
            search = advisor._mcts_kwargs(call_cfg, sims)
            d = decide(core, fight, profile, seed=search.get("seed") or 0,
                       carry=(search.get("win_hp_carry"), search.get("win_potion_carry")), tier=tier,
                       drop_chance_pct=turn.drop_chance_pct, potion_used=turn.potion_used, character=character)
            advice, rows = rs.advice_from_result(state, d.result, dataclasses.asdict(cfg), max(int(d.sims or sims), 1),
                                                 _names(), None)
            logger.info("pilot %s: %s %s  (%d sims, %.2fs, win %s)", encounter or turn.room, d.rule, d.action, d.sims,
                        d.seconds, "?" if d.win_chance is None else f"{d.win_chance:.0%}")
            return advice, fight, dataclasses.replace(call_cfg, sims=sims), rows
        except Exception:
            logger.exception("pilot search failed; this decision uses Mentor's own search")
            return mentor_advice(state, cfg, sims, mirror)

    if hasattr(rs, "advice_from_result"):       # Mentor 0.11 and later
        if hasattr(advisor, "InstinctAdvisor"):
            class SearchInsteadOfInstinct(advisor.InstinctAdvisor):
                """Mentor's Instinct net, declined: with the pilot on, the fight search answers, and that is the pilot's."""

                def capabilities(self):
                    return []

                def advise(self, *args, **kwargs):
                    raise advisor.InstinctUnavailable("the STS agent's pilot decides fights")

            advisor.InstinctAdvisor = SearchInsteadOfInstinct      # main() builds its own when given --instinct-weights
            advisor.INSTINCT = SearchInsteadOfInstinct()
        advisor.compute_combat_advice = combat_advice_in_engine
    else:
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
    if not bundle.is_dir():                   # Mentor 0.11 ships a native advisor: its data sits beside the exe
        bundle = bundle.parent
    runtime_paths.data_root = lambda: bundle  # the installed mod's localization and nets
    import realgame_advisor
    install(realgame_advisor, rs, context_path(game_dir))
    return realgame_advisor.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
