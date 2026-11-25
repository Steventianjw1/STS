"""Run coordinator: LLM choices, with optional native Autobattle ownership of whole fights.

Loop per decision:

    observe -> wait until a supported screen is ready AND stable
            -> track encounter memory / combat outcomes
            -> execute forced progress clicks without calling a model, otherwise
            -> refresh the strategic assessment if strategic facts changed
            -> build DecisionFrame -> LLM picks one action id (<= 1 corrective retry)
            -> record decision (before execution)
            -> re-observe; send only if the screen is unchanged and the binding still exists
            -> send ONCE -> wait for screen-specific resolution -> record outcome

No heuristic fallback for actual choices: optional reward batches and shop plans must be
explicitly selected by the model. Ambiguous deliveries are inspected and never resent.
Single-caller, synchronous.
"""
from __future__ import annotations

import json
import os
import re
import time
from itertools import combinations
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable

from ..evidence import boss_demands, budget, deck_balance, enemy_patterns
from ..evidence.potion_odds import drop_state
from ..game.client import AmbiguousActionError, GameClientError, GameUnavailable, HttpError
from ..config import Limits
from ..session.hooks import SessionHooks
from ..evidence.route_paths import enumerate_routes, map_type
from ..evidence.route_risk import agent_loss_scale, node_class
from ..pilot.potion_context import publish as publish_potion_context
from ..strategic import StrategicSnapshot
from .models import SMITH, Choice, DecisionFrame, is_rest_destination, names_card
from .policy import LLMDecisionPolicy, model_identity
from .prompts import MENTOR_PILOT, OWN_PILOT
from .screens import CAPABILITIES, MOD_REVISION, ScreenView, automatic_action, build_view
from .whatif import WhatIfTools

# stop reasons (shared names with the baseline runner where they mean the same thing)
STOP_COMBAT_ENDED = "combat_ended"
STOP_PLAYER_DEFEATED = "player_defeated"
STOP_RUN_OVER_UNCONFIRMED = "run_over_unconfirmed"
STOP_UNSUPPORTED_SCREEN = "unsupported_decision_screen"
STOP_NOT_IN_COMBAT = "not_in_combat"
STOP_READY_TIMEOUT = "ready_timeout"
STOP_UNRESOLVED_ACTION = "unresolved_action"
STOP_AMBIGUOUS_UNRESOLVED = "ambiguous_outcome_unresolved"
STOP_REPEATED_REJECTIONS = "repeated_rejections"
STOP_MAX_ACTIONS = "max_actions"
STOP_NO_PROGRESS = "no_progress"
STOP_GAME_UNAVAILABLE = "game_unavailable"
STOP_INTERRUPTED = "interrupted"
STOP_STEP_DONE = "step_done"
STOP_MODEL_FAILED = "model_failed"
STOP_MODEL_BUDGET = "model_budget_exhausted"
STOP_INVALID_MODEL_OUTPUT = "invalid_model_output"
STOP_INPUT_TOO_LARGE = "decision_input_too_large"
STOP_NO_ACTIONS = "no_actions_on_actionable_screen"
STOP_INDEX_MISMATCH = "adapter_index_mismatch"
STOP_AUTOBATTLE_FAILED = "autobattle_failed"

LLM_PROVENANCE_LIVE = {"data_source": "agent_generated", "behavior": "llm_policy", "is_expert_demonstration": False,
                       "note": "Choices made by an LLM through this harness. Not human or expert play."}
LLM_PROVENANCE_SCRIPTED = {"data_source": "synthetic", "behavior": "scripted_model_replies",
                           "is_expert_demonstration": False, "is_llm_reasoning": False,
                           "note": "SYNTHETIC: canned model replies (test double). Not LLM reasoning, not live play."}


# Non-combat screens that can open on top of a fight (potion or card-effect pickers). While a
# combat is open on the same floor they belong to that fight; they are not a room exit.
COMBAT_OVERLAY_KINDS = ("card_select",)
# The game's refusal while a room is still opening, e.g. "Rest site room is not open" (observed live).
_ROOM_NOT_OPEN = re.compile(r"\bnot open\b", re.I)
STATE_VERSION = 1


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = "", view: ScreenView | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.view = reason, detail, view


@dataclass
class _Combat:
    key: str
    encounter_type: str | None
    start_hp: int | None
    rounds: int | None = None
    potions_used: int = 0
    act: int | None = None
    floor: int | None = None
    # HP movement seen between consecutive observations of this fight, whoever played it.
    last_hp: int | None = None
    damage: int = 0            # sum of HP decreases: what run files call damage_taken
    healed: int = 0            # sum of HP increases, including end-of-combat healing
    hp_tracked: bool = True    # False when part of the fight was not observed with this tracking
    potions_held: list | None = None   # potion names held when the fight was first seen; None = not observed

    def note_hp(self, hp):
        if type(hp) is not int:
            return
        if self.last_hp is not None:
            self.damage += max(0, self.last_hp - hp)
            self.healed += max(0, hp - self.last_hp)
        self.last_hp = hp


@dataclass
class CoordStats:
    decisions: int = 0
    actions_sent: int = 0
    actions_resolved: int = 0
    rejections: int = 0
    room_not_open_retries: int = 0   # refusals answered by waiting for the room and sending again
    consecutive_rejections: int = 0
    stale: int = 0
    ambiguous: int = 0
    model_retries: int = 0
    provider_retries: int = 0
    strategic_updates: int = 0
    strategic_failures: int = 0
    combats_finished: int = 0
    combats_started: int = 0
    autobattle_combats: int = 0
    offer_handoffs: int = 0       # in-fight offers Mentor held that this agent's model decided
    offer_ai_picks: int = 0       # held in-fight offers handed back to Mentor's own recommendation
    tool_calls: int = 0           # what-if tool requests answered
    blocked: set = field(default_factory=set)   # (view fingerprint, action_id)


_OPP_KIND = {"select_card_reward": "card_reward", "choose_map_node": "route", "choose_event_option": "event",
             "choose_rest_option": "rest_site", "select_relic": "relic_reward", "claim_treasure_relic": "relic_reward"}
_REWARD_KIND = {"gold": "gold_reward", "potion": "potion_reward", "relic": "relic_reward", "card": "card_reward",
                "special_card": "card_reward", "card_removal": "card_removal"}
# Screens that add, remove or change deck cards: they carry deck_balance and the prompt module that reads it.
_DECK_CHOICE_KINDS = ("card_reward", "shop", "event", "card_select")


