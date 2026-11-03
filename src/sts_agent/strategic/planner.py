"""Strategic planner orchestration: update gate, one model call, validation, memory.

Synchronous and single-caller: do not share one ``StrategicPlanner`` between
threads. The injected ``TextModel`` owns timeouts and cancellation. There is
one completion attempt per update, no repair loop, and no heuristic fallback.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import (
    FAILED, INVALID_INPUT, MAX_MODEL_TEXT_BYTES, UNCHANGED, UPDATED, InputError, OutputError,
    PlannerResult, StrategicAssessment, StrategicSnapshot, parse_assessment,
)
from .prompts import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_user_message


class TextModel(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


class StrategicPlanner:
    def __init__(self, model: TextModel, *, transcript_path: str | Path | None = None):
        """``transcript_path``: optional local JSONL log of model calls. Keep it
        under the Git-ignored ``traces/`` directory."""
        self._model = model
        self._transcript = Path(transcript_path) if transcript_path else None
        self.reset()

    # -- state -----------------------------------------------------------------
    def reset(self) -> None:
        self._run_id: str | None = None
        self._assessment: StrategicAssessment | None = None   # last accepted
        self._assessed: StrategicSnapshot | None = None       # snapshot it was built from
        self._latest_checked: StrategicSnapshot | None = None
        self._revisions: dict[int, str] = {}                  # revision -> snapshot_id (this run)

    @property
    def assessment(self) -> StrategicAssessment | None:
        """Last accepted assessment (may not match the latest facts)."""
        return self._assessment

    @property
    def latest_checked(self) -> StrategicSnapshot | None:
        return self._latest_checked

    # -- main entry --------------------------------------------------------------
    def update(self, snapshot: StrategicSnapshot | Mapping[str, Any], *, force: bool = False) -> PlannerResult:
        if not isinstance(snapshot, StrategicSnapshot):
            try:
                snapshot = StrategicSnapshot(**dict(snapshot))
            except (InputError, TypeError) as e:
                return self._invalid(f"invalid snapshot: {e}")

        if snapshot.run_id != self._run_id:
            # New run: forget everything first, so a failed first call cannot
            # leave the previous run's strategy around.
            self.reset()
            self._run_id = snapshot.run_id

        known = self._revisions.get(snapshot.revision)
        if known is not None and known != snapshot.snapshot_id:
            return self._invalid(f"revision {snapshot.revision} was already supplied with different content")
        latest = self._latest_checked.revision if self._latest_checked else None
        if latest is not None and snapshot.revision < latest:
            return self._invalid(f"revision {snapshot.revision} is older than the latest checked revision {latest}")

        self._revisions[snapshot.revision] = snapshot.snapshot_id
        self._latest_checked = snapshot

        reason = self._refresh_reason(snapshot, force)
        if reason is None:
            return self._result(UNCHANGED, snapshot, model_called=False,
                                diagnostic=None if self._assessed.snapshot_id == snapshot.snapshot_id else
                                f"facts unchanged; reusing assessment of revision {self._assessment.input_revision}")
        return self._call_model(snapshot, reason)

    def _refresh_reason(self, snap: StrategicSnapshot, force: bool) -> str | None:
        if self._assessment is None:
            return "no assessment yet"
        if force:
            return "forced"
        if snap.snapshot_id == self._assessed.snapshot_id:
            return None
        if snap.facts_hash != self._assessment.facts_hash:
            return "strategic facts changed"
        if "combat_finished" in snap.change_reasons and snap.revision > self._assessed.revision:
            return "new combat_finished revision"
        return None

    def _call_model(self, snap: StrategicSnapshot, reason: str) -> PlannerResult:
        user = build_user_message(snap, self._assessment)
        text: Any = None
        try:
            text = self._model.complete(system=SYSTEM_INSTRUCTION, user=user)
        except Exception as e:  # provider errors, timeouts, cancellations raised as exceptions
            diag = f"model call failed ({reason}): {type(e).__name__}: {e}"
            self._log(snap, user, None, FAILED, diag)
            return self._result(FAILED, snap, model_called=True, diagnostic=diag)
        try:
            content = parse_assessment(text, snap.facts)
        except OutputError as e:
            diag = f"model output rejected ({reason}): {e}"
            self._log(snap, user, text, FAILED, diag)
            return self._result(FAILED, snap, model_called=True, diagnostic=diag)

        self._assessment = StrategicAssessment(
            run_id=snap.run_id, input_revision=snap.revision, snapshot_id=snap.snapshot_id,
            facts_hash=snap.facts_hash, data_source=snap.data_source, live_validated=snap.live_validated,
            prompt_version=PROMPT_VERSION, content=content)
        self._assessed = snap
        self._log(snap, user, text, UPDATED, reason)
        return self._result(UPDATED, snap, model_called=True, diagnostic=reason)

    # -- results -----------------------------------------------------------------
    def _result(self, status, snap, *, model_called, diagnostic) -> PlannerResult:
        a = self._assessment
        applies = a is not None and a.run_id == snap.run_id and a.facts_hash == snap.facts_hash
        if status == FAILED and a is None:
            diagnostic = f"{diagnostic}; no assessment available"
        elif status == FAILED:
            diagnostic = (f"{diagnostic}; previous assessment (revision {a.input_revision}) is historical"
                          f"{' and still matches current facts' if applies else ' and does NOT match current facts'}")
        return PlannerResult(status=status, assessment=a, applies_to_current_facts=applies,
                             snapshot_id=snap.snapshot_id, latest_checked_revision=snap.revision,
                             model_called=model_called, diagnostic=diagnostic)

    def _invalid(self, diagnostic: str) -> PlannerResult:
        latest = self._latest_checked
        a = self._assessment
        applies = bool(a and latest and a.facts_hash == latest.facts_hash)
        return PlannerResult(status=INVALID_INPUT, assessment=a, applies_to_current_facts=applies,
                             snapshot_id=None, latest_checked_revision=latest.revision if latest else None,
                             model_called=False, diagnostic=diagnostic)

    # -- local transcript ----------------------------------------------------------
    def _log(self, snap, user, text, status, diagnostic) -> None:
        if self._transcript is None:
            return
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "note": "Recorded model call. Model outputs are not reproducible by resampling.",
            "prompt_version": PROMPT_VERSION, "run_id": snap.run_id, "revision": snap.revision,
            "snapshot_id": snap.snapshot_id, "data_source": snap.data_source,
            "live_validated": snap.live_validated, "status": status, "diagnostic": diagnostic,
            "user_message": user,
            "model_text": text[:MAX_MODEL_TEXT_BYTES] if isinstance(text, str) else None,
        }
        self._transcript.parent.mkdir(parents=True, exist_ok=True)
        with open(self._transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
