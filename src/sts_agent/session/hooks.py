"""What a running session reports to whoever is watching it, and how it is paused.

The coordinator, the model-call tracker and the providers call these methods. This
base class does nothing, so a command-line session behaves exactly as before; the
interface's hub (``ui/hub.py``) overrides them. Hooks only observe and gate: they
never choose an action and never talk to the game.
"""
from __future__ import annotations


class SessionHooks:
    # -- what the agent is doing -------------------------------------------------
    def step(self, phase: str, detail: str = "", **info) -> None:
        """The agent entered a phase: waiting, strategy, evidence, deciding, sending, autobattle..."""

    def observed(self, view) -> None:
        """Every game state the coordinator reads (a ``ScreenView``)."""

    def model_event(self, phase: str, data: dict) -> None:
        """Model-call progress: call_started, call_waiting, call_finished, retry_scheduled..."""

    def thinking(self, kind: str, delta: str) -> None:
        """A piece of the stream of the model call in flight: ``reasoning`` or ``answer`` text."""

    # -- what it concluded -------------------------------------------------------
    def decision(self, seq: int, screen: str, description: str, reason: str, turn_intent: str = "",
                 source: str = "llm") -> None:
        """A chosen action and the model's stated reason (``source`` is llm, forced_progress...)."""

    def strategy(self, result, reasons: list) -> None:
        """A strategic planner update (``PlannerResult``) and why it was requested."""

    def route(self, plan: dict | None, map_memory: dict | None) -> None:
        """The stored route plan and the act map as last seen."""

    def evidence(self, screen: str, support) -> None:
        """Advisory evidence attached to a decision (or None when none was fetched)."""

    def combat_finished(self, item: dict, outcome: dict) -> None:
        """One fight ended: the summary kept for the planner and the route-calibration record."""

    def autobattle(self, status: dict) -> None:
        """Latest answer of the Autobattle bridge."""

    def log(self, message: str) -> None:
        """A console line of the session."""

    def finished(self, summary: dict, trace_dir: str) -> None:
        """The session ended: its summary (stop reason, counters) and where its trace is."""

    # -- control -----------------------------------------------------------------
    def gate(self, point: str) -> float:
        """Block here while the session is paused. Returns the seconds spent paused."""
        return 0.0

    def pause_requested(self) -> bool:
        return False

    def stop_requested(self) -> bool:
        return False