class RunCoordinator:
    def __init__(self, client, policy: LLMDecisionPolicy, *, planner=None, recorder=None, expert_stats=None, autobattle=None,
                 own_pilot: bool = False, offer_handoff: bool = False, limits: Limits | None = None, scope: str = "run", run_id: str, run_id_source: str,
                 facts_source: str = "observed", objective: str, what_if_tools: bool = True,
                 reward_routine: bool = True,
                 state_path=None, pilot_potion_context=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print, hooks: SessionHooks | None = None):
        if scope not in ("run", "combat", "step"):
            raise ValueError("scope must be run, combat or step")
        self.hooks = hooks or SessionHooks()         # the interface watches and pauses through these; no-ops otherwise
        self.client, self.policy, self.planner, self.rec = client, policy, planner, recorder
        self.expert_stats = expert_stats
        self.autobattle = autobattle
        self.own_pilot = own_pilot                   # the agent's pilot decides inside Autobattle (it keeps potions)
        self.pilot_potion_context = pilot_potion_context
        self.offer_handoff = offer_handoff and autobattle is not None
        self._offer_hold: bool | None = None         # Mentor's answer to the last hold request (None: not asked)
        self._offers_answered: dict = {}             # held offer id -> clock time one answer was sent
        # Fingerprint of a card grid on which this agent's own toggle just made Confirm available.
        self._selection_complete: str | None = None
        # The card named with the decision to smith, until the fire's upgrade grid opens: card and room.
        self._smith_plan: dict | None = None
        # Card rewards of this room's rewards screen that were opened and skipped: the game keeps listing them.
        self._skipped_card_rewards = 0
        self.reward_routine = reward_routine         # collect rewards that need no judgment without a model call
        self._relic_counters: dict = {}              # relic id -> last counter the game showed outside combat
        self.treasures_opened = 0                    # treasure rooms entered this run
        self._handoff = False                        # True only while deciding a held offer
        self.whatif = WhatIfTools(self) if what_if_tools else None
        self.lim = limits or Limits()
        self.scope, self.run_id, self.run_id_source = scope, run_id, run_id_source
        self.facts_source, self.objective = facts_source, objective
        self.clock, self.sleep, self.log = clock, sleep, log
        self.stats = CoordStats()
        self.session_start = clock()
        self.last_progress = clock()
        self._last_fp = None
        self.last_view: ScreenView | None = None
        # memory
        self.encounter_key: str | None = None
        self.history: list = []
        self.turn_intent: str | None = None
        self.route_plan: dict | None = None   # survives encounters; a hypothesis shown back on map screens
        self.route_outcomes: list = []        # every finished combat this run, for re-plan triggers
        self.combat: _Combat | None = None
        self.seen_combat = False
        self.recent_outcomes: deque = deque(maxlen=10)
        self.strat_revision = 0
        self.strat_facts = None
        self.pending_combat_finished = False
        self.last_planner_result = None
        self.strat_route_key = None
        self.strat_place = None                      # (act, floor) of the last planner refresh
        self.energy_observation: dict | None = None
        self.shop_entry: dict | None = None
        self.seq = 0
        self.last_combat_facts: dict | None = None   # shown on pickers that open during a fight
        self.incoming_changes: list = []             # this encounter: intent damage moved by the agent's own actions
        self.hallway_done: dict = {}                 # act -> finished hallway fights (for the early/later class)
        self.risk_ref: dict | None = None            # pooled per-class loss seen on the latest map screen
        self.rest_checkpoints: list = []             # projected vs. actual HP on arrival at planned rest sites
        self.coverage: dict = {}                     # screen kind -> summed evidence_coverage counters
        self._coverage_seen: set = set()
        self.map_memory: dict | None = None          # the act map as last seen, and the node travelled to
        self.act_bosses: dict | None = None          # {"act", "bosses"} from the last screen that showed the act map
        self.state_path = state_path                 # same-run resume file; None disables persistence
        self._restore_pending = state_path is not None
        self.resumed_from: dict | None = None

    # ---------------------------------------------------------------- observe
    def _observe(self, label: str) -> ScreenView:
        raw = self.client.get_state()
        if self.rec:
            self.rec.raw(label, raw)
        view = build_view(raw)
        maximum = (view.facts.get("player") or {}).get("max_energy")
        if type(maximum) is int and maximum >= 0:
            self.energy_observation = {"max_energy": maximum, "run": dict(view.facts.get("run") or {}),
                                       "source": "observed player.max_energy"}
        if self.combat is not None:
            self.combat.note_hp((view.facts.get("player") or {}).get("hp"))
        if view.fingerprint != self._last_fp:
            self._last_fp = view.fingerprint
            self.last_progress = self.clock()
        self.last_view = view
        self.hooks.observed(view)
        return view

    def _gate(self, point: str, view: ScreenView | None = None) -> float:
        """A safe point to honor the interface's pause or stop. Returns the seconds spent paused.

        Never called between sending an action and observing its resolution. Time spent
        paused is not a game stall, so the no-progress clock restarts on resume.
        """
        waited = self.hooks.gate(point)
        if self.hooks.stop_requested():
            raise _Stop(STOP_INTERRUPTED, "stopped from the interface", view or self.last_view)
        if waited:
            self.last_progress = self.clock()
        return waited

    def _publish_route(self):
        self.hooks.route(self.route_plan, self.map_memory)

    def _observe_tolerant(self, label, deadline) -> ScreenView | None:
        try:
            return self._observe(label)
        except (GameUnavailable, HttpError) as e:
            if self.clock() >= deadline:
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e), self.last_view)
            return None

    def _check_global(self):
        now = self.clock()
        if now - self.last_progress >= self.lim.max_no_progress_s:
            raise _Stop(STOP_NO_PROGRESS, f"state unchanged for {self.lim.max_no_progress_s}s", self.last_view)

    def _wait_ready(self) -> ScreenView:
        deadline = self.clock() + self.lim.ready_timeout_s
        stable, prev = 0, None
        self.hooks.step("waiting", "Waiting for a screen that is ready for a decision")
        while True:
            if self._gate("wait"):
                deadline = self.clock() + self.lim.ready_timeout_s
                stable, prev = 0, None
            self._check_global()
            v = self._observe_tolerant("wait_ready", deadline)
            if v is not None and self.autobattle is not None:
                status = self._autobattle_control("status", v)
                self.hooks.autobattle(status)
                if status.get("combat_in_progress"):
                    self._delegate_autobattle(v)
                    deadline = self.clock() + self.lim.ready_timeout_s
                    stable, prev = 0, None
                    self.hooks.step("waiting", "Waiting for a screen that is ready for a decision")
                    continue
                if status.get("mode") != "Off" or status.get("autoplay"):
                    self._autobattle_control("off", v)
            if v is not None:
                stable = stable + 1 if v.fingerprint == prev else 1
                prev = v.fingerprint
                if stable >= self.lim.settle_polls:
                    self._raise_if_stop(v)
                    if v.ready:
                        return v
            if self.clock() >= deadline:
                raise _Stop(STOP_READY_TIMEOUT, f"no ready screen after {self.lim.ready_timeout_s}s "
                                                f"(last: {v.kind if v else 'unreadable'})", v)
            self.sleep(self.lim.poll_interval_s)

    def _autobattle_control(self, command, view):
        try:
            return self.autobattle.control(command)
        except GameClientError as exc:
            raise _Stop(STOP_AUTOBATTLE_FAILED, str(exc), view) from exc

    def _delegate_autobattle(self, view):
        """Yield the whole fight to native Autobattle; a held in-fight offer is answered once (offer_handoff)."""
        if self._restore_pending:
            self._restore_pending = False
            self._load_state(view)
        if view.in_combat:
            self._track_encounter(view)
        if self.pilot_potion_context is None or self.combat is None:
            return self._play_autobattle(view)
        combat = self.combat
        starting_potions = combat.potions_held
        with publish_potion_context(
            self.pilot_potion_context, act=combat.act, floor=combat.floor,
            drop_chance_pct=drop_state(self.route_outcomes)["next_fight_drop_chance_pct"],
            starting_potion_count=len(starting_potions) if starting_potions is not None else None,
        ):
            return self._play_autobattle(view)

    def _play_autobattle(self, view):
        hold = self._request_offer_hold()
        self._offers_answered.clear()
        self._autobattle_control("on", view)
        self.log("combat: handing the fight to Spire Mentor Autobattle (no LLM combat actions"
                 + ("; offers an enemy opens come back to the model)" if hold else ")"))
        if self.rec:
            self.rec.event("autobattle_started", {"source": "spire_mentor_autobattle", "encounter": view.encounter_key})
        self.hooks.step("autobattle", "Autobattle is playing this fight")
        while True:
            if self.hooks.pause_requested() or self.hooks.stop_requested():
                # Mentor must stop playing before the session waits; it is switched back on after resume.
                self._autobattle_control("off", view)
                if self.rec:
                    self.rec.event("autobattle_paused", {"encounter": view.encounter_key})
                self._gate("autobattle", view)
                if self._autobattle_control("status", view).get("combat_in_progress"):
                    hold = self._request_offer_hold()
                    self._autobattle_control("on", view)
                    if self.rec:
                        self.rec.event("autobattle_resumed", {"encounter": view.encounter_key})
                self.hooks.step("autobattle", "Autobattle is playing this fight")
            self._check_global()
            status = self._autobattle_control("status", view)
            self.hooks.autobattle(status)
            if not status.get("combat_in_progress"):
                self._autobattle_control("off", view)
                after = self._observe("autobattle_finished")
                if after.terminal:
                    self._raise_if_stop(after)
                self._finish_combat(after, *self._delegated_result(after))
                self.stats.autobattle_combats += 1
                self._save_state()
                if self.rec:
                    self.rec.event("autobattle_finished", {"source": "spire_mentor_autobattle", "screen": after.kind})
                self.log(f"combat: Autobattle finished; the model resumes on {after.kind}")
                if self.scope in ("combat", "step"):
                    raise _Stop(STOP_COMBAT_ENDED, "Spire Mentor Autobattle finished", after)
                return
            if status.get("mode") != "Advisor" or not status.get("autobattle") or not status.get("autoplay"):
                raise _Stop(STOP_AUTOBATTLE_FAILED, "Mentor stopped Autobattle; no LLM combat fallback", view)
            if hold and status.get("fight_offer_held") is not None:
                self._answer_held_offer(str(status["fight_offer_held"]))
            self.sleep(self.lim.poll_interval_s)
            observed = self._observe_tolerant("autobattle", self.last_progress + self.lim.ready_timeout_s)
            if observed is not None:
                view = observed
                if view.in_combat:
                    self._track_encounter(view)
                if view.terminal:
                    self._raise_if_stop(view)

    def _delegated_result(self, after: ScreenView) -> tuple[str, str | None]:
        """(result, note) for a delegated fight that just ended without a game-over screen.

        Enemies that were dead at the last poll mean a win. Enemies still alive then are settled by
        the screen that follows: a kill between two polls leads to the rewards screen, while a fight
        that ends another way (a turn limit running out, an enemy leaving) does not. That second case
        is recorded as ``unknown`` with what was seen, never as a victory.
        """
        alive = [e for e in (self.last_combat_facts or {}).get("enemies") or []
                 if type(e.get("hp")) is int and e["hp"] > 0]
        if not alive:
            return "victory", None
        deadline, v = self.clock() + self.lim.ready_timeout_s, after
        while v.kind == "combat_ended" and self.clock() < deadline:   # the short gap before the next screen
            self.sleep(self.lim.poll_interval_s)
            v = self._observe_tolerant("autobattle_finished", deadline) or v
            if v.terminal:
                self._raise_if_stop(v)
        if v.kind in ("rewards", "card_reward"):
            return "victory", None
        seen = "; ".join(
            f"{e.get('name') or '?'} {e['hp']}/{e.get('max_hp')} HP"
            + (" [" + ", ".join(f"{s.get('name')} {s.get('amount')}" for s in (e.get("statuses") or [])[:3]) + "]"
               if e.get("statuses") else "")
            for e in alive[:2])
        return "unknown", (f"NOT CONFIRMED AS A WIN: last seen {seen}; no rewards screen followed (next: {v.kind}). "
                           "Assume any reward for winning was not received.")

    def _request_offer_hold(self) -> bool:
        """Ask Mentor to hold in-fight choose-a-card offers instead of answering them.

        False leaves the fight exactly as before: Mentor answers every offer itself.
        """
        if not self.offer_handoff:
            return False
        try:
            active, detail = self.autobattle.control("hold_offers").get("fight_offer_hold") is True, ""
        except GameClientError as exc:
            active, detail = False, str(exc)[:300]
        if active != self._offer_hold:
            self._offer_hold = active
            self.log("combat: Mentor holds in-fight offers for this agent" if active else
                     f"combat: in-fight offers stay with Mentor (hold unavailable{': ' + detail if detail else ''})")
            if self.rec:
                self.rec.event("autobattle_offer_hold", {"active": active, "detail": detail})
        return active

    def _answer_held_offer(self, offer: str):
        """Mentor is holding one in-fight offer: answer it exactly once, then Mentor plays on.

        An offer an enemy opened during its turn has no fight search in Mentor (it would score the
        options as deck picks), so the model decides it with the fight in view. Any other held
        offer goes back to Mentor's own recommendation, which is what it would have played.
        Whose turn it is comes from the picker screen when it says so, otherwise from the last
        polled combat state; an unskippable offer of only Status/Curse cards counts as enemy-opened.
        """
        sent = self._offers_answered.get(offer)
        if sent is not None:
            if self.clock() - sent >= self.lim.ready_timeout_s:
                raise _Stop(STOP_AUTOBATTLE_FAILED, "an in-fight offer stayed open after it was answered; "
                                                    "not answering it twice", self.last_view)
            return
        deadline = self.clock() + self.lim.ready_timeout_s
        stable, prev = 0, None
        while True:   # the screen as this agent reads it must be the settled choose-a-card picker
            self._check_global()
            v = self._observe_tolerant("held_offer", deadline)
            if v is not None:
                if v.terminal:
                    self._raise_if_stop(v)
                choose = (v.kind == "card_select" and v.ready and bool(v.candidates)
                          and v.facts["screen_details"].get("screen_type") == "choose")
                stable = stable + 1 if choose and v.fingerprint == prev else int(choose)
                prev = v.fingerprint
                if stable >= self.lim.settle_polls:
                    break
            if self.clock() >= deadline:
                raise _Stop(STOP_AUTOBATTLE_FAILED, "Mentor is holding an in-fight offer this agent cannot read "
                                                    f"(screen {v.kind if v else 'unreadable'})", v)
            self.sleep(self.lim.poll_interval_s)
        details = v.facts["screen_details"]
        side = ((v.facts.get("combat_context") or {}).get("combat") or {}).get("turn_side")
        side_source = "picker_screen"
        if side is None:
            # The mod's picker state has no battle block, so the side comes from the fight as it was
            # last polled, a fraction of a second before the offer was reported as held.
            side = ((self.last_combat_facts or {}).get("combat") or {}).get("turn_side") if self.combat else None
            side_source = "last_combat_observation" if side is not None else "unknown"
        offered = details.get("cards") or []
        # A poll can miss the turn change. An offer of nothing but Status/Curse cards that cannot be
        # skipped is one an enemy forces, whichever side the last poll showed.
        forced = (bool(offered) and details.get("can_skip") is False
                  and all(str(c.get("type") or "").lower() in ("status", "curse") for c in offered))
        by = "llm" if side == "enemy" or forced else "mentor"
        cards = [c.get("name") for c in offered]
        if self.rec:
            self.rec.event("autobattle_offer", {"offer": offer, "turn_side": side, "turn_side_source": side_source,
                                                "forced_harmful_offer": forced, "decided_by": by, "cards": cards,
                                                "observation_fingerprint": v.fingerprint})
        if by == "mentor":
            self._offers_answered[offer] = self.clock()   # one request only; its effect is seen by polling
            try:
                self.autobattle.control("ai_pick")
            except GameClientError as exc:   # e.g. the hold ended by itself; a still-open offer stops the run above
                self.log(f"combat: Mentor did not take the held offer back: {str(exc)[:200]}")
                return
            self.stats.offer_ai_picks += 1
            self.log(f"combat: held offer {cards} (turn side {side}) -> Mentor's own pick")
            return
        self.log(f"combat: held offer {cards} opened in the enemy's turn -> the model decides this one choice")
        self._handoff = True
        try:
            if self._decide(v):
                self._offers_answered[offer] = self.clock()
                self.stats.offer_handoffs += 1
        finally:
            self._handoff = False

    def _raise_if_stop(self, v: ScreenView):
        if v.terminal:
            if self.combat:
                self._finish_combat(v, "defeat" if v.terminal == "defeat" else "unknown")
            raise _Stop(STOP_PLAYER_DEFEATED if v.terminal == "defeat" else STOP_RUN_OVER_UNCONFIRMED,
                        f"game_over screen ({v.terminal})", v)
        if (self.scope == "combat" and self.seen_combat and not v.in_combat and self.combat
                and not self._fight_overlay(v)):
            self._finish_combat(v, "victory")
            raise _Stop(STOP_COMBAT_ENDED, f"left combat (screen {v.kind})", v)
        if v.unsupported:
            raise _Stop(STOP_UNSUPPORTED_SCREEN, f"missing capability - {v.unsupported}", v)

    # -------------------------------------------------------- encounter memory
    def _fight_overlay(self, v: ScreenView) -> bool:
        """A picker that opened during the current fight (same floor, combat still open)."""
        return (self.combat is not None and not v.in_combat and v.kind in COMBAT_OVERLAY_KINDS
                and v.encounter_key == self.combat.key.rsplit(":", 1)[0] + ":room")

    def _track_encounter(self, v: ScreenView):
        key = self.combat.key if self._fight_overlay(v) else v.encounter_key
        if key != self.encounter_key:
            if self.combat and not v.in_combat:
                self._finish_combat(v, "victory")
            self.history, self.turn_intent, self.incoming_changes = [], None, []
            self.encounter_key = key
            self._skipped_card_rewards = 0
            if v.kind == "treasure":
                self.treasures_opened += 1
        if not v.in_combat and not self._fight_overlay(v):
            # Outside fights only: a counter some relics show during a fight alone is not a count of uses.
            for r in v.facts.get("relics") or []:
                if r.get("id") and type(r.get("counter")) is int:
                    self._relic_counters[str(r["id"])] = r["counter"]
        if v.in_combat:
            self.seen_combat = True
            if self.combat is None or self.combat.key != v.encounter_key:
                run = v.facts.get("run") or {}
                self.combat = _Combat(v.encounter_key, v.state_type, v.facts["player"]["hp"],
                                      act=run.get("act"), floor=run.get("floor"),
                                      last_hp=v.facts["player"]["hp"], potions_held=_potion_names(v.facts))
                self.stats.combats_started += 1
            if v.combat_round is not None:
                self.combat.rounds = max(self.combat.rounds or 0, v.combat_round)
            self.last_combat_facts = {k: v.facts.get(k) for k in ("combat", "player", "hand", "piles", "enemies",
                                                                  "turn_outlook")}
        elif v.kind == "rest_site":
            self._rest_checkpoint(v)
        elif v.kind == "rewards":
            self._note_potion_drop(v)

    def _counter_note(self, relic: dict) -> str | None:
        """What a relic's vanished counter means, as an observation; None while it shows one or never did."""
        last = self._relic_counters.get(str(relic.get("id")))
        if last is None or relic.get("counter") is not None:
            return None
        return (f"OBSERVED: this relic showed a counter earlier in the run (last seen {last}) and shows none now. "
                "For an effect its text limits to a number of uses, those uses are spent.")

    def _reward_routine(self, v: ScreenView) -> tuple | None:
        """(candidate, reason) for the next step on a rewards screen that needs no judgment, else None.

        Gold, relics and a potion with a free slot cost nothing to take, and opening a card reward only
        shows its cards. Relics go before potions so a relic that adds slots counts. A card reward that
        was opened and skipped stays in the game's list; it is never opened again. What is left for the
        model is a potion with a full belt and reward kinds that are a choice in themselves."""
        if v.kind != "rewards" or not v.ready or v.unsupported or v.terminal:
            return None
        details = v.facts.get("screen_details") or {}
        items = details.get("items") or []
        if any(x.get("index") is None for x in items):
            return None
        by_id = {c["action_id"]: c for c in v.candidates}

        def claim(x):
            return by_id.get(f"reward_{x['index']}")
        for kind, why in (("gold", "Gold costs nothing to take."), ("relic", "A reward relic costs nothing to take.")):
            x = next((x for x in items if x.get("type") == kind and claim(x)), None)
            if x:
                return claim(x), why
        held, slots = v.facts.get("potions"), v.facts.get("max_potion_slots")
        x = next((x for x in items if x.get("type") == "potion" and claim(x)), None)
        if x and isinstance(held, list) and type(slots) is int and len(held) < slots:
            return claim(x), "A potion slot is free."
        cards = [x for x in items if x.get("type") == "card" and claim(x)]
        if len(cards) > self._skipped_card_rewards:
            return claim(cards[self._skipped_card_rewards]), "Opening a card reward only shows its cards."
        if (cards and len(cards) == len(items) and details.get("can_proceed") is True and "proceed" in by_id):
            return by_id["proceed"], "The card reward left here was already seen and skipped."
        return None

    def _balance_rejects(self, v: ScreenView) -> dict:
        """Offered cards that would only add to a role the deck already holds too many of, by action_id.

        They are withheld from the model at card rewards and shops: left to its judgment, nearly
        every such card found a reason to be taken. A card reward keeps them unless it can be skipped."""
        if (v.kind not in ("card_reward", "shop") or v.in_combat or self._handoff or self._fight_overlay(v)
                or v.facts.get("combat_context")):
            return {}
        if v.kind == "card_reward" and not any(c["kind"] == "skip_card_reward" for c in v.candidates):
            return {}
        run = v.facts.get("run") or {}
        state = deck_balance.balance(v.facts.get("deck"), run.get("act"), run.get("character"))
        if not state:
            return {}
        offers = deck_balance.offer_effects(state, [
            (c["action_id"], c["details"]) for c in v.candidates
            if c["kind"] == "select_card_reward"
            or (c["kind"] == "shop_purchase" and c["details"].get("category") == "card")])
        return {o["action_id"]: o for o in offers if o["effect"] == "worsens"}

    def _hidden_candidates(self, v: ScreenView) -> dict:
        """Candidates withheld from the model on this screen: action_id -> the limitation line that says why."""
        hidden = {}
        if v.kind == "rewards" and self._skipped_card_rewards:
            cards = [c for c in v.candidates if c["kind"] == "claim_reward" and c["details"].get("type") == "card"]
            for c in cards[:self._skipped_card_rewards]:
                hidden[c["action_id"]] = ("A card reward listed on this screen is not offered: you already opened it "
                                          "here and skipped its cards.")
        rejects = self._balance_rejects(v)
        if rejects:
            line = ("Not offered, because each adds only to a role this deck already holds too many of "
                    "(current_facts.deck_balance): "
                    + "; ".join(f"{o['name']} ({', '.join(o['adds_to_over_supplied'])})" for o in rejects.values()) + ".")
            hidden.update({aid: line for aid in rejects})
        return hidden

    def _balance_skip(self, v: ScreenView) -> tuple | None:
        """(skip candidate, reason) when every card of a card reward breaks the deck's balance."""
        if v.kind != "card_reward" or not v.ready:
            return None
        cards = [c for c in v.candidates if c["kind"] == "select_card_reward"]
        rejects = self._balance_rejects(v)
        if not cards or any(c["action_id"] not in rejects for c in cards):
            return None
        skip = next(c for c in v.candidates if c["kind"] == "skip_card_reward")
        return skip, ("Every offered card adds only to a role the deck already holds too many of: "
                      + "; ".join(f"{o['name']} ({', '.join(o['adds_to_over_supplied'])})".replace("_", " ")
                                  for o in rejects.values()) + ".")

    def _note_potion_drop(self, v: ScreenView):
        """Whether the fight just won dropped a potion, read from its first rewards screen (the drop counter)."""
        last = self.route_outcomes[-1] if self.route_outcomes else None
        run = v.facts.get("run") or {}
        if (last is None or "potion_reward" not in last or last["potion_reward"] is not None
                or last.get("result") == "defeat" or (last.get("act"), last.get("floor")) != (run.get("act"), run.get("floor"))):
            return
        items = (v.facts.get("screen_details") or {}).get("items") or []
        last["potion_reward"] = any(x.get("type") == "potion" for x in items)

    def _finish_combat(self, after: ScreenView, result: str, note: str | None = None):
        """``note`` leads the recorded notes, ahead of the HP and potion accounting, so the length cap keeps it."""
        c, self.combat = self.combat, None
        if c is None:
            return
        player = (after.facts.get("player") or {}) if after.facts else {}
        end_hp, max_hp = player.get("hp"), player.get("max_hp")
        if result == "defeat" and (end_hp is None or end_hp > 0):
            end_hp = 0
        change = None if (c.start_hp is None or end_hp is None) else end_hp - c.start_hp
        lost = None if change is None else max(0, -change)
        c.note_hp(end_hp)   # the last step: a killing blow, or healing on the way to this screen
        damage, healed = (c.damage, c.healed) if c.hp_tracked and c.last_hp is not None else (None, None)
        ordinal = self.hallway_done.get(c.act, 0) + 1
        ref = self.risk_ref if self.risk_ref and self.risk_ref["act"] == c.act else None
        cls = node_class(c.encounter_type, ordinal, ref["early_k"]) if ref else None
        if c.encounter_type == "monster" and c.act is not None:
            self.hallway_done[c.act] = ordinal
        outcome = {"encounter_type": c.encounter_type, "result": result, "act": c.act, "floor": c.floor,
                   "hp_lost": lost, "hp_lost_pct": round(lost / max_hp * 100, 1) if lost is not None and max_hp else None,
                   "hp_damage": damage, "hp_healed": healed,
                   "hp_damage_pct": round(damage / max_hp * 100, 1) if damage is not None and max_hp else None,
                   "node_class": cls, "expected_pct": (ref["classes"].get(cls) or {}).get("mean_pct") if cls else None,
                   "expected_p90_pct": (ref["classes"].get(cls) or {}).get("p90_pct") if cls else None,
                   "potion_reward": None}   # set from the rewards screen that follows (potion drop counter)
        # What the pilot did with the potions is visible only as slots that emptied during the fight.
        left = _potion_names(after.facts) if after.facts else None
        if c.potions_held is None or left is None:
            potions = "potions held before and after the fight were not both observed"
        else:
            gone = list(c.potions_held)
            for name in left:
                if name in gone:
                    gone.remove(name)
            potions = (f"potions held at the start {c.potions_held or 'none'}, no longer held after it "
                       f"{gone or 'none'}")
        item = {"id": f"combat-{c.key}", "encounter_type": c.encounter_type, "result": result,
                "hp_lost": lost, "turns": c.rounds,
                "notes": ((note + " " if note else "") +
                          f"OBSERVED: net HP change {change if change is not None else 'unknown'} measured on the "
                          f"first post-combat screen (includes end-of-combat effects); HP removed during the fight "
                          f"{damage if damage is not None else 'unknown'}, HP regained "
                          f"{healed if healed is not None else 'unknown'} (between observations, so lower bounds); "
                          f"{potions}; potions sent by this agent: {c.potions_used}.")[:500]}
        self.recent_outcomes.append(item)
        self.route_outcomes.append(outcome)
        self.pending_combat_finished = True
        self.stats.combats_finished += 1
        if self.rec:
            self.rec.event("combat_finished", item)
            self.rec.event("route_calibration", outcome)
        self.hooks.combat_finished(item, outcome)

    def _rest_checkpoint(self, v: ScreenView):
        """Projected vs. actual HP on arrival at a rest site the stored route plan expected here."""
        plan, run, pl = self.route_plan, v.facts.get("run") or {}, v.facts.get("player") or {}
        floor = run.get("floor")
        if (not plan or not plan.get("trajectory") or plan.get("act") != run.get("act") or floor is None
                or plan.get("floor") is None or not pl.get("max_hp") or pl.get("hp") is None
                or any(c["floor"] == floor and c["act"] == run.get("act") for c in self.rest_checkpoints)):
            return
        i = floor - plan["floor"]   # the plan was stored on the map before node 1
        steps = plan["trajectory"]
        if not 1 <= i <= len(steps) or steps[i - 1].get("type") != "rest_site":
            return
        expected = steps[i - 2].get("expected_hp_pct") if i >= 2 else plan.get("hp_pct")
        item = {"act": run.get("act"), "floor": floor, "actual_arrival_hp_pct": round(pl["hp"] / pl["max_hp"] * 100, 1),
                "projected_arrival_hp_pct": None if expected is None else round(expected, 1),
                "plan_set_at_floor": plan["floor"]}
        self.rest_checkpoints.append(item)
        if self.rec:
            self.rec.event("route_rest_checkpoint", item)

    # ---------------------------------------------------------- map memory
    def _remember_map(self, v: ScreenView):
        """Keep the act graph of the latest map screen; rooms do not show it.

        An ancient shows the same graph read-only before the act's first map screen, so the map (and the
        route planned with the ancient's choice) is known from the act's opening screen on."""
        d, run = v.facts.get("screen_details") or {}, v.facts.get("run") or {}
        if v.kind != "map":
            d = d.get("map") if isinstance(d.get("map"), dict) else {}
            known = self.map_memory
            if known and known.get("act") == run.get("act") and known.get("nodes"):
                return   # this act's map is already held; only a map screen refreshes it and its travel state
        if not d.get("nodes"):
            return
        self._settle_ancient_plan(v)
        self.map_memory = {"act": run.get("act"), "floor": run.get("floor"),
                           "nodes": [{"col": n["col"], "row": n["row"], "type": n["type"], "children": n["children"]}
                                     for n in d["nodes"]],
                           "bosses": list(d.get("bosses") or []), "chosen": None,
                           # For the interface's map drawing only; route logic does not read these.
                           "visited": [p for p in d.get("visited") or [] if p],
                           "current": d.get("current_position")}
        self._publish_route()

    def _settle_ancient_plan(self, v: ScreenView):
        """A route planned with an ancient's choice was stored before that relic existed. On the map
        screen that follows, the state the relic left becomes the plan's baseline, so the relic it
        was made for (and that relic's HP or gold cost) is not read back as a reason to re-plan."""
        plan = self.route_plan
        if v.kind != "map" or not plan or not plan.get("made_with_ancient_choice"):
            return
        del plan["made_with_ancient_choice"]
        player = v.facts.get("player") or {}
        if plan.get("act") != (v.facts.get("run") or {}).get("act"):
            return
        hp, max_hp = player.get("hp"), player.get("max_hp")
        plan.update(relics=sorted(str(r.get("id") or r.get("name")) for r in v.facts.get("relics") or []),
                    potions=[str(p.get("id") or p.get("name")) for p in v.facts.get("potions") or []],
                    gold=player.get("gold"), hp_pct=hp / max_hp * 100 if hp is not None and max_hp else None)

    def _act_boss(self, v: ScreenView) -> dict | None:
        """This act's boss as the act map shows it, with its static reference data.

        Unlike ``_route_context`` this holds on every screen of the act, the map included: the
        boss does not depend on the room. The name is an observed fact; the reference entry is
        static data for one game build and is None when the monster is not in it.
        """
        run, d = v.facts.get("run") or {}, v.facts.get("screen_details") or {}
        shown = d.get("bosses") or (d["map"].get("bosses") if isinstance(d.get("map"), dict) else None)
        if shown and run.get("act") is not None:
            self.act_bosses = {"act": run["act"], "bosses": [{"id": b.get("id"), "name": b.get("name")}
                                                             for b in shown if isinstance(b, dict)]}
        known = self.act_bosses
        if not known or known["act"] != run.get("act"):
            m = self.map_memory   # restored from the run state after a restart
            if not m or m.get("act") != run.get("act") or not m.get("bosses"):
                return None
            known = {"act": m["act"], "bosses": [{"id": b.get("id"), "name": b.get("name")} for b in m["bosses"]]}
        ref = enemy_patterns.active()
        bosses = []
        for b in known["bosses"]:
            monsters = (ref.encounter_profiles(encounter_id=b["id"], name=b["name"], ascension=run.get("ascension"))
                        if ref else None)
            bosses.append({**b, "monsters": monsters, "deck_demands": boss_demands.fight_demands(monsters, b["id"])})
        return {
            "label": "ACT BOSS, as named on this act's map (observed). monsters lists every monster of that boss "
                     "fight (a boss can be several monsters fought together; count marks repeats) as static data "
                     "for one game build with base values at this ascension, not an observation of the fight. "
                     "monsters null means the boss is not in the reference data; missing true means that one "
                     "monster is not. deck_demands say what this fight asks of the deck: the capability (need), "
                     "the kind of card that supplies it (take) and why, which is either the data a rule read or, "
                     "where it starts with \"Owner's guidance\", what the person running this agent knows about "
                     "the fight and the reference data omits. They ignore relics and potions; null means unknown, "
                     "an empty list that nothing known asks for a special deck.",
            "bosses": bosses}

    def _route_context(self, v: ScreenView) -> dict | None:
        """What the map leads to after the current room, from the last observed map.

        Only for the room the agent travelled to from that map (one floor later, same act);
        anything else is unknown and returns None. Counts are ranges over every distinct
        continuation, so they hold whichever branch is taken next.
        """
        m, run = self.map_memory, v.facts.get("run") or {}
        if (v.in_combat or v.kind == "map" or not m or not m.get("chosen") or m.get("act") != run.get("act")
                or type(m.get("floor")) is not int or run.get("floor") != m["floor"] + 1):
            return None
        here = (m["chosen"]["col"], m["chosen"]["row"])
        node = next((n for n in m["nodes"] if (n["col"], n["row"]) == here), None)
        types = {(n["col"], n["row"]): n["type"] for n in m["nodes"]}
        boss_at = {(b.get("col"), b.get("row")) for b in m["bosses"]}
        nxt = [tuple(c) for c in (node or {}).get("children") or [] if len(c) == 2 and
               (tuple(c) in types or tuple(c) in boss_at)]
        if node is None or (not nxt and not m["bosses"]):
            return None
        options = [{"col": c, "row": r, "type": "boss" if (c, r) in boss_at else types[(c, r)], "preview": True}
                   for c, r in nxt]
        routes, truncated, _ = enumerate_routes({"nodes": m["nodes"], "bosses": m["bosses"],
                                                 "next_options": options}, max_routes=400)
        paths = [[t for t, _, _ in path] for _, path in routes] or [["boss"]]   # no children: the boss is next

        def before_rest(path):
            return path[:path.index("rest_site")] if "rest_site" in path else [t for t in path if t != "boss"]

        def span(values):
            return {"min": min(values), "max": max(values)}
        elite_first = [("elite" in before_rest(p)) for p in paths]
        ctx = {
            "label": "OBSERVED MAP, as shown on the map screen before this room: which rooms follow is fact; "
                     "the stored route plan and its HP projection are your own earlier hypotheses",
            "current_room": m["chosen"]["type"],
            "act_boss": [{"id": b.get("id"), "name": b.get("name")} for b in m["bosses"]],
            "next_nodes": sorted({p[0] for p in paths}),
            "next_is_boss": all(p[0] == "boss" for p in paths),
            "ahead_over_all_continuations": {
                "distinct_routes": len(paths), "truncated": truncated,
                "rooms_before_boss": span([sum(t != "boss" for t in p) for p in paths]),
                "rest_sites_before_boss": span([p.count("rest_site") for p in paths]),
                "shops_before_boss": span([p.count("shop") for p in paths]),
                "elites_before_boss": span([p.count("elite") for p in paths]),
                "fights_before_next_rest_or_boss": span([sum(t in ("monster", "elite") for t in before_rest(p))
                                                         for p in paths]),
                "elite_before_next_rest_or_boss": ("on_every_route" if all(elite_first) else
                                                   "on_some_routes" if any(elite_first) else "on_no_route"),
                "note": "? rooms can also be fights; they are not counted as fights here."}}
        plan = self.route_plan
        if plan and plan.get("act") == run.get("act") and plan.get("trajectory"):
            coords = [tuple(c) for c in plan.get("coords") or []]
            if here in coords:
                rest = plan["trajectory"][coords.index(here) + 1:]
                ctx["stored_route_plan"] = {
                    "status": "this room is on the stored plan",
                    "remaining_rooms": [{"type": step.get("type"), "projected_hp_pct_after": step.get("expected_hp_pct")}
                                        for step in rest],
                    "projection_note": "Projected before this room from pooled players' fights; compare with your "
                                       "current HP rather than trusting it.",
                    "plan": plan.get("plan"), "change_course_if": plan.get("change_course_if")}
            else:
                ctx["stored_route_plan"] = {"status": "this room is not on the stored plan; its projections do not apply"}
        return ctx

    # ---------------------------------------------------------- strategy
    def _strategic_facts(self, v: ScreenView) -> dict:
        f = v.facts
        run, pl = f.get("run") or {}, f.get("player") or {}
        relics = None
        if f.get("relics") is not None:
            seen, items = {}, []
            for r in f["relics"]:
                rid = r.get("id") or r.get("name") or "UNKNOWN"
                seen[rid] = seen.get(rid, 0) + 1
                spent = self._counter_note(r)
                items.append({"id": rid if seen[rid] == 1 else f"{rid}#{seen[rid]}", "name": r.get("name") or rid,
                              "description": _clip((r.get("description") or "") + (f" [{spent}]" if spent else ""))
                              if spent or r.get("description") is not None else None,
                              "counter": r.get("counter")})
            relics = {"items": items}
        potions = None
        if f.get("potions") is not None:
            items = [{"slot": p["slot"], "id": p.get("id") or "UNKNOWN", "name": p.get("name") or "?",
                      "description": _clip(p.get("description"))} for p in f["potions"] if p.get("slot") is not None]
            slots = f.get("max_potion_slots")
            potions = {"slots": slots if slots is not None and slots >= len(items) and
                       all(i["slot"] < slots for i in items) else None, "items": items}
        deck = None  # only the mod-supplied permanent deck; never rebuilt from combat piles
        if isinstance(f.get("deck"), list):
            seen, cards = {}, []
            for c in f["deck"]:
                key = str(c.get("id") or "UNKNOWN")[:70] + ("+" if c.get("upgraded") else "")
                seen[key] = seen.get(key, 0) + 1
                cards.append({"id": key if seen[key] == 1 else f"{key}#{seen[key]}",
                              "name": _clip(c.get("name") or key, 120), "count": c["count"],
                              "upgraded": c.get("upgraded"),
                              "cost": c["cost"][:8] if isinstance(c.get("cost"), str) else None,
                              "type": _clip(c.get("type"), 80), "description": _clip(c.get("description"))})
            deck = {"cards": cards[:200]}
        opps = None
        if not v.in_combat:
            items = []
            for c in v.candidates:
                kind = _OPP_KIND.get(c["kind"])
                if c["kind"] == "claim_reward":
                    kind = _REWARD_KIND.get(c["details"].get("type"), "other")
                elif c["kind"] == "shop_purchase":
                    kind = "card_removal" if c["details"].get("category") == "card_removal" else "shop_item"
                if kind:
                    items.append({"id": f"{v.kind}:{c['action_id']}", "kind": kind, "description": _clip(c["description"]),
                                  "details": _clip(json.dumps(c["details"], ensure_ascii=False, sort_keys=True))})
            ahead = self._route_context(v)
            if ahead:   # not a choice on this screen: the observed map beyond this room, boss included
                bosses = ", ".join(str(b.get("name") or b.get("id")) for b in ahead["act_boss"]) or "unknown"
                items = items[:39] + [{
                    "id": "map_after_this_room", "kind": "route",
                    "description": _clip(f"Observed map after this room: next {', '.join(ahead['next_nodes'])}; "
                                         f"act boss {bosses}."),
                    "details": _clip(json.dumps({k: ahead[k] for k in ("next_is_boss", "ahead_over_all_continuations")},
                                                ensure_ascii=False, sort_keys=True))}]
            boss = self._act_boss(v)   # not a choice either: who ends the act, on every screen including the map
            extra = []   # one entry per monster of each boss fight, so each fits the bounded details field
            for i, b in enumerate((boss or {}).get("bosses") or []):
                key, name = f"act_boss:{b['id'] or b['name'] or i}", b["name"] or b["id"]
                members = b["monsters"] or [None]
                for j, m in enumerate(members):
                    several = len(members) > 1
                    who = ("" if m is None or not several else
                           f" Monster {j + 1} of {len(members)} in that fight, fought together: "
                           f"{m.get('name')}" + (f" x{m['count']}" if m.get("count") else "") + ".")
                    extra.append({
                        "id": (f"{key}:{j + 1}" if several else key)[:80], "kind": "encounter",
                        "description": _clip(f"Act boss {name} (not a choice on this screen), as named on this "
                                             f"act's map.{who}"),
                        "details": _clip("No reference data for it: its monsters and moves are unknown." if m is None
                                         else "Static reference data, base values at this ascension: "
                                         + enemy_patterns.profile_text(m))})
                if b.get("deck_demands") is not None:   # first, so the cap below never drops it
                    extra.insert(i, {
                        "id": f"{key}:deck"[:80], "kind": "encounter",
                        "description": _clip(f"What act boss {name} asks of the deck built during this act "
                                             "(not a choice on this screen)."),
                        "details": _clip(boss_demands.demand_text(b.get("deck_demands")))})
            extra = extra[:8]
            state = deck_balance.balance(f.get("deck"), run.get("act"), run.get("character"))
            if state:   # not a choice either: what the deck holds too little and too much of
                extra.insert(0, {"id": "deck_balance", "kind": "other",
                                 "description": "Balance of the current deck over damage, Block and the scaling of "
                                                "each (not a choice on this screen).",
                                 "details": _clip(deck_balance.summary_text(state))})
            items = items[:40 - len(extra)] + extra
            opps = {"items": items[:40]}
        glossary = f.get("keyword_glossary")
        return {
            # Bounded: the snapshot has a size limit and the planner needs definitions, not prose.
            **({"glossary": {"items": [{"term": _clip(k, 120), "definition": _clip(d, 240)}
                                       for k, d in list(glossary.items())[:40]]}} if glossary else {}),
            "run": {"character": run.get("character"), "difficulty": run.get("difficulty"), "act": run.get("act"),
                    "floor": run.get("floor"), "encounter_type": v.state_type},
            "resources": {"hp": pl.get("hp"), "max_hp": pl.get("max_hp"), "gold": pl.get("gold"),
                          "max_energy": pl.get("max_energy"),
                          "last_observed_max_energy": (self.energy_observation or {}).get("max_energy")},
            "deck": deck,
            "relics": relics, "potions": potions, "opportunities": opps,
            "recent_outcomes": {"items": list(self.recent_outcomes)},
        }

    def _maybe_refresh_strategy(self, v: ScreenView):
        if self.planner is None:
            return
        if (v.in_combat or self._fight_overlay(v) or self._handoff) and self.strat_revision > 0:
            return  # ordinary card plays never trigger a refresh; refresh after the combat ends
        details = v.facts.get("screen_details") or {}
        can_wait = (v.kind in ("rewards", "card_reward", "card_select", "treasure", "rest_site")
                    # an ancient opens the act and plans its route; any other event is one bounded choice
                    or (v.kind == "event" and details.get("is_ancient") is not True)
                    # a map with one way on decides no route (at most how to use the fire it leads to)
                    or (v.kind == "map" and sum(c["kind"] == "choose_map_node" for c in v.candidates) == 1))
        if (self.planner.assessment is not None and self.strat_facts is not None and can_wait
                and (v.facts.get("run") or {}).get("character") == self.strat_facts["run"]["character"]
                and (v.facts.get("run") or {}).get("act") == self.strat_facts["run"]["act"]):
            # Fold the fight outcome, reward claims and drafted/changed cards into
            # one refresh at the next strategic choice. Preserve the old facts and
            # pending outcome so the refresh still sees every accumulated change.
            return
        facts = self._strategic_facts(v)
        place = (facts["run"]["act"], facts["run"]["floor"])
        if (self.strat_revision > 0 and not self.pending_combat_finished and self.strat_facts is not None
                and self.planner.assessment is not None and place == self.strat_place and None not in place
                and facts["deck"] == self.strat_facts["deck"]):
            # One plan per floor while the deck is unchanged: the plan made before this floor's
            # choice (an ancient's relic above all) already weighed it, and the frame shows the result.
            return
        route_key = ((facts["run"]["act"], facts["run"]["floor"],
                      tuple((c["details"].get("col"), c["details"].get("row"))
                            for c in v.candidates if c["kind"] == "choose_map_node"))
                     if v.kind == "map" and sum(c["kind"] == "choose_map_node" for c in v.candidates) > 1 else None)
        if self.strat_revision > 0 and not self.pending_combat_finished:
            old = self.strat_facts
            relics = lambda f: [(r["id"], r["description"]) for r in (f.get("relics") or {}).get("items", [])]
            meaningful = ((facts["deck"] is not None and facts["deck"] != old["deck"])
                          or (facts["relics"] is not None and relics(facts) != relics(old))
                          or facts["run"]["character"] != old["run"]["character"]
                          or facts["run"]["act"] != old["run"]["act"]
                          or facts["resources"]["max_hp"] != old["resources"]["max_hp"]
                          or (facts["resources"]["max_energy"] is not None and
                              facts["resources"]["max_energy"] != old["resources"]["max_energy"])
                          or (route_key is not None and route_key != self.strat_route_key))
            if not meaningful:
                return
        reasons = _change_reasons(self.strat_facts, facts, initial=self.strat_revision == 0,
                                  combat_finished=self.pending_combat_finished)
        self.strat_revision += 1
        try:
            snap = StrategicSnapshot(run_id=self.run_id, revision=self.strat_revision, data_source=self.facts_source,
                                     facts=facts, change_reasons=reasons)
        except ValueError as e:
            self.stats.strategic_failures += 1
            if self.rec:
                self.rec.error("strategic_snapshot_invalid", {"error": str(e)[:300]})
            self.last_planner_result = None
            self.strat_facts, self.pending_combat_finished = facts, False
            return
        self.hooks.step("strategy", f"Refreshing the strategic assessment (revision {snap.revision})",
                        reasons=list(reasons))
        result = self.planner.update(snap)
        self.last_progress = self.clock()   # model latency is not a game stall
        self.hooks.strategy(result, list(reasons))
        self.last_planner_result = result
        self.strat_facts, self.pending_combat_finished = facts, False
        self.strat_place = place
        if route_key is not None:
            self.strat_route_key = route_key
        if result.model_called:
            self.stats.strategic_updates += 1
        if result.status == "failed":
            self.stats.strategic_failures += 1
        if self.rec:
            self.rec.record("strategic_update", revision=snap.revision, snapshot_id=snap.snapshot_id,
                            change_reasons=list(reasons), status=result.status, model_called=result.model_called,
                            diagnostic=result.diagnostic,
                            assessment=result.assessment.to_dict() if result.assessment else None,
                            applies_to_current_facts=result.applies_to_current_facts,
                            call=_last_call(self.policy.model),
                            attempts=self.planner.last_attempts if result.model_called else [])

    def _assessment_for_frame(self, v: ScreenView) -> dict | None:
        if self.planner is None:
            return {"status": "planner_disabled"}
        a = self.planner.assessment
        pr = self.last_planner_result
        if a is None:
            return {"status": "assessment_unavailable",
                    "diagnostic": (pr.diagnostic if pr else "no strategic update attempted yet")}
        base = {"label": "REVISABLE MODEL JUDGMENT - NOT FACT", "based_on_revision": a.input_revision,
                "snapshot_id": a.snapshot_id, "content": a.content}
        if pr is not None and pr.current_assessment is a:
            if v.in_combat:
                status = "current_in_combat_not_refreshed"
            elif self._strategic_facts(v) == self.strat_facts:
                status = "current"
            else:
                status = "reused_between_refreshes"
                base["note"] = ("Full refresh deferred until the next route, shop or act-opening choice. Current facts, "
                                "recent fight outcomes and candidates override old deck, relics, HP, gold, potions "
                                "and opportunities; assess newly revealed offers directly and revise the plan if needed.")
        else:
            status = "historical"
            base["note"] = ("The latest strategic update failed or facts changed; this assessment is from an older "
                            "revision and may not match current facts.")
        return {"status": status, **base}

    # ---------------------------------------------------------- one decision
    def _frame(self, v: ScreenView) -> DecisionFrame:
        cands = [c for c in v.candidates if (v.fingerprint, c["action_id"]) not in self.stats.blocked
                 and not any((v.fingerprint, aid) in self.stats.blocked
                             for aid in v.bindings[c["action_id"]].get("steps", []))]
        blocked = [c["action_id"] for c in v.candidates if (v.fingerprint, c["action_id"]) in self.stats.blocked]
        lims = list(v.limitations)
        if blocked:
            lims.append(f"Not offered because the game rejected them in this exact state: {blocked}")
        hidden = self._hidden_candidates(v)
        if hidden:
            cands = [c for c in cands if c["action_id"] not in hidden
                     and not any(aid in hidden for aid in v.bindings[c["action_id"]].get("steps", []))]
            lims.extend(sorted(set(hidden.values())))
        cap = CAPABILITIES.get(v.kind, {})
        overlay = self._fight_overlay(v) or self._handoff
        boss = None if v.in_combat or overlay else self._act_boss(v)
        support = None
        if self.expert_stats is not None:
            started = self.clock()
            self.hooks.step("evidence", f"Collecting expert evidence for the {v.kind} screen")
            support = self.expert_stats.for_view(
                v, route_state={"plan": self.route_plan, "outcomes": self.route_outcomes,
                                # The route model spends early only for Mentor's packaged advisor.
                                "combat_delegated": self.autobattle is not None and not self.own_pilot}, act_boss=boss)
            self.last_progress += self.clock() - started  # evidence latency is not a game stall
            self._note_support(v, support)
            self.hooks.evidence(v.kind, support)
            if hidden and isinstance(support, dict) and isinstance(support.get("options"), list):
                support = {**support, "options": [o for o in support["options"]
                                                  if not isinstance(o, dict) or o.get("action_id") not in hidden]}
        facts = v.facts
        notes = {str(r.get("id")): self._counter_note(r) for r in facts.get("relics") or []}
        if any(notes.values()):
            facts = {**facts, "relics": [{**r, "counter_note": notes[str(r.get("id"))]}
                                         if notes.get(str(r.get("id"))) else r for r in facts["relics"]]}
        if self.treasures_opened and not v.in_combat and not overlay:
            facts = {**facts, "run_progress": {"treasure_chests_opened_this_run": self.treasures_opened}}
        if self.pending_combat_finished:
            facts = {**facts, "recent_combat_outcomes": {
                "label": "Observed fight results since the plan may have last been refreshed.",
                "items": list(self.recent_outcomes)}}
        if self.energy_observation is not None:
            facts = {**facts, "energy_observation": {**self.energy_observation,
                     "note": "Last observed maximum, not current energy or guaranteed future capacity. "
                             "Current max_energy remains unknown when null; later effects can change capacity."}}
        ahead = self._route_context(v)
        if ahead:
            facts = {**facts, "route_context": ahead}
        mechanics = _card_zone_constraints(v)
        if mechanics:
            support = {**(support or {}), "card_zone_constraints": mechanics}
        if v.kind == "shop":
            if self.shop_entry is None or self.shop_entry["encounter_key"] != v.encounter_key:
                self.shop_entry = {"encounter_key": v.encounter_key,
                                   "gold": (v.facts.get("player") or {}).get("gold")}
            support = {**(support or {}), "shop_budget": _shop_packages(v, self.shop_entry["gold"], hidden)}
        if v.kind == "map":
            spending = _spending_outlook(v, support)
            if spending:
                support = {**(support or {}), "spending": spending}
        if boss:   # every screen between fights, the map included
            facts = {**facts, "act_boss": boss}
        if v.kind in _DECK_CHOICE_KINDS and not v.in_combat and not overlay:
            run = v.facts.get("run") or {}
            state = deck_balance.balance(v.facts.get("deck"), run.get("act"), run.get("character"))
            if state:   # the deck as it stands, then what each card offered here would do to it
                facts = {**facts, "deck_balance": state}
                offered = deck_balance.offer_effects(state, [
                    (c["action_id"], c["details"]) for c in cands
                    if c["kind"] == "select_card_reward"
                    or (c["kind"] == "shop_purchase" and c["details"].get("category") == "card")])
                if offered:
                    facts = {**facts, "deck_balance_offers": offered}
            if budget.active() and v.kind in ("card_reward", "shop"):
                # Whether this position can afford a card that waits for its enabler: checked here, not left to the model.
                facts = {**facts, "payoff_bet": budget.bet_window(state, run, v.facts.get("player"), ahead)}
        if budget.active() and v.kind in ("map", "rest_site") and not v.in_combat and not overlay:
            costs = budget.hp_costs(v.facts.get("deck"), v.facts.get("relics"))
            if costs:   # HP the deck pays on purpose comes back only at rest sites: part of rest and route choices
                facts = {**facts, "hp_costs": costs}
        if self._handoff:
            lims.append("AUTOBATTLE HANDOFF: an enemy's action opened this offer during its turn. Spire Mentor is "
                        "playing this fight and is holding it for this one choice, because it has no fight search for "
                        "an enemy-opened offer. Read each option's own text for what it does. A choice an enemy forces "
                        "is normally a cost to you in this fight, not a card worth owning: do not score it as a deck "
                        "pick. Compare what each option costs over the turns this fight is likely to last with your "
                        "current HP, Block and draw (facts.combat_context). Mentor resumes after your choice.")
        if overlay:
            lims.append("This picker opened during a fight that is still in progress: choose with the enemy intent, "
                        "your HP, Block and energy in mind (facts.combat_context).")
            if "combat_context" not in facts and self.last_combat_facts:
                facts = {**facts, "combat_context": {
                    "label": "LAST OBSERVED COMBAT STATE BEFORE THIS PICKER OPENED; the fight is still on",
                    **self.last_combat_facts}}
        if (v.in_combat or overlay) and self.incoming_changes:
            facts = {**facts, "observed_incoming_changes": {
                "label": "OBSERVED IN THIS ENCOUNTER: displayed incoming damage before and after your own actions",
                "items": self.incoming_changes[-12:]}}
        return DecisionFrame(
            run_id=self.run_id, run_id_source=self.run_id_source, objective=self.objective,
            screen={"kind": v.kind, "state_type": v.state_type,
                    "encounter_key": self.encounter_key if overlay else v.encounter_key,
                    **({"during_combat": True} if overlay else {}),
                    **({"autobattle_handoff": True} if self._handoff else {}),
                    # Who plays the fights: the prompt prices potions and per-play costs for that pilot.
                    **({"combat_pilot": OWN_PILOT if self.own_pilot else MENTOR_PILOT} if self.autobattle is not None and not overlay
                       else {}),
                    "capability": {"status": cap.get("status"), "live_validated": False, "mod_revision": MOD_REVISION}},
            observation_fingerprint=v.fingerprint, facts_source=self.facts_source, facts=facts,
            candidates=cands, bindings={c["action_id"]: v.bindings[c["action_id"]] for c in cands},
            limitations=lims, assessment=self._assessment_for_frame(v),
            previous_turn_intent=self.turn_intent, history=self.history, decision_support=support)

    def _note_support(self, v: ScreenView, support):
        """Keep what later bookkeeping needs from the evidence: class losses and coverage counters."""
        if not isinstance(support, dict):
            return
        risk = support.get("hp_risk")
        if v.kind == "map" and isinstance(risk, dict) and isinstance(risk.get("classes"), dict):
            self.risk_ref = {"act": (v.facts.get("run") or {}).get("act"), "early_k": risk.get("early_hallway_fights"),
                             "classes": {k: {"mean_pct": c.get("mean_pct"), "p90_pct": c.get("p90_pct")}
                                         for k, c in risk["classes"].items()}}
        cov = support.get("evidence_coverage")
        seen = (v.encounter_key, v.kind)
        if isinstance(cov, dict) and seen not in self._coverage_seen:   # once per screen visit, not per re-render
            self._coverage_seen.add(seen)
            total = self.coverage.setdefault(v.kind, {"screens": 0})
            total["screens"] += 1
            for k, val in cov.items():
                if isinstance(val, dict):
                    bucket = total.setdefault(k, {})
                    for rung, n in val.items():
                        bucket[rung] = bucket.get(rung, 0) + n
                elif k != "min_samples" and type(val) is int:
                    total[k] = total.get(k, 0) + val

    def _step(self):
        v = self._wait_ready()
        if self._restore_pending:
            self._restore_pending = False
            self._load_state(v)
        self._track_encounter(v)
        if v.kind == "map" or (v.kind == "event" and not v.in_combat
                               and isinstance((v.facts.get("screen_details") or {}).get("map"), dict)):
            self._remember_map(v)   # an ancient shows the act map read-only, before the first map screen
        self._decide(v)

    def _decide(self, v: ScreenView) -> bool | None:
        """One decision on a ready screen. True when a single action was sent and resolved."""
        # Forced progress needs neither strategy, decision support nor a model call.
        # Classify before rejection filtering so a rejected alternative cannot
        # turn a real choice into an automatic click.
        cand = automatic_action(v)
        # The grid needed more cards until the toggle this agent just sent; that toggle completed the
        # selection the model chose. Asking again can only undo it or select one card too many, which
        # leaves a grid whose Confirm does nothing (observed live on a single-card enchant grid).
        completed = cand is None and self._selection_complete == v.fingerprint
        self._selection_complete = None
        if completed:
            cand = next((c for c in v.candidates if c["kind"] == "confirm_selection"), None)
        # The card to upgrade was chosen together with Smith; the grid that Smith opens is not a new decision.
        smith = self._smith_target(v)
        if cand is None:
            cand = smith
        # Reward collection and a card reward whose every card breaks the deck's balance are not judgments.
        routine = None
        if cand is None and not self._handoff:
            for source, found in (("reward_routine", self._reward_routine(v) if self.reward_routine else None),
                                  ("deck_balance", self._balance_skip(v))):
                if found and (v.fingerprint, found[0]["action_id"]) not in self.stats.blocked:
                    routine, cand = (source, found[1]), found[0]
                    break
        if cand is not None:
            aid = cand["action_id"]
            if (v.fingerprint, aid) in self.stats.blocked:
                raise _Stop(STOP_REPEATED_REJECTIONS, "forced progress action was rejected in this state", v)
            source = (routine[0] if routine else "smith_plan" if cand is smith else
                      "single_map_destination" if v.kind == "map" else "forced_progress")
            reason = (routine[1] if routine else "Chosen together with the decision to smith." if cand is smith
                      else "Only one map destination is available." if v.kind == "map"
                      else "Confirm the selected cards." if v.kind == "card_select" and
                      v.facts["screen_details"].get("preview_showing")
                      else "The card just selected completed the selection; confirm it." if completed
                      else "Only one progress action is available.")
            ch = Choice(frame_id="", action_id=aid, reason=reason, turn_intent="")
            binding = v.bindings[aid]
            self.seq += 1
            self.stats.decisions += 1
            if self.rec:
                self.rec.record("automatic_decision", seq=self.seq, decision_source=source,
                                facts_source=self.facts_source, observation_fingerprint=v.fingerprint,
                                candidate=cand, binding=binding, reason=ch.reason)
            self.log(f"[{self.seq}] {v.kind}: {cand['description']} - {ch.reason} Skipping LLM.")
            self.hooks.decision(self.seq, v.kind, cand["description"], ch.reason, "", source)
            return self._execute_action(self.seq, cand, ch, binding, v)
        self._maybe_refresh_strategy(v)
        if self._gate("pre_decide", v):
            return None   # paused after the strategy call: the screen may have changed, so observe again
        frame = self._frame(v)
        if not frame.candidates:
            raise _Stop(STOP_NO_ACTIONS if not v.candidates else STOP_REPEATED_REJECTIONS,
                        f"ready {v.kind} screen offers no actions", v)

        self.seq += 1
        seq = self.seq
        self.hooks.step("deciding", f"The model is choosing on the {v.kind} screen", seq=seq,
                        options=len(frame.candidates))
        result = self.policy.choose(frame, tools=self.whatif) if self.whatif else self.policy.choose(frame)
        self.last_progress = self.clock()
        self.stats.decisions += 1
        self.stats.provider_retries += result.provider_retries
        self.stats.tool_calls += result.tool_calls
        self.stats.model_retries += max(0, len(result.attempts) - 1 - result.provider_retries - result.tool_calls)
        if self.rec:
            self.rec.record("llm_decision", seq=seq, provenance=self._provenance_label(),
                            decision_source=result.source,
                            policy={"name": self.policy.name, "version": self.policy.version},
                            prompt_version=self.policy.prompt_version, model=model_identity(self.policy.model),
                            frame=frame.to_record(),
                            choice=None if not result.ok else {"action_id": result.choice.action_id,
                                                               "reason": result.choice.reason,
                                                               "turn_intent": result.choice.turn_intent,
                                                               "route_plan": result.choice.route_plan,
                                                               "shop_plan": result.choice.shop_plan,
                                                               "rest_option": result.choice.rest_option,
                                                               "smith_card": result.choice.smith_card},
                            attempts=result.attempts, error_kind=result.error_kind, diagnostic=result.diagnostic,
                            history_dropped=result.history_dropped, support_trim=result.support_trim,
                            provider_retries=result.provider_retries, tool_calls=result.tool_calls,
                            prompt_modules=result.prompt_modules)
        if not result.ok and self.hooks.stop_requested():
            raise _Stop(STOP_INTERRUPTED, "stopped from the interface", v)
        if not result.ok:
            reason = {"input_too_large": STOP_INPUT_TOO_LARGE, "invalid_output": STOP_INVALID_MODEL_OUTPUT}.get(
                result.error_kind, STOP_MODEL_FAILED)
            if result.error_kind == "provider" and (result.diagnostic or "").startswith("budget_exhausted"):
                reason = STOP_MODEL_BUDGET
            raise _Stop(reason, result.diagnostic or "", v)

        ch = result.choice
        cand = frame.candidate(ch.action_id)
        binding = frame.bindings[ch.action_id]
        self.turn_intent = ch.turn_intent or None
        previous_plan, stale_before = self.route_plan, self.stats.stale
        if ch.route_plan is not None:   # a map screen, or an ancient that planned the act with its choice
            self.route_plan = _stored_route_plan(ch.route_plan, frame, v, len(self.route_outcomes))
            self._publish_route()
        self.log(f"[{seq}] {v.kind}: {cand['description']} - {ch.reason}")
        self.hooks.decision(seq, v.kind, cand["description"], ch.reason, ch.turn_intent or "", result.source)

        try:
            if v.kind == "map" and is_rest_destination(cand) and ch.rest_option is not None:
                return self._execute_rest_plan(seq, cand, ch, binding, v)
            elif v.kind == "shop" and ch.shop_plan is not None:
                self._execute_shop_plan(seq, ch, frame, v)
            elif cand["kind"] == "reward_batch":
                self._execute_reward_batch(seq, ch, binding, v)
            else:
                return self._execute_action(seq, cand, ch, binding, v)
        finally:
            if ch.route_plan is not None and self.stats.stale > stale_before:
                # The choice was made for a screen that had already changed and was never sent:
                # its route is not the one being travelled, so the plan before it stands.
                self.route_plan = previous_plan
                self._publish_route()

    def _smith_target(self, v: ScreenView) -> dict | None:
        """The upgrade grid's card that was named with the decision to smith, or None.

        The plan holds only for the grid that Smith opens at the same fire. It is spent on the first
        screen that is not that fire, so a grid without the named card (not upgradable, already
        upgraded) is decided by the model among the legal targets, as any other grid."""
        plan = self._smith_plan
        if plan is None:
            return None
        same_room = v.encounter_key == plan["encounter_key"]
        if same_room and v.kind == "rest_site":
            return None   # Smith is sent but its grid is not open yet
        self._smith_plan = None
        details = v.facts.get("screen_details") or {}
        if (not same_room or v.kind != "card_select" or details.get("screen_type") != "upgrade"
                or details.get("preview_showing") or v.facts.get("combat_context")):
            return None
        offered = [c for c in v.candidates if c["kind"] == "select_card" and names_card(plan["card"], c["details"])
                   and (v.fingerprint, c["action_id"]) not in self.stats.blocked]
        offered.sort(key=lambda c: c["details"].get("is_upgraded") is True)   # a copy still to upgrade first
        if self.rec:
            self.rec.event("smith_plan_step", {"card": plan["card"], "offered": len(offered),
                                               "action_id": offered[0]["action_id"] if offered else None})
        if not offered:
            self.log(f"card_select: the fire does not offer {plan['card']} for an upgrade; choosing among its targets")
            return None
        return offered[0]

    def _execute_rest_plan(self, seq, cand, ch, binding, view):
        """Travel and select the already-decided campfire option with fresh bindings."""
        if not self._execute_action(seq, cand, ch, binding, view):
            return
        current = self.last_view
        before, after = view.facts.get("run") or {}, current.facts.get("run") or {}
        # Room-entry hooks can change resources; never apply the old decision to those facts.
        def resources(v):
            player = v.facts.get("player") or {}
            return ({k: player.get(k) for k in ("hp", "max_hp", "gold")},
                    *(v.facts.get(k) for k in ("deck", "relics", "potions", "max_potion_slots")))

        if (current.kind != "rest_site" or not current.ready
                or before.get("act") != after.get("act")
                or type(before.get("floor")) is not int
                or after.get("floor") != before["floor"] + 1
                or resources(current) != resources(view)):
            self._outcome(seq, "rest_plan_stopped_state_changed", None, current,
                          "campfire arrival or player facts changed unexpectedly; reconsider at the current screen")
            return
        matches = [c for c in current.candidates if c["kind"] == "choose_rest_option"
                   and c["details"].get("id") == ch.rest_option]
        if len(matches) != 1 or (current.fingerprint, matches[0]["action_id"]) in self.stats.blocked:
            self._outcome(seq, "rest_plan_stopped_unavailable", None, current,
                          f"planned campfire option {ch.rest_option} is unavailable; reconsider at the campfire")
            return
        step = matches[0]
        self._track_encounter(current)
        if self.rec:
            self.rec.event("rest_plan_step", {"seq": seq, "option_id": ch.rest_option,
                                              "action_id": step["action_id"],
                                              "binding": current.bindings[step["action_id"]]})
        self.log(f"[{seq}] rest_site: {step['description']} - Already chosen on the map. Skipping LLM.")
        return self._execute_action(seq, step, replace(ch, action_id=step["action_id"]),
                                    current.bindings[step["action_id"]], current)

    def _execute_shop_plan(self, seq, ch, frame, view):
        """Execute one model's ordered basket, observing and validating every purchase."""
        def signature(item):
            # Sold entries can lose their model/description after the purchase resolves.
            keys = ("index", "category", "is_stocked") if item.get("is_stocked") is False else item
            return {k: item[k] for k in keys if k != "can_afford"}

        def stock(v):
            return [signature(item) for item in v.facts["screen_details"]["items"]]

        expected_stock = stock(view)
        expected_gold = (view.facts.get("player") or {}).get("gold")
        current = view
        for number, aid in enumerate(ch.shop_plan, 1):
            if (current.kind != "shop" or current.encounter_key != view.encounter_key
                    or not current.ready or stock(current) != expected_stock
                    or (current.facts.get("player") or {}).get("gold") != expected_gold):
                self._outcome(seq, "shop_plan_stopped_state_changed", None, current,
                              "shop, prices, stock or gold changed unexpectedly; unexecuted actions abandoned")
                return
            step = next((c for c in current.candidates if c["action_id"] == aid), None)
            if (step is None or step != frame.candidate(aid)
                    or (current.fingerprint, aid) in self.stats.blocked
                    or current.bindings.get(aid) != frame.bindings[aid]):
                self._outcome(seq, "shop_plan_stopped_unavailable", None, current,
                              "next planned shop action changed or is unavailable; unexecuted actions abandoned")
                return
            if self.rec:
                self.rec.event("shop_plan_step", {"seq": seq, "step": number, "action_id": aid,
                                                  "binding": current.bindings[aid]})
            if not self._execute_action(seq, step, replace(ch, action_id=aid, shop_plan=None),
                                        current.bindings[aid], current):
                return
            if step["kind"] == "shop_purchase":
                details = step["details"]
                for i, item in enumerate(expected_stock):
                    if item["index"] == details["index"]:
                        expected_stock[i] = signature({**item, "is_stocked": False})
                if type(expected_gold) is int:
                    expected_gold -= details["price"]
            current = self.last_view

    def _execute_reward_batch(self, seq, ch, binding, view):
        """One model choice, with individually observed and validated game actions.

        Only gold may precede the last step. Match the remaining ordered rewards
        without their shifting UI indices, then bind each step to its current index.
        Any unexpected change or uncertain delivery abandons the remaining steps.
        """
        remaining = list(view.facts["screen_details"]["items"])
        steps = {c["action_id"]: c for c in view.candidates}
        current = view

        def signatures(items):
            return [{k: val for k, val in x.items() if k != "index"} for x in items]

        for number, aid in enumerate(binding["steps"], 1):
            original = steps[aid]
            items = (current.facts.get("screen_details") or {}).get("items", [])
            if (current.kind != "rewards" or current.encounter_key != view.encounter_key
                    or signatures(items) != signatures(remaining)):
                self._outcome(seq, "batch_stopped_state_changed", None, current,
                              "remaining rewards changed unexpectedly; unexecuted steps abandoned")
                return
            if original["kind"] == "claim_reward":
                position = remaining.index(original["details"])
                current_id = f"reward_{items[position]['index']}"
            else:
                position, current_id = None, aid
            step = next((c for c in current.candidates if c["action_id"] == current_id), None)
            if step is None or (current.fingerprint, current_id) in self.stats.blocked:
                self._outcome(seq, "batch_stopped_unavailable", None, current,
                              "next reward action is unavailable; unexecuted steps abandoned")
                return
            if self.rec:
                self.rec.event("reward_batch_step", {"seq": seq, "batch_action_id": ch.action_id,
                                                     "step": number, "action_id": current_id,
                                                     "binding": current.bindings[current_id]})
            # Each step has its own outcome/history entry under the batch decision's seq.
            if not self._execute_action(seq, step, replace(ch, action_id=current_id),
                                        current.bindings[current_id], current):
                return
            if position is not None:
                remaining.pop(position)
            current = self.last_view

    def _execute_action(self, seq, cand, ch, binding, v) -> bool | None:
        self._gate("pre_send", v)   # paused before sending: the validation below catches a changed screen
        self.hooks.step("sending", cand["description"], seq=seq)
        # Fresh validation: same screen state and the same binding must still exist.
        try:
            fresh = self._observe("validate")
        except GameClientError as e:
            self._outcome(seq, "not_sent_validation_read_failed", None, None, str(e))
            raise _Stop(STOP_GAME_UNAVAILABLE, str(e), v)
        if fresh.fingerprint != v.fingerprint or fresh.bindings.get(ch.action_id) != binding:
            self.stats.stale += 1
            self._outcome(seq, "stale_not_sent", None, fresh, "screen changed after the decision")
            self._remember(seq, cand, ch, "stale_not_sent", fresh)
            self._save_state()
            return
        max_actions = self.lim.max_run_actions if self.scope == "run" else self.lim.max_actions
        if max_actions is not None and self.stats.actions_sent >= max_actions:
            self._outcome(seq, "not_sent_action_limit", None, fresh, "action limit reached before sending")
            raise _Stop(STOP_MAX_ACTIONS, f"reached {max_actions} actions", fresh)

        t0 = self.clock()
        while True:
            self.stats.actions_sent += 1
            try:
                resp = self.client.post_action(binding["payload"])
            except GameUnavailable as e:
                self._outcome(seq, "not_delivered", None, None, str(e))
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e), fresh)
            except AmbiguousActionError as e:
                return self._ambiguous(seq, cand, ch, binding, fresh, e, t0)
            except KeyboardInterrupt:
                self._outcome(seq, "interrupted_during_send", None, None, "Ctrl+C while the request was in flight")
                raise
            if (resp.accepted or not _ROOM_NOT_OPEN.search(resp.error or "")
                    or self.clock() - t0 >= self.lim.play_resolution_timeout_s):
                break
            # The game refused because the room is still opening although its state already lists the
            # option. A refusal executes nothing, so the same action is sent again once the unchanged
            # screen is confirmed; this is not the ambiguous case, which is never resent.
            self.stats.room_not_open_retries += 1
            if self.rec:
                self.rec.event("action_retry_room_not_open", {"seq": seq, "action_id": ch.action_id,
                                                               "error": (resp.error or "")[:200],
                                                               "waited_s": round(self.clock() - t0, 3)})
            self.sleep(self.lim.poll_interval_s)
            try:
                again = self._observe("validate")
            except GameClientError as e:
                self._outcome(seq, "rejected", resp.to_record(), None, resp.error or "", t0=t0)
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e), fresh)
            if again.fingerprint != fresh.fingerprint or again.bindings.get(ch.action_id) != binding:
                self.stats.stale += 1
                self._outcome(seq, "stale_not_sent", resp.to_record(), again,
                              "screen changed while waiting for the room to open")
                self._remember(seq, cand, ch, "stale_not_sent", again)
                self._save_state()
                return

        if not resp.accepted:
            self.stats.rejections += 1
            self.stats.consecutive_rejections += 1
            self.stats.blocked.add((fresh.fingerprint, ch.action_id))
            self._outcome(seq, "rejected", resp.to_record(), None, resp.error or "", t0=t0)
            self._remember(seq, cand, ch, f"rejected by game: {resp.error}", None)
            if (self.stats.consecutive_rejections >= self.lim.max_consecutive_rejections
                    or self.stats.rejections >= self.lim.max_total_rejections):
                raise _Stop(STOP_REPEATED_REJECTIONS, f"last: {resp.error}", fresh)
            return
        self.stats.consecutive_rejections = 0
        post, verdict = self._wait_resolution(fresh, binding["resolution"], travel=cand["kind"] == "choose_map_node")
        self._outcome(seq, verdict, resp.to_record(), post, "", t0=t0)
        if verdict in ("no_visible_change", "timeout_after_change"):
            raise _Stop(STOP_UNRESOLVED_ACTION, f"{ch.action_id} accepted but did not resolve ({verdict})", post)
        self._after_resolved(seq, cand, ch, binding, fresh, post, verdict)
        self._save_state()
        return True

    def _after_resolved(self, seq, cand, ch, binding, pre, post, verdict):
        self.stats.actions_resolved += 1
        if cand["kind"] == "select_card" and pre.kind == post.kind == "card_select":
            before, after = pre.facts["screen_details"], post.facts["screen_details"]
            if (before.get("can_confirm") is False and after.get("can_confirm") is True
                    and not after.get("preview_showing")):
                self._selection_complete = post.fingerprint   # the next decision on this exact grid is Confirm
        if cand["kind"] == "use_potion" and self.combat:
            self.combat.potions_used += 1
        if cand["kind"] == "skip_card_reward" and post.kind == "rewards" and not self._fight_overlay(pre):
            self._skipped_card_rewards += 1
        if cand["kind"] == "choose_rest_option" and cand["details"].get("id") == SMITH and ch.smith_card:
            self._smith_plan = {"card": ch.smith_card, "encounter_key": post.encounter_key}
        if cand["kind"] == "choose_map_node" and self.map_memory is not None:
            d = cand["details"]
            self.map_memory["chosen"] = {"col": d.get("col"), "row": d.get("row"), "type": map_type(d.get("type"))}
            self._publish_route()
        self._remember(seq, cand, ch, verdict, post, pre=pre)
        res = binding["resolution"]
        if res.startswith("shop:") and post.kind == "shop" and pre.shop_stock and post.shop_stock:
            idx = int(res.split(":")[1])
            changed = {i for (i, s), (_, s2) in zip(pre.shop_stock, post.shop_stock) if s != s2}
            if changed and idx not in changed:
                raise _Stop(STOP_INDEX_MISMATCH, f"bought index {idx} but items {sorted(changed)} changed stock", post)

    def _ambiguous(self, seq, cand, ch, binding, pre, err, t0):
        self.stats.ambiguous += 1
        if self.rec:
            self.rec.error("ambiguous_action_response", {"seq": seq, "error": str(err)[:300]})
        post, verdict = self._wait_resolution(pre, binding["resolution"], travel=cand["kind"] == "choose_map_node")
        if post.fingerprint != pre.fingerprint:
            self._outcome(seq, "ambiguous_inferred_executed", {"error": str(err)[:300]}, post, verdict, t0=t0)
            if verdict == "timeout_after_change":
                raise _Stop(STOP_UNRESOLVED_ACTION, "state changed but never settled after an ambiguous response", post)
            self._after_resolved(seq, cand, ch, binding, pre, post, "ambiguous_inferred_executed")
            return
        self._outcome(seq, "ambiguous_unresolved", {"error": str(err)[:300]}, post,
                      "no visible change; request may still be pending", t0=t0)
        raise _Stop(STOP_AMBIGUOUS_UNRESOLVED, "request outcome unknown and no visible change; not resending", post)

    def _wait_resolution(self, pre: ScreenView, resolution: str, *, travel: bool = False) -> tuple[ScreenView, str]:
        """``travel``: the action moved to a map node. While the move plays, the game still shows the
        map of the floor being left, ready and without the chosen node; that is not the next screen."""
        timeout = (self.lim.end_turn_resolution_timeout_s if resolution == "end_turn"
                   else self.lim.play_resolution_timeout_s)
        deadline = self.clock() + timeout
        changed, stable, prev, last = False, 0, None, pre
        leaving = (lambda v: travel and pre.kind == "map" and v.kind == "map"
                   and v.facts.get("run") == pre.facts.get("run"))
        while True:
            v = self._observe_tolerant("resolve", deadline)
            if v is not None:
                last = v
                if v.fingerprint != pre.fingerprint:
                    changed = True
                if changed:
                    stable = stable + 1 if v.fingerprint == prev else 1
                    prev = v.fingerprint
                    done = v.ready or v.terminal or v.unsupported or v.kind == "combat_ended"
                    if resolution == "end_turn" and v.kind == "combat" and v.ready:
                        done = (pre.combat_round is None or v.combat_round is None
                                or v.combat_round > pre.combat_round)
                    if self._handoff and v.kind != pre.kind:
                        return v, "resolved"   # the picker closed; Mentor is already playing the fight on
                    if stable >= self.lim.settle_polls and done and not leaving(v):
                        return v, "resolved"
            if self.clock() >= deadline:
                if not changed and resolution == "toggle":
                    return last, "accepted_unobservable"  # API does not expose grid selection state
                if changed and leaving(last) and last.ready:
                    return last, "resolved"   # still that map after the full wait: decide on it, as before
                return last, "timeout_after_change" if changed else "no_visible_change"
            self.sleep(self.lim.poll_interval_s)

    def _remember(self, seq, cand, ch, verdict, post: ScreenView | None, pre: ScreenView | None = None):
        entry = {"seq": seq, "action_id": ch.action_id, "kind": cand["kind"], "description": cand["description"],
                 "your_reason": ch.reason, "confirmed_result": verdict}
        if post is not None:
            p = post.facts.get("player") or {}
            entry["observed_after"] = {"screen": post.kind, "hp": p.get("hp"), "block": p.get("block"),
                                       "energy": p.get("energy"), "gold": p.get("gold"), "round": post.combat_round}
        # Did this action move the displayed enemy intent? Only within one round (a new round has a new intent).
        if (pre is not None and post is not None and pre.in_combat and post.in_combat
                and cand["kind"] != "end_turn" and pre.combat_round == post.combat_round):
            before = (pre.facts.get("turn_outlook") or {}).get("incoming_damage_total")
            after = (post.facts.get("turn_outlook") or {}).get("incoming_damage_total")
            if before is not None and after is not None:
                entry["incoming_damage"] = {"before": before, "after": after}
                if before != after:
                    self.incoming_changes.append({"round": post.combat_round, "action": cand["description"][:120],
                                                  "incoming_before": before, "incoming_after": after})
                    self.incoming_changes = self.incoming_changes[-12:]
        self.history = (self.history + [entry])[-8:]

    def _outcome(self, seq, verdict, response, post, detail, t0=None):
        if self.rec:
            self.rec.outcome(seq, verdict=verdict, response=response,
                             timing={"since_send_s": None if t0 is None else round(self.clock() - t0, 4)},
                             observation_after=None if post is None else
                             {"kind": post.kind, "fingerprint": post.fingerprint, "facts": post.facts},
                             detail=detail)

    # ---------------------------------------------------------- same-run resume
    def _state(self) -> dict:
        v = self.last_view
        run = (v.facts.get("run") or {}) if v is not None and v.facts else {}
        c = self.combat
        return {"version": STATE_VERSION, "run_id": self.run_id, "character": run.get("character"),
                "act": run.get("act"), "floor": run.get("floor"),
                "route_plan": self.route_plan, "route_outcomes": self.route_outcomes,
                "recent_outcomes": list(self.recent_outcomes), "encounter_key": self.encounter_key,
                "history": self.history, "turn_intent": self.turn_intent,
                "incoming_changes": self.incoming_changes, "hallway_done": self.hallway_done,
                "risk_ref": self.risk_ref, "rest_checkpoints": self.rest_checkpoints,
                "coverage": self.coverage, "coverage_seen": sorted(list(k) for k in self._coverage_seen),
                "last_combat_facts": self.last_combat_facts,
                "energy_observation": self.energy_observation, "shop_entry": self.shop_entry,
                "map_memory": self.map_memory,
                "skipped_card_rewards": self._skipped_card_rewards, "relic_counters": self._relic_counters,
                "treasures_opened": self.treasures_opened,
                "combat": None if c is None else {"key": c.key, "encounter_type": c.encounter_type,
                                                  "start_hp": c.start_hp, "rounds": c.rounds,
                                                  "potions_used": c.potions_used, "act": c.act, "floor": c.floor,
                                                  "last_hp": c.last_hp, "damage": c.damage, "healed": c.healed,
                                                  "hp_tracked": c.hp_tracked, "potions_held": c.potions_held}}

    def _save_state(self):
        if self.state_path is None or self.run_id_source == "session_scoped":
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = f"{self.state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state(), fh, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except (OSError, TypeError, ValueError) as e:
            if self.rec:
                self.rec.error("run_state_save_failed", {"error": str(e)[:300]})

    def _load_state(self, v: ScreenView):
        """Restore route and encounter memory when the saved file is verifiably the same run, earlier or here."""
        if self.run_id_source == "session_scoped":
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return
        run = v.facts.get("run") or {}
        here, saved = (run.get("act"), run.get("floor")), (st.get("act"), st.get("floor"))
        why = None
        if not isinstance(st, dict) or st.get("version") != STATE_VERSION or st.get("run_id") != self.run_id:
            why = "different_run_or_version"
        elif st.get("character") != run.get("character"):
            why = "different_character"
        elif None in here or None in saved or here < saved:
            why = "saved_state_is_ahead_of_the_game_or_position_unknown"
        if why:
            if self.rec:
                self.rec.event("run_state_not_restored", {"reason": why, "saved": saved, "observed": here})
            return
        self.route_plan = st.get("route_plan")
        # A fresh API observation takes precedence over persisted energy information.
        if self.energy_observation is None:
            self.energy_observation = st.get("energy_observation")
        self.shop_entry = st.get("shop_entry")
        if self.map_memory is None:   # a map screen seen in this session is newer than the file
            self.map_memory = st.get("map_memory")
        self.route_outcomes = list(st.get("route_outcomes") or [])
        self.recent_outcomes.extend(st.get("recent_outcomes") or [])
        self.hallway_done = {int(k): n for k, n in (st.get("hallway_done") or {}).items() if str(k).isdigit()}
        self.risk_ref = st.get("risk_ref")
        self.rest_checkpoints = list(st.get("rest_checkpoints") or [])
        self.coverage = st.get("coverage") or {}
        self._coverage_seen = {tuple(k) for k in st.get("coverage_seen") or []}
        self._relic_counters = {**{str(k): n for k, n in (st.get("relic_counters") or {}).items() if type(n) is int},
                                **self._relic_counters}
        self.treasures_opened = max(self.treasures_opened, st.get("treasures_opened") or 0)
        same_place = here == saved
        c = st.get("combat")
        # Encounter memory only continues inside the very same room.
        if same_place and st.get("encounter_key") in (v.encounter_key, (c or {}).get("key")):
            self.encounter_key = st["encounter_key"]
            self._skipped_card_rewards = st.get("skipped_card_rewards") or 0
            self.history = list(st.get("history") or [])[-8:]
            self.turn_intent = st.get("turn_intent")
            self.incoming_changes = list(st.get("incoming_changes") or [])
            self.last_combat_facts = st.get("last_combat_facts")
            if c and (v.in_combat and c["key"] == v.encounter_key or v.kind in COMBAT_OVERLAY_KINDS):
                self.combat = _Combat(c["key"], c.get("encounter_type"), c.get("start_hp"), c.get("rounds"),
                                      c.get("potions_used") or 0, c.get("act"), c.get("floor"),
                                      last_hp=c.get("last_hp"), damage=c.get("damage") or 0,
                                      healed=c.get("healed") or 0,
                                      hp_tracked=c.get("hp_tracked") is True and c.get("last_hp") is not None,
                                      potions_held=c.get("potions_held"))
                self.combat.note_hp((v.facts.get("player") or {}).get("hp"))   # what happened while unobserved
                self.seen_combat = True
        elif c:  # the fight that was open when the session stopped ended while nobody was watching
            self.route_outcomes.append({"encounter_type": c.get("encounter_type"), "result": "unobserved",
                                        "act": c.get("act"), "floor": c.get("floor"), "hp_lost": None,
                                        "hp_lost_pct": None, "hp_damage": None, "hp_healed": None,
                                        "hp_damage_pct": None, "node_class": None, "expected_pct": None,
                                        "expected_p90_pct": None})
            if c.get("encounter_type") == "monster" and c.get("act") is not None:
                self.hallway_done[c["act"]] = self.hallway_done.get(c["act"], 0) + 1
        self.resumed_from = {"act": saved[0], "floor": saved[1], "same_room": same_place,
                             "route_plan_restored": self.route_plan is not None,
                             "combat_restored": self.combat is not None}
        if self.rec:
            self.rec.event("run_state_restored", self.resumed_from)
        self.log(f"resumed run state saved at act {saved[0]} floor {saved[1]}"
                 f"{' (route plan restored)' if self.route_plan else ''}")
        self._publish_route()

    def _provenance_label(self) -> str:
        if self.policy.scripted:
            return "synthetic_scripted_model"
        return "llm_generated_synthetic_input" if self.facts_source == "synthetic" else "llm_generated"

    # ---------------------------------------------------------- public
    def run(self) -> dict:
        max_actions = self.lim.max_run_actions if self.scope == "run" else self.lim.max_actions
        stop, detail, final = None, "", None
        try:
            if self.scope == "combat":
                first = self._observe("initial")
                if not first.in_combat:
                    raise _Stop(STOP_NOT_IN_COMBAT, f"screen {first.kind}", first)
            while True:
                if max_actions is not None and self.stats.actions_sent >= max_actions:
                    raise _Stop(STOP_MAX_ACTIONS, f"reached {max_actions} actions", self.last_view)
                self._gate("step_start")
                self._check_global()
                self._step()
                if self.scope == "step":
                    raise _Stop(STOP_STEP_DONE, "", self.last_view)
        except _Stop as s:
            stop, detail, final = s.reason, s.detail, s.view or self.last_view
        except KeyboardInterrupt:
            stop, detail, final = STOP_INTERRUPTED, "Ctrl+C", self.last_view
        finally:
            if self.autobattle is not None:
                try:
                    self.autobattle.control("off")
                except GameClientError as exc:
                    stop = STOP_AUTOBATTLE_FAILED
                    detail = f"{detail}; could not turn Mentor off: {exc}".lstrip("; ")
        self._save_state()
        return self.summary(stop, detail, final)

    def _evaluation(self) -> dict:
        """Run-quality measures: dispatch reliability, combat bookkeeping, route calibration, evidence coverage."""
        s = self.stats
        by_class = {}
        for o in self.route_outcomes:
            # HP removed during the fight is what the pooled expectation measures; older records only have the net.
            realized = o.get("hp_damage_pct") if o.get("hp_damage_pct") is not None else o.get("hp_lost_pct")
            if realized is None or not o.get("node_class"):
                continue
            row = by_class.setdefault(o["node_class"], {"fights": 0, "realized_pct": 0.0, "expected_pct": 0.0,
                                                        "worse_than_p90": 0, "net_only_fights": 0})
            row["fights"] += 1
            row["net_only_fights"] += o.get("hp_damage_pct") is None
            row["realized_pct"] = round(row["realized_pct"] + realized, 1)
            row["expected_pct"] = round(row["expected_pct"] + (o.get("expected_pct") or 0), 1)
            row["worse_than_p90"] += o.get("expected_p90_pct") is not None and realized > o["expected_p90_pct"]
        coverage = {}
        for kind, c in self.coverage.items():
            n = c.get("options") or 0
            coverage[kind] = {**c, "identifier_match_pct": _per_100(c.get("identifier_matched", 0), n),
                              "qualifying_any_rung_pct": _per_100(c.get("qualifying_any_rung", 0), n),
                              "qualifying_nearby_floor_pct": _per_100(c.get("qualifying_nearby_floor", 0), n),
                              "qualifying_deck_matched_pct": _per_100(c.get("qualifying_deck_matched", 0), n),
                              "history_rate_quoted_pct": _per_100(c.get("history_rate_quoted", 0), n)}
        open_now = int(self.combat is not None)
        return {
            "dispatch_per_100_decisions": {"stale": _per_100(s.stale, s.decisions),
                                           "rejected": _per_100(s.rejections, s.decisions),
                                           "ambiguous": _per_100(s.ambiguous, s.decisions),
                                           "model_corrective_retries": _per_100(s.model_retries, s.decisions)},
            "combat_bookkeeping": {"combats_started_this_session": s.combats_started,
                                   "combats_finished_this_session": s.combats_finished, "combat_still_open": bool(open_now),
                                   "consistent": s.combats_finished + open_now - int(bool(
                                       self.resumed_from and self.resumed_from["combat_restored"])) == s.combats_started},
            "route_calibration": {"scope": "whole run when resumed from saved state, else this session",
                                  "agent_loss_scale": agent_loss_scale(self.route_outcomes), "by_class": by_class,
                                  "rest_checkpoints": self.rest_checkpoints},
            "evidence_coverage": coverage,
            "resumed_from": self.resumed_from,
        }

    def summary(self, stop, detail, final) -> dict:
        s = self.stats
        budget = getattr(self.policy.model, "budget", None)
        timing = self.policy.progress.summary()
        if self.planner is not None and self.planner.progress is not self.policy.progress:
            # The CLI shares one progress tracker; injected callers may use two.
            strategy_timing = self.planner.progress.summary()
            timing["by_purpose"].update(strategy_timing["by_purpose"])
            timing["retry_backoff_s"] = round(timing["retry_backoff_s"] + strategy_timing["retry_backoff_s"], 3)
        return {
            "stop_reason": stop, "detail": detail[:500],
            "final_screen": final.kind if final else None,
            "final_player_hp": (final.facts.get("player") or {}).get("hp") if final and final.facts else None,
            "decisions": s.decisions, "actions_sent": s.actions_sent, "actions_resolved": s.actions_resolved,
            "rejections": s.rejections, "room_not_open_retries": s.room_not_open_retries,
            "stale_decisions": s.stale, "ambiguous_responses": s.ambiguous,
            "model_corrective_retries": s.model_retries, "model_provider_retries": s.provider_retries,
            "what_if_tool_calls": s.tool_calls,
            "strategic_model_updates": s.strategic_updates,
            "strategic_failures": s.strategic_failures, "combats_finished": s.combats_finished,
            "evaluation": self._evaluation(),
            "autobattle_combats": s.autobattle_combats,
            "autobattle_offer_hold": self._offer_hold,
            "autobattle_offer_handoffs": s.offer_handoffs, "autobattle_offer_ai_picks": s.offer_ai_picks,
            "recent_outcomes": list(self.recent_outcomes),
            "model_usage": budget.summary() if budget is not None else None,
            "model_timing": timing,
            "elapsed_s": round(self.clock() - self.session_start, 2),
            "run_id": self.run_id, "run_id_source": self.run_id_source,
        }


