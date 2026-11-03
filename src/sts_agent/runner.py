"""Runner: observe -> wait until ready -> build candidates -> choose -> validate
-> execute once -> wait for resolution -> observe again.

Safety properties:

* Strictly sequential: one in-flight HTTP request at a time, and no new action
  until the previous one has visibly resolved.
* Candidates are rebuilt from a fresh observation for every decision, and the
  decision is re-validated against another fresh observation right before it
  is sent.
* An action whose outcome is ambiguous (timeout / dropped connection / 5xx)
  is never resent. The runner inspects the game: if the state changed it
  continues from the new state; if not, it stops with a diagnostic.
* Every wait is bounded (ready, resolution, no-progress, total elapsed,
  action count) and Ctrl+C stops cleanly with the trace closed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .actions import ACTION_END_TURN, build_candidates, validate_against
from .client import AmbiguousActionError, GameClientError, GameUnavailable, HttpError
from .config import Limits
from .observation import (
    PHASE_COMBAT_BUSY, PHASE_COMBAT_ENDED, PHASE_COMBAT_READY, PHASE_COMBAT_SELECTION, PHASE_GAME_OVER,
    PHASE_MENU, PHASE_OVERLAY, PHASE_POST_COMBAT, PHASE_UNKNOWN, normalize,
)

# Stop reasons
STOP_COMBAT_ENDED = "combat_ended"
STOP_PLAYER_DEFEATED = "player_defeated"
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
STOP_POLICY_ERROR = "policy_error"

TERMINAL_COMBAT_PHASES = {PHASE_COMBAT_ENDED, PHASE_POST_COMBAT, PHASE_GAME_OVER}
UNSUPPORTED_PHASES = {PHASE_COMBAT_SELECTION, PHASE_OVERLAY, PHASE_MENU, PHASE_UNKNOWN}


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = "", obs: dict | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.obs = reason, detail, obs


@dataclass
class StepResult:
    executed: bool                 # an action was accepted (or inferred to have happened)
    stop_reason: str | None        # None = combat continues
    detail: str = ""
    decision: dict | None = None
    verdict: str | None = None
    obs_after: dict | None = None


@dataclass
class SessionStats:
    actions_sent: int = 0
    actions_resolved: int = 0
    rejections: int = 0
    consecutive_rejections: int = 0
    stale_decisions: int = 0
    ambiguous: int = 0
    turns_ended: int = 0
    decisions: int = 0
    blocked: dict = field(default_factory=dict)  # fingerprint -> set(action_id) rejected there


def combat_result(obs: dict | None) -> str:
    if obs is None:
        return "unknown"
    hp = obs["player"].get("hp")
    if obs["phase"] == PHASE_GAME_OVER:
        return "defeat" if (hp is None or hp <= 0) else "run_ended"
    if hp is not None and hp <= 0:
        return "defeat"
    if obs["phase"] in (PHASE_COMBAT_ENDED, PHASE_POST_COMBAT):
        return "victory"
    return "in_progress"


class Runner:
    def __init__(self, client, policy, recorder=None, limits: Limits | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        self.client = client
        self.policy = policy
        self.rec = recorder
        self.lim = limits or Limits()
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.stats = SessionStats()
        self.session_start = clock()
        self.last_progress = clock()
        self._last_fp: str | None = None
        self.last_obs: dict | None = None

    # -- observation ---------------------------------------------------------
    def observe(self, label: str = "observe") -> dict:
        raw = self.client.get_state()
        if self.rec:
            self.rec.raw(label, raw)
        obs = normalize(raw)
        if obs["fingerprint"] != self._last_fp:
            self._last_fp = obs["fingerprint"]
            self.last_progress = self.clock()
        self.last_obs = obs
        return obs

    def _session_deadline(self) -> float:
        return self.session_start + self.lim.max_elapsed_s

    def _check_global(self) -> None:
        now = self.clock()
        if now >= self._session_deadline():
            raise _Stop(STOP_MAX_ELAPSED, f"session exceeded {self.lim.max_elapsed_s}s", self.last_obs)
        if now - self.last_progress >= self.lim.max_no_progress_s:
            raise _Stop(STOP_NO_PROGRESS, f"state unchanged for {self.lim.max_no_progress_s}s", self.last_obs)

    def _observe_tolerant(self, label: str, deadline: float) -> dict | None:
        """Observe, tolerating transient connection errors until ``deadline``."""
        try:
            return self.observe(label)
        except (GameUnavailable, HttpError) as e:
            if self.clock() >= deadline:
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e), self.last_obs)
            self.log(f"  (transient read error: {e}; retrying)")
            return None

    def wait_until_ready(self, timeout_s: float | None = None) -> dict:
        """Poll until the play phase is ready and stable, or the combat is over.

        Returns a ready observation. Raises ``_Stop`` for terminal or
        unsupported states and for timeouts.
        """
        deadline = min(self.clock() + (timeout_s or self.lim.ready_timeout_s), self._session_deadline())
        stable, prev_fp = 0, None
        while True:
            self._check_global()
            obs = self._observe_tolerant("wait_ready", deadline)
            if obs is not None:
                self._raise_if_terminal(obs)
                if obs["phase"] == PHASE_COMBAT_READY:
                    stable = stable + 1 if obs["fingerprint"] == prev_fp else 1
                    prev_fp = obs["fingerprint"]
                    if stable >= self.lim.settle_polls:
                        return obs
                else:
                    stable, prev_fp = 0, None
            if self.clock() >= deadline:
                phase = obs["phase"] if obs else "unreadable"
                raise _Stop(STOP_READY_TIMEOUT, f"not ready after {timeout_s or self.lim.ready_timeout_s}s (phase={phase})", obs)
            self.sleep(self.lim.poll_interval_s)

    def _raise_if_terminal(self, obs: dict) -> None:
        hp = obs["player"].get("hp")
        if obs["phase"] == PHASE_GAME_OVER or (hp is not None and hp <= 0 and obs["phase"] != PHASE_COMBAT_READY):
            raise _Stop(STOP_PLAYER_DEFEATED if combat_result(obs) == "defeat" else STOP_COMBAT_ENDED,
                        f"phase={obs['phase']}", obs)
        if obs["phase"] in (PHASE_COMBAT_ENDED, PHASE_POST_COMBAT):
            raise _Stop(STOP_COMBAT_ENDED, f"phase={obs['phase']} state_type={obs['state_type']}", obs)
        if obs["phase"] in UNSUPPORTED_PHASES:
            raise _Stop(STOP_UNSUPPORTED_SCREEN, f"phase={obs['phase']} state_type={obs['state_type']} screen={obs.get('screen')}", obs)

    # -- resolution ----------------------------------------------------------
    def _wait_resolution(self, pre: dict, kind: str) -> tuple[dict, str]:
        """After an accepted action, wait until the game visibly moved on and settled.

        play_card: any change in the observation (hand, energy, piles, enemies,
        phase) counts as the card having been played; HP/energy specifically
        need not change. Then wait for a stable ready state (or combat end).
        end_turn: wait until a new player round is ready, or combat ends.
        """
        timeout = self.lim.end_turn_resolution_timeout_s if kind == ACTION_END_TURN else self.lim.play_resolution_timeout_s
        deadline = min(self.clock() + timeout, self._session_deadline())
        pre_round = (pre.get("combat") or {}).get("round")
        changed = False
        stable, prev_fp = 0, None
        last = pre
        while True:
            obs = self._observe_tolerant("resolve", deadline)
            if obs is not None:
                last = obs
                if obs["fingerprint"] != pre["fingerprint"]:
                    changed = True
                if obs["phase"] in TERMINAL_COMBAT_PHASES or obs["phase"] in UNSUPPORTED_PHASES:
                    return obs, "resolved_screen_changed" if changed else "screen_changed"
                if changed and obs["phase"] == PHASE_COMBAT_READY:
                    if kind == ACTION_END_TURN:
                        rnd = (obs.get("combat") or {}).get("round")
                        new_turn = pre_round is None or rnd is None or rnd > pre_round
                        if not new_turn:
                            stable, prev_fp = 0, None
                            obs = None
                    if obs is not None:
                        stable = stable + 1 if obs["fingerprint"] == prev_fp else 1
                        prev_fp = obs["fingerprint"]
                        if stable >= self.lim.settle_polls:
                            return obs, "resolved"
                elif obs["phase"] != PHASE_COMBAT_READY:
                    stable, prev_fp = 0, None
            if self.clock() >= deadline:
                return last, ("timeout_after_change" if changed else "no_visible_change")
            self.sleep(self.lim.poll_interval_s)

    # -- one decision --------------------------------------------------------
    def step(self) -> StepResult:
        try:
            return self._step()
        except _Stop as s:
            return StepResult(False, s.reason, s.detail, obs_after=s.obs)

    def _step(self) -> StepResult:
        obs = self.wait_until_ready()
        candidates, excluded = build_candidates(obs)
        blocked = self.stats.blocked.get(obs["fingerprint"], set())
        if blocked:
            candidates = [c for c in candidates if c["action_id"] not in blocked]
        if not candidates:
            raise _Stop(STOP_REPEATED_REJECTIONS, "every candidate was rejected for this state", obs)
        try:
            decision = self.policy.decide(obs, candidates)
        except Exception as e:  # a policy bug must not leave the game mid-action
            raise _Stop(STOP_POLICY_ERROR, f"{type(e).__name__}: {e}", obs)
        self.stats.decisions += 1
        cand = decision["action"]
        seq = self.rec.next_seq() if self.rec else self.stats.decisions
        if self.rec:
            self.rec.decision(seq, obs, candidates, excluded, decision)
            if blocked:
                self.rec.event("blocked_candidates", {"seq": seq, "action_ids": sorted(blocked)})
        self.log(f"[{seq}] {decision['rule']}: {decision['reason']}")
        unsupported = [x for x in excluded if x["reason"] == "unsupported effect text"]
        if unsupported:
            self.log("     unsupported cards not considered: " + ", ".join(f"{x['name']} ({'; '.join(x['unsupported_effects'])})" for x in unsupported))

        # Validate right before sending.
        try:
            fresh = self.observe("validate")
        except GameClientError as e:
            self._outcome(seq, "not_sent_validation_read_failed", None, 0.0, None, detail=str(e))
            raise _Stop(STOP_GAME_UNAVAILABLE, f"could not re-read state before sending: {e}", obs)
        problem = validate_against(cand, fresh)
        if problem:
            self.stats.stale_decisions += 1
            self._outcome(seq, "stale_not_sent", None, 0.0, fresh, detail=problem)
            self.log(f"     not sent: {problem}")
            return StepResult(False, None, problem, decision, "stale_not_sent", fresh)

        # Execute exactly once.
        t0 = self.clock()
        self.stats.actions_sent += 1
        try:
            resp = self.client.post_action(cand["payload"])
        except GameUnavailable as e:
            self._outcome(seq, "not_delivered", None, self.clock() - t0, None, detail=str(e))
            raise _Stop(STOP_GAME_UNAVAILABLE, str(e), fresh)
        except AmbiguousActionError as e:
            return self._handle_ambiguous(seq, decision, fresh, e, t0)
        except KeyboardInterrupt:
            self._outcome(seq, "interrupted_during_send", None, self.clock() - t0, None,
                          detail="Ctrl+C while the request was in flight; outcome unknown")
            raise

        if not resp.accepted:
            self.stats.rejections += 1
            self.stats.consecutive_rejections += 1
            self.stats.blocked.setdefault(fresh["fingerprint"], set()).add(cand["action_id"])
            self._outcome(seq, "rejected", resp.to_record(), self.clock() - t0, None, detail=resp.error or "")
            self.log(f"     rejected by game: {resp.error}")
            if (self.stats.consecutive_rejections >= self.lim.max_consecutive_rejections
                    or self.stats.rejections >= self.lim.max_total_rejections):
                raise _Stop(STOP_REPEATED_REJECTIONS,
                            f"{self.stats.consecutive_rejections} consecutive / {self.stats.rejections} total rejections; last: {resp.error}",
                            fresh)
            return StepResult(False, None, resp.error or "rejected", decision, "rejected", fresh)

        self.stats.consecutive_rejections = 0
        send_s = self.clock() - t0
        post, verdict = self._wait_resolution(fresh, cand["kind"])
        self._outcome(seq, verdict, resp.to_record(), send_s, post, resolution_s=self.clock() - t0)
        if verdict in ("no_visible_change", "timeout_after_change"):
            raise _Stop(STOP_UNRESOLVED_ACTION,
                        f"action {cand['action_id']} was accepted but did not resolve ({verdict}); "
                        "stopping instead of acting on an uncertain state", post)
        self.stats.actions_resolved += 1
        if cand["kind"] == ACTION_END_TURN:
            self.stats.turns_ended += 1
        return StepResult(True, None, "", decision, verdict, post)

    def _handle_ambiguous(self, seq, decision, pre, err, t0) -> StepResult:
        """Never resend. Look at the game to find out what happened."""
        self.stats.ambiguous += 1
        self.log(f"     ambiguous result ({err}); inspecting the game, not resending")
        if self.rec:
            self.rec.error("ambiguous_action_response", {"seq": seq, "error": str(err)})
        post, verdict = self._wait_resolution(pre, decision["action"]["kind"])
        if post["fingerprint"] != pre["fingerprint"]:
            v = "ambiguous_inferred_executed"
            self._outcome(seq, v, {"error": str(err)}, err.elapsed_s, post,
                          resolution_s=self.clock() - t0, detail=f"state changed after ambiguous response ({verdict})")
            self.stats.actions_resolved += 1
            if verdict in ("timeout_after_change",):
                raise _Stop(STOP_UNRESOLVED_ACTION, "state changed but never settled after an ambiguous response", post)
            return StepResult(True, None, "inferred executed", decision, v, post)
        self._outcome(seq, "ambiguous_unresolved", {"error": str(err)}, err.elapsed_s, post,
                      resolution_s=self.clock() - t0, detail="no visible change; the request may still be pending in the game")
        raise _Stop(STOP_AMBIGUOUS_UNRESOLVED,
                    "action request timed out and the game shows no change; not resending. "
                    "Check the game, then run `observe` before continuing.", post)

    def _outcome(self, seq, verdict, response, send_s, post_obs, resolution_s=None, detail=""):
        if self.rec:
            self.rec.outcome(seq, verdict=verdict, response=response,
                             timing={"send_s": round(send_s, 4),
                                     "resolution_s": None if resolution_s is None else round(resolution_s, 4)},
                             observation_after=post_obs, detail=detail)

    # -- full combat ----------------------------------------------------------
    def play_combat(self) -> dict:
        stop, detail, final = None, "", None
        try:
            try:
                first = self.observe("initial")
            except GameClientError as e:
                raise _Stop(STOP_GAME_UNAVAILABLE, str(e))
            final = first
            if first["phase"] in (PHASE_COMBAT_SELECTION, PHASE_OVERLAY):
                raise _Stop(STOP_UNSUPPORTED_SCREEN, f"phase={first['phase']} screen={first.get('screen')}", first)
            if first["phase"] not in (PHASE_COMBAT_READY, PHASE_COMBAT_BUSY):
                raise _Stop(STOP_NOT_IN_COMBAT, f"phase={first['phase']} state_type={first['state_type']}", first)
            while True:
                if self.stats.actions_sent >= self.lim.max_actions:
                    raise _Stop(STOP_MAX_ACTIONS, f"reached {self.lim.max_actions} actions", self.last_obs)
                self._check_global()
                r = self.step()
                final = r.obs_after or final
                if r.stop_reason:
                    stop, detail = r.stop_reason, r.detail
                    break
        except _Stop as s:
            stop, detail, final = s.reason, s.detail, s.obs or final
        except KeyboardInterrupt:
            stop, detail, final = STOP_INTERRUPTED, "Ctrl+C", self.last_obs
        return self._summary(stop, detail, final)

    def run_single_step(self) -> dict:
        try:
            r = self.step()
        except KeyboardInterrupt:
            return self._summary(STOP_INTERRUPTED, "Ctrl+C", self.last_obs)
        stop = r.stop_reason or STOP_STEP_DONE
        return self._summary(stop, r.detail or (r.verdict or ""), r.obs_after or self.last_obs)

    def _summary(self, stop: str, detail: str, final: dict | None) -> dict:
        s = self.stats
        return {
            "stop_reason": stop,
            "detail": detail,
            "combat_result": combat_result(final),
            "final_phase": final["phase"] if final else None,
            "final_state_type": final["state_type"] if final else None,
            "final_player_hp": final["player"]["hp"] if final else None,
            "decisions": s.decisions,
            "actions_sent": s.actions_sent,
            "actions_resolved": s.actions_resolved,
            "turns_ended": s.turns_ended,
            "rejections": s.rejections,
            "stale_decisions": s.stale_decisions,
            "ambiguous_responses": s.ambiguous,
            "elapsed_s": round(self.clock() - self.session_start, 2),
        }
