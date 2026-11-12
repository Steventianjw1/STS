"""Run coordinator: the LLM chooses every gameplay action on supported screens.

Loop per decision:

    observe -> wait until a supported screen is ready AND stable
            -> track encounter memory / combat outcomes
            -> refresh the strategic assessment if strategic facts changed
            -> build DecisionFrame -> LLM picks one action id (<= 1 corrective retry)
            -> record decision (before execution)
            -> re-observe; send only if the screen is unchanged and the binding still exists
            -> send ONCE -> wait for screen-specific resolution -> record outcome

No heuristic fallback, no automatic gold/potion/reward handling: code only
waits and observes. Ambiguous deliveries are inspected and never resent.
Single-caller, synchronous.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..client import AmbiguousActionError, GameClientError, GameUnavailable, HttpError
from ..config import Limits
from ..route_risk import agent_loss_scale, node_class
from ..strategic import StrategicSnapshot
from .models import DecisionFrame
from .policy import LLMDecisionPolicy, model_identity
from .screens import CAPABILITIES, MOD_REVISION, ScreenView, build_view

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
STOP_MAX_ELAPSED = "max_elapsed"
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

LLM_PROVENANCE_LIVE = {"data_source": "agent_generated", "behavior": "llm_policy", "is_expert_demonstration": False,
                       "note": "Choices made by an LLM through this harness. Not human or expert play."}
LLM_PROVENANCE_SCRIPTED = {"data_source": "synthetic", "behavior": "scripted_model_replies",
                           "is_expert_demonstration": False, "is_llm_reasoning": False,
                           "note": "SYNTHETIC: canned model replies (test double). Not LLM reasoning, not live play."}


# Non-combat screens that can open on top of a fight (potion or card-effect pickers). While a
# combat is open on the same floor they belong to that fight; they are not a room exit.
COMBAT_OVERLAY_KINDS = ("card_select",)
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


@dataclass
class CoordStats:
    decisions: int = 0
    actions_sent: int = 0
    actions_resolved: int = 0
    rejections: int = 0
    consecutive_rejections: int = 0
    stale: int = 0
    ambiguous: int = 0
    model_retries: int = 0
    strategic_updates: int = 0
    strategic_failures: int = 0
    combats_finished: int = 0
    combats_started: int = 0
    search_decisions: int = 0
    search_misses: int = 0
    blocked: set = field(default_factory=set)   # (view fingerprint, action_id)


_OPP_KIND = {"select_card_reward": "card_reward", "choose_map_node": "route", "choose_event_option": "event",
             "choose_rest_option": "rest_site", "select_relic": "relic_reward", "claim_treasure_relic": "relic_reward"}
_REWARD_KIND = {"gold": "gold_reward", "potion": "potion_reward", "relic": "relic_reward", "card": "card_reward",
                "special_card": "card_reward", "card_removal": "card_removal"}


class RunCoordinator:
    def __init__(self, client, policy: LLMDecisionPolicy, *, planner=None, recorder=None, expert_stats=None,
                 limits: Limits | None = None, scope: str = "run", run_id: str, run_id_source: str,
                 facts_source: str = "observed", objective: str, model_timeout_s: float = 0.0,
                 state_path=None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        if scope not in ("run", "combat", "step"):
            raise ValueError("scope must be run, combat or step")
        self.client, self.policy, self.planner, self.rec = client, policy, planner, recorder
        self.expert_stats = expert_stats
        self.lim = limits or Limits()
        self.scope, self.run_id, self.run_id_source = scope, run_id, run_id_source
        self.facts_source, self.objective = facts_source, objective
        self.model_timeout_s = model_timeout_s
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
        self.seq = 0
        self.last_combat_facts: dict | None = None   # shown on pickers that open during a fight
        self.incoming_changes: list = []             # this encounter: intent damage moved by the agent's own actions
        self.hallway_done: dict = {}                 # act -> finished hallway fights (for the early/later class)
        self.risk_ref: dict | None = None            # pooled per-class loss seen on the latest map screen
        self.rest_checkpoints: list = []             # projected vs. actual HP on arrival at planned rest sites
        self.coverage: dict = {}                     # screen kind -> summed evidence_coverage counters
        self._coverage_seen: set = set()
        self.state_path = state_path                 # same-run resume file; None disables persistence
        self._restore_pending = state_path is not None
        self.resumed_from: dict | None = None

    # ---------------------------------------------------------------- observe
    def _observe(self, label: str) -> ScreenView:
        raw = self.client.get_state()
        if self.rec:
            self.rec.raw(label, raw)
        view = build_view(raw)
        if view.fingerprint != self._last_fp:
            self._last_fp = view.fingerprint
            self.last_progress = self.clock()
        self.last_view = view
        return view

    def _observe_tolerant(self, label, deadline) -> ScreenView | None:
        try:
            return self._observe(label)
        except (GameUnavailable, HttpError) as e:
            if self.clock() >= deadline:
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e), self.last_view)
            return None

    def _deadline(self) -> float:
        return self.session_start + self.lim.max_elapsed_s

    def _check_global(self):
        now = self.clock()
        if now >= self._deadline():
            raise _Stop(STOP_MAX_ELAPSED, f"session exceeded {self.lim.max_elapsed_s}s", self.last_view)
        if now - self.last_progress >= self.lim.max_no_progress_s:
            raise _Stop(STOP_NO_PROGRESS, f"state unchanged for {self.lim.max_no_progress_s}s", self.last_view)

    def _wait_ready(self) -> ScreenView:
        deadline = min(self.clock() + self.lim.ready_timeout_s, self._deadline())
        stable, prev = 0, None
        while True:
            self._check_global()
            v = self._observe_tolerant("wait_ready", deadline)
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
        if v.in_combat:
            self.seen_combat = True
            if self.combat is None or self.combat.key != v.encounter_key:
                run = v.facts.get("run") or {}
                self.combat = _Combat(v.encounter_key, v.state_type, v.facts["player"]["hp"],
                                      act=run.get("act"), floor=run.get("floor"))
                self.stats.combats_started += 1
            if v.combat_round is not None:
                self.combat.rounds = max(self.combat.rounds or 0, v.combat_round)
            self.last_combat_facts = {k: v.facts.get(k) for k in ("combat", "player", "hand", "piles", "enemies",
                                                                  "turn_outlook")}
        elif v.kind == "rest_site":
            self._rest_checkpoint(v)

    def _finish_combat(self, after: ScreenView, result: str):
        c, self.combat = self.combat, None
        if c is None:
            return
        player = (after.facts.get("player") or {}) if after.facts else {}
        end_hp, max_hp = player.get("hp"), player.get("max_hp")
        if result == "defeat" and (end_hp is None or end_hp > 0):
            end_hp = 0
        change = None if (c.start_hp is None or end_hp is None) else end_hp - c.start_hp
        lost = None if change is None else max(0, -change)
        ordinal = self.hallway_done.get(c.act, 0) + 1
        ref = self.risk_ref if self.risk_ref and self.risk_ref["act"] == c.act else None
        cls = node_class(c.encounter_type, ordinal, ref["early_k"]) if ref else None
        if c.encounter_type == "monster" and c.act is not None:
            self.hallway_done[c.act] = ordinal
        outcome = {"encounter_type": c.encounter_type, "result": result, "act": c.act, "floor": c.floor,
                   "hp_lost": lost, "hp_lost_pct": round(lost / max_hp * 100, 1) if lost is not None and max_hp else None,
                   "node_class": cls, "expected_pct": (ref["classes"].get(cls) or {}).get("mean_pct") if cls else None,
                   "expected_p90_pct": (ref["classes"].get(cls) or {}).get("p90_pct") if cls else None}
        item = {"id": f"combat-{c.key}", "encounter_type": c.encounter_type, "result": result,
                "hp_lost": lost, "turns": c.rounds,
                "notes": (f"OBSERVED: HP change {change if change is not None else 'unknown'} measured on the first "
                          f"post-combat screen (includes end-of-combat effects); potions used by this agent: "
                          f"{c.potions_used}.")}
        self.recent_outcomes.append(item)
        self.route_outcomes.append(outcome)
        self.pending_combat_finished = True
        self.stats.combats_finished += 1
        if self.rec:
            self.rec.event("combat_finished", item)
            self.rec.event("route_calibration", outcome)

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
                items.append({"id": rid if seen[rid] == 1 else f"{rid}#{seen[rid]}", "name": r.get("name") or rid,
                              "description": _clip(r.get("description")), "counter": r.get("counter")})
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
            opps = {"items": items[:40]}
        return {
            "run": {"character": run.get("character"), "difficulty": run.get("difficulty"), "act": run.get("act"),
                    "floor": run.get("floor"), "encounter_type": v.state_type},
            "resources": {"hp": pl.get("hp"), "max_hp": pl.get("max_hp"), "gold": pl.get("gold"), "max_energy": None},
            "deck": deck,
            "relics": relics, "potions": potions, "opportunities": opps,
            "recent_outcomes": {"items": list(self.recent_outcomes)},
        }

    def _maybe_refresh_strategy(self, v: ScreenView):
        if self.planner is None:
            return
        if (v.in_combat or self._fight_overlay(v)) and self.strat_revision > 0:
            return  # ordinary card plays never trigger a refresh; refresh after the combat ends
        facts = self._strategic_facts(v)
        if self.strat_revision > 0 and facts == self.strat_facts and not self.pending_combat_finished:
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
        result = self.planner.update(snap)
        self.last_progress = self.clock()   # model latency is not a game stall
        self.last_planner_result = result
        self.strat_facts, self.pending_combat_finished = facts, False
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
            status = "current_in_combat_not_refreshed" if v.in_combat else "current"
        else:
            status = "historical"
            base["note"] = ("The latest strategic update failed or facts changed; this assessment is from an older "
                            "revision and may not match current facts.")
        return {"status": status, **base}

    # ---------------------------------------------------------- one decision
    def _frame(self, v: ScreenView) -> DecisionFrame:
        cands = [c for c in v.candidates if (v.fingerprint, c["action_id"]) not in self.stats.blocked]
        blocked = [c["action_id"] for c in v.candidates if (v.fingerprint, c["action_id"]) in self.stats.blocked]
        lims = list(v.limitations)
        if blocked:
            lims.append(f"Not offered because the game rejected them in this exact state: {blocked}")
        cap = CAPABILITIES.get(v.kind, {})
        support = None
        if self.expert_stats is not None:
            started = self.clock()
            support = self.expert_stats.for_view(
                v, budget_s=max(0.0, self._deadline() - started - self.model_timeout_s),
                route_state={"plan": self.route_plan, "outcomes": self.route_outcomes})
            self.last_progress += self.clock() - started  # evidence latency is not a game stall
            self._note_support(v, support)
        facts = v.facts
        overlay = self._fight_overlay(v)
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
        self._maybe_refresh_strategy(v)
        frame = self._frame(v)
        if not frame.candidates:
            raise _Stop(STOP_NO_ACTIONS if not v.candidates else STOP_REPEATED_REJECTIONS,
                        f"ready {v.kind} screen offers no actions", v)
        if self._deadline() - self.clock() < self.model_timeout_s:
            raise _Stop(STOP_MAX_ELAPSED, "not enough session time left for a model call", v)

        self.seq += 1
        seq = self.seq
        result = self.policy.choose(frame)
        self.last_progress = self.clock()
        self.stats.decisions += 1
        self.stats.model_retries += max(0, len(result.attempts) - 1)
        if result.search is not None:
            if result.source == "llm":
                self.stats.search_misses += 1
                self.log(f"[{seq}] combat search miss -> LLM: {result.search.get('miss')}")
            else:
                self.stats.search_decisions += 1
        if self.rec:
            self.rec.record("llm_decision", seq=seq, provenance=self._provenance_label(result.source),
                            decision_source=result.source, search=result.search,
                            policy={"name": self.policy.name, "version": self.policy.version},
                            prompt_version=self.policy.prompt_version, model=model_identity(self.policy.model),
                            frame=frame.to_record(),
                            choice=None if not result.ok else {"action_id": result.choice.action_id,
                                                               "reason": result.choice.reason,
                                                               "turn_intent": result.choice.turn_intent,
                                                               "route_plan": result.choice.route_plan},
                            attempts=result.attempts, error_kind=result.error_kind, diagnostic=result.diagnostic,
                            history_dropped=result.history_dropped, support_trim=result.support_trim)
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
        if v.kind == "map" and ch.route_plan is not None:
            self.route_plan = _stored_route_plan(ch.route_plan, frame, v, len(self.route_outcomes))
        self.log(f"[{seq}] {v.kind}: {cand['description']} - {ch.reason}")

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
        if self.clock() >= self._deadline():
            self._outcome(seq, "not_sent_deadline", None, fresh, "session deadline reached before sending")
            raise _Stop(STOP_MAX_ELAPSED, "deadline reached before sending", fresh)

        t0 = self.clock()
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
        post, verdict = self._wait_resolution(fresh, binding["resolution"])
        self._outcome(seq, verdict, resp.to_record(), post, "", t0=t0)
        if verdict in ("no_visible_change", "timeout_after_change"):
            raise _Stop(STOP_UNRESOLVED_ACTION, f"{ch.action_id} accepted but did not resolve ({verdict})", post)
        self._after_resolved(seq, cand, ch, binding, fresh, post, verdict)
        self._save_state()

    def _after_resolved(self, seq, cand, ch, binding, pre, post, verdict):
        self.stats.actions_resolved += 1
        if cand["kind"] == "use_potion" and self.combat:
            self.combat.potions_used += 1
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
        post, verdict = self._wait_resolution(pre, binding["resolution"])
        if post.fingerprint != pre.fingerprint:
            self._outcome(seq, "ambiguous_inferred_executed", {"error": str(err)[:300]}, post, verdict, t0=t0)
            if verdict == "timeout_after_change":
                raise _Stop(STOP_UNRESOLVED_ACTION, "state changed but never settled after an ambiguous response", post)
            self._after_resolved(seq, cand, ch, binding, pre, post, "ambiguous_inferred_executed")
            return
        self._outcome(seq, "ambiguous_unresolved", {"error": str(err)[:300]}, post,
                      "no visible change; request may still be pending", t0=t0)
        raise _Stop(STOP_AMBIGUOUS_UNRESOLVED, "request outcome unknown and no visible change; not resending", post)

    def _wait_resolution(self, pre: ScreenView, resolution: str) -> tuple[ScreenView, str]:
        timeout = (self.lim.end_turn_resolution_timeout_s if resolution == "end_turn"
                   else self.lim.play_resolution_timeout_s)
        deadline = min(self.clock() + timeout, self._deadline())
        changed, stable, prev, last = False, 0, None, pre
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
                    if stable >= self.lim.settle_polls and done:
                        return v, "resolved"
            if self.clock() >= deadline:
                if not changed and resolution == "toggle":
                    return last, "accepted_unobservable"  # API does not expose grid selection state
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
                "combat": None if c is None else {"key": c.key, "encounter_type": c.encounter_type,
                                                  "start_hp": c.start_hp, "rounds": c.rounds,
                                                  "potions_used": c.potions_used, "act": c.act, "floor": c.floor}}

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
        self.route_outcomes = list(st.get("route_outcomes") or [])
        self.recent_outcomes.extend(st.get("recent_outcomes") or [])
        self.hallway_done = {int(k): n for k, n in (st.get("hallway_done") or {}).items() if str(k).isdigit()}
        self.risk_ref = st.get("risk_ref")
        self.rest_checkpoints = list(st.get("rest_checkpoints") or [])
        self.coverage = st.get("coverage") or {}
        self._coverage_seen = {tuple(k) for k in st.get("coverage_seen") or []}
        same_place = here == saved
        c = st.get("combat")
        # Encounter memory only continues inside the very same room.
        if same_place and st.get("encounter_key") in (v.encounter_key, (c or {}).get("key")):
            self.encounter_key = st["encounter_key"]
            self.history = list(st.get("history") or [])[-8:]
            self.turn_intent = st.get("turn_intent")
            self.incoming_changes = list(st.get("incoming_changes") or [])
            self.last_combat_facts = st.get("last_combat_facts")
            if c and (v.in_combat and c["key"] == v.encounter_key or v.kind in COMBAT_OVERLAY_KINDS):
                self.combat = _Combat(c["key"], c.get("encounter_type"), c.get("start_hp"), c.get("rounds"),
                                      c.get("potions_used") or 0, c.get("act"), c.get("floor"))
                self.seen_combat = True
        elif c:  # the fight that was open when the session stopped ended while nobody was watching
            self.route_outcomes.append({"encounter_type": c.get("encounter_type"), "result": "unobserved",
                                        "act": c.get("act"), "floor": c.get("floor"), "hp_lost": None,
                                        "hp_lost_pct": None, "node_class": None, "expected_pct": None,
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

    def _provenance_label(self, source: str = "llm") -> str:
        if source != "llm":
            return "search_generated_synthetic_input" if self.facts_source == "synthetic" else "search_generated"
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
                if self.stats.actions_sent >= max_actions:
                    raise _Stop(STOP_MAX_ACTIONS, f"reached {max_actions} actions", self.last_view)
                self._check_global()
                self._step()
                if self.scope == "step":
                    raise _Stop(STOP_STEP_DONE, "", self.last_view)
        except _Stop as s:
            stop, detail, final = s.reason, s.detail, s.view or self.last_view
        except KeyboardInterrupt:
            stop, detail, final = STOP_INTERRUPTED, "Ctrl+C", self.last_view
        self._save_state()
        return self.summary(stop, detail, final)

    def _evaluation(self) -> dict:
        """Run-quality measures: dispatch reliability, combat bookkeeping, route calibration, evidence coverage."""
        s = self.stats
        by_class = {}
        for o in self.route_outcomes:
            if o.get("hp_lost_pct") is None or not o.get("node_class"):
                continue
            row = by_class.setdefault(o["node_class"], {"fights": 0, "realized_pct": 0.0, "expected_pct": 0.0,
                                                        "worse_than_p90": 0})
            row["fights"] += 1
            row["realized_pct"] = round(row["realized_pct"] + o["hp_lost_pct"], 1)
            row["expected_pct"] = round(row["expected_pct"] + (o.get("expected_pct") or 0), 1)
            row["worse_than_p90"] += o.get("expected_p90_pct") is not None and o["hp_lost_pct"] > o["expected_p90_pct"]
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
        return {
            "stop_reason": stop, "detail": detail[:500],
            "final_screen": final.kind if final else None,
            "final_player_hp": (final.facts.get("player") or {}).get("hp") if final and final.facts else None,
            "decisions": s.decisions, "actions_sent": s.actions_sent, "actions_resolved": s.actions_resolved,
            "rejections": s.rejections, "stale_decisions": s.stale, "ambiguous_responses": s.ambiguous,
            "model_corrective_retries": s.model_retries, "strategic_model_updates": s.strategic_updates,
            "strategic_failures": s.strategic_failures, "combats_finished": s.combats_finished,
            "evaluation": self._evaluation(),
            "combat_search_decisions": s.search_decisions, "combat_search_misses": s.search_misses,
            "recent_outcomes": list(self.recent_outcomes),
            "model_usage": budget.summary() if budget is not None else None,
            "elapsed_s": round(self.clock() - self.session_start, 2),
            "run_id": self.run_id, "run_id_source": self.run_id_source,
        }


def _per_100(n, base):
    return round(n / base * 100, 2) if base else None


def _clip(s, n=1000):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[: n - 15] + " …[truncated]"


def _last_call(model):
    lc = getattr(model, "last_call", None)
    return lc.to_dict() if hasattr(lc, "to_dict") else None

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
    here = (v.facts.get("screen_details") or {}).get("current_position") or {}
    hp, max_hp = player.get("hp"), player.get("max_hp")
    return {**plan, "act": run.get("act"), "floor": run.get("floor"),
            "coords": path["coords"] if path else None, "trajectory": path["trajectory"] if path else None,
            "start": [here.get("col"), here.get("row")],
            "hp_pct": hp / max_hp * 100 if hp is not None and max_hp else None, "gold": player.get("gold"),
            "relics": sorted(str(r.get("id") or r.get("name")) for r in v.facts.get("relics") or []),
            "potions": [str(p.get("id") or p.get("name")) for p in v.facts.get("potions") or []],
            "outcomes_seen": outcomes_seen}