def _per_100(n, base):
    return round(n / base * 100, 2) if base else None


def _potion_names(facts) -> list | None:
    """Names of the potions held, or None when the facts do not list potions."""
    potions = (facts or {}).get("potions")
    if not isinstance(potions, list):
        return None
    return [str(p.get("name") or p.get("id") or "?") for p in potions if isinstance(p, dict)]


def _clip(s, n=1000):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 15] + " …[truncated]"


def _last_call(model):
    lc = getattr(model, "last_call", None)
    return lc.to_dict() if hasattr(lc, "to_dict") else None

def _card_zone_constraints(view: ScreenView) -> dict | None:
    """Only explicit standalone Exhaust text establishes self-exhaust here."""
    cards = list(view.facts.get("deck") or [])
    for candidate in view.candidates:
        d = candidate.get("details") or {}
        if d.get("card_name"):
            cards.append({"name": d["card_name"], "description": d.get("card_description")})
        elif d.get("name") and d.get("description"):
            cards.append(d)
    exhausting = sorted({c["name"] for c in cards if c.get("name") and
                         re.search(r"(?:^|[.!?]\s*)Exhaust\s*(?:[.!](?:\s|$)|$)", c.get("description") or "")})
    retrieval = sorted({c["name"] for c in cards if c.get("name") and
                        re.search(r"from (?:your |the )?Discard Pile", c.get("description") or "", re.I)})
    if not exhausting and not retrieval:
        return None
    return {"source": "explicit visible card descriptions; partial text checks, not a combat simulation",
            "self_exhausting_after_play": exhausting, "retrieves_from_discard": retrieval,
            "constraint": "A self-exhausting card goes to exhaust after play; discard-only retrieval cannot "
                          "recover it afterward. Exhausting another card does not imply self-exhaust. "
                          "Unlisted interactions remain unknown; inspect their text."}


def _spending_outlook(view: ScreenView, support) -> dict | None:
    """Gold in hand and empty potion slots against the shops each offered route passes.

    The route projections count HP only: a path through a shop loses the gold it is expected to
    spend there and gains nothing for what the gold buys. This states that side in plain numbers."""
    player = view.facts.get("player") or {}
    gold, slots, held = player.get("gold"), view.facts.get("max_potion_slots"), view.facts.get("potions")
    if type(gold) is not int:
        return None
    support = support if isinstance(support, dict) else {}
    paths = [p for p in (support.get("paths") or {}).get("candidates") or [] if isinstance(p, dict)]
    spend = ((support.get("hp_risk") or {}).get("shop_spend") or {}).get("mean")
    by_opening = {}
    for p in paths:
        aid, nodes = p.get("first_action_id"), p.get("nodes") or []
        if not aid:
            continue
        row = by_opening.setdefault(aid, {"action_id": aid, "paths_listed": 0, "paths_with_a_shop": 0,
                                          "rooms_to_first_shop": None})
        row["paths_listed"] += 1
        if "shop" in nodes:
            row["paths_with_a_shop"] += 1
            first = nodes.index("shop") + 1
            row["rooms_to_first_shop"] = first if row["rooms_to_first_shop"] is None else min(first, row["rooms_to_first_shop"])
    free = slots - len(held) if type(slots) is int and isinstance(held, list) else None
    visits = round(gold / spend, 1) if type(spend) in (int, float) and spend > 0 else None
    return {
        "label": "GOLD AND POTION SLOTS VS. THE SHOPS ON EACH OPENING; arithmetic on observed values, not a ranking",
        "gold": gold, "pooled_spend_per_shop_visit": spend, "gold_as_shop_visits": visits,
        "potions_held": len(held) if isinstance(held, list) else None, "free_potion_slots": free,
        "shops_by_opening": sorted(by_opening.values(), key=lambda r: r["action_id"]) or None,
        "note": "Every HP projection here ignores what gold buys: a route through a shop is charged the gold and "
                "credited nothing for the potions, removal, relic or card it turns into. Gold still held at the act "
                "boss did nothing in any fight of the act. paths_with_a_shop counts only the listed non-dominated "
                "routes; null shops_by_opening means no routes were listed."}


def _shop_packages(view: ScreenView, entry_gold, hidden=()) -> dict:
    """Partial basket comparisons; shop_plan can select any feasible offered sequence."""
    gold = (view.facts.get("player") or {}).get("gold")
    result = {"entry_gold": entry_gold, "current_gold": gold,
              "leave_now": {"action_id": "proceed", "gold_remaining": gold},
              "scope": "current prices/remaining stock, up to 3 purchases; no utility ranking; "
                       "shop_plan may include more than 3 purchases and need not match a listed package; "
                       "execution rechecks each step and abandons the plan if prices or stock change",
              "packages": [], "truncated": False}
    if type(gold) is not int:
        return result
    items = [c for c in view.candidates if c["kind"] == "shop_purchase" and c["action_id"] not in hidden
             and type(c["details"].get("price")) is int and c["details"]["price"] >= 0]
    slots = view.facts.get("max_potion_slots")
    free_slots = max(0, slots - len(view.facts.get("potions") or [])) if type(slots) is int else None
    for count in range(1, min(3, len(items)) + 1):
        for package in combinations(items, count):
            cost = sum(c["details"]["price"] for c in package)
            potions = sum(c["details"].get("category") == "potion" for c in package)
            if cost > gold or (free_slots is not None and potions > free_slots):
                continue
            if len(result["packages"]) >= 64:
                result["truncated"] = True
                return result
            result["packages"].append({"action_ids": [c["action_id"] for c in package],
                                       "total_cost": cost, "gold_remaining": gold - cost})
    return result


def _change_reasons(old, new, *, initial, combat_finished) -> list:
    if initial or old is None:
        return ["initial"]
    r = []
    if combat_finished:
        r.append("combat_finished")
    if old.get("deck") != new.get("deck"):
        r.append("deck_changed")
    if old.get("relics") != new.get("relics"):
        r.append("relic_changed")
    if old.get("potions") != new.get("potions"):
        r.append("potion_changed")
    if old.get("resources") != new.get("resources"):
        r.append("resources_changed")
    if (old.get("run") or {}).get("act") != (new.get("run") or {}).get("act"):
        r.append("act_changed")
    if old.get("opportunities") != new.get("opportunities"):
        r.append("opportunities_changed")
    return r or ["corrected_facts"]


def _stored_route_plan(plan: dict, frame, v: ScreenView, outcomes_seen: int) -> dict:
    """Snapshot what the plan assumed, so later map screens can detect when it no longer holds."""
    support = frame.decision_support or {}
    path = next((p for p in (support.get("paths") or {}).get("candidates") or []
                 if plan["path_id"] and p.get("path_id") == plan["path_id"]), None)
    run, player = v.facts.get("run") or {}, v.facts.get("player") or {}
    details = v.facts.get("screen_details") or {}
    ancient_map = details.get("map") if v.kind != "map" and isinstance(details.get("map"), dict) else None
    here = (ancient_map if ancient_map is not None else details).get("current_position") or {}
    start = [here.get("col"), here.get("row")]
    if ancient_map is not None and None in start and ancient_map.get("preview_from"):
        start = list(ancient_map["preview_from"])   # the node the act's first row hangs from
    hp, max_hp = player.get("hp"), player.get("max_hp")
    return {**plan, "act": run.get("act"), "floor": run.get("floor"),
            "coords": path["coords"] if path else None, "trajectory": path["trajectory"] if path else None,
            "start": start,
            # Planned before the ancient's relic was taken: _settle_ancient_plan rebases it on the next map screen.
            **({"made_with_ancient_choice": True} if ancient_map is not None else {}),
            "hp_pct": hp / max_hp * 100 if hp is not None and max_hp else None, "gold": player.get("gold"),
            "relics": sorted(str(r.get("id") or r.get("name")) for r in v.facts.get("relics") or []),
            "potions": [str(p.get("id") or p.get("name")) for p in v.facts.get("potions") or []],
            "outcomes_seen": outcomes_seen}
