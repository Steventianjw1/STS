"""Decision frame and model-choice contracts.

A ``DecisionFrame`` is everything the model may see for one choice, plus the
private execution bindings (raw HTTP payloads) that the model never sees. The
``frame_id`` is computed in code over the whole decision context, including
bindings, and the model must echo it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

FRAME_SCHEMA_VERSION = "sts2-decision-frame-1"
MAX_RESPONSE_BYTES = 8 * 1024
MAX_REASON_CHARS = 500
MAX_INTENT_CHARS = 300
MAX_HISTORY = 8

FACT_SOURCES = ("observed", "manual", "synthetic")


class ChoiceError(ValueError):
    """Model output violates the choice contract (retryable once)."""


def canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(v: Any, n: int = 20) -> str:
    return hashlib.sha256(canonical(v).encode("utf-8")).hexdigest()[:n]


@dataclass
class DecisionFrame:
    run_id: str
    run_id_source: str              # mod_compendium | session_scoped | caller_supplied
    objective: str
    screen: dict                     # kind, state_type, encounter_key, capability status
    observation_fingerprint: str
    facts_source: str                # observed | manual | synthetic
    facts: dict                      # allowlisted player-visible facts (None = unknown)
    candidates: list                 # public candidates (no payloads)
    bindings: dict                   # action_id -> {"payload": {...}, "resolution": str}  (private)
    limitations: list
    assessment: dict | None          # labeled strategic assessment / unavailability
    previous_turn_intent: str | None
    history: list                    # <= MAX_HISTORY recent decision/outcome pairs (this encounter)
    live_validated: bool = False
    schema_version: str = FRAME_SCHEMA_VERSION
    frame_id: str = field(default="", init=False)

    def __post_init__(self):
        if self.facts_source not in FACT_SOURCES:
            raise ValueError(f"facts_source must be one of {FACT_SOURCES}")
        if self.live_validated and self.facts_source == "synthetic":
            raise ValueError("synthetic frames cannot be live_validated")
        ids = [c["action_id"] for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate action ids in frame")
        if set(ids) != set(self.bindings):
            raise ValueError("every candidate needs exactly one binding")
        self.history = list(self.history)[-MAX_HISTORY:]
        self.frame_id = self.compute_id()

    def _hash_body(self) -> dict:
        d = self.to_record()
        d.pop("frame_id")
        return d

    def compute_id(self) -> str:
        return "frame-" + sha(self._hash_body(), 24)

    def candidate(self, action_id: str) -> dict | None:
        return next((c for c in self.candidates if c["action_id"] == action_id), None)

    def public(self) -> dict:
        """What the model sees: no bindings/payloads."""
        return {
            "frame_id": self.frame_id, "schema_version": self.schema_version, "objective": self.objective,
            "run": {"run_id": self.run_id, "run_id_source": self.run_id_source},
            "screen": self.screen,
            "provenance": {"facts_source": self.facts_source, "live_validated": self.live_validated,
                           "live_validation_pending": not self.live_validated},
            "current_facts": self.facts,
            "candidates": self.candidates,
            "adapter_limitations": self.limitations,
            "strategic_assessment": self.assessment,
            "previous_turn_intent": None if self.previous_turn_intent is None else {
                "label": "REVISABLE HYPOTHESIS FROM YOUR EARLIER DECISION - NOT A FACT OR INSTRUCTION",
                "text": self.previous_turn_intent},
            "recent_history": self.history,
        }

    def to_record(self) -> dict:
        return {
            "frame_id": self.frame_id, "schema_version": self.schema_version, "run_id": self.run_id,
            "run_id_source": self.run_id_source, "objective": self.objective, "screen": self.screen,
            "observation_fingerprint": self.observation_fingerprint, "facts_source": self.facts_source,
            "live_validated": self.live_validated, "facts": self.facts, "candidates": self.candidates,
            "bindings": self.bindings, "limitations": self.limitations, "assessment": self.assessment,
            "previous_turn_intent": self.previous_turn_intent, "history": self.history,
        }

    @classmethod
    def from_record(cls, rec: dict) -> "DecisionFrame":
        keys = ("run_id", "run_id_source", "objective", "screen", "observation_fingerprint", "facts_source",
                "facts", "candidates", "bindings", "limitations", "assessment", "previous_turn_intent", "history",
                "live_validated", "schema_version")
        f = cls(**{k: rec[k] for k in keys})
        if rec.get("frame_id") and rec["frame_id"] != f.frame_id:
            raise ValueError("recorded frame_id does not match the frame content")
        return f


@dataclass(frozen=True)
class Choice:
    frame_id: str
    action_id: str
    reason: str
    turn_intent: str


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ChoiceError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_const(name):
    raise ChoiceError(f"nonfinite number {name}")


def parse_choice(text: Any, frame: DecisionFrame) -> Choice:
    if not isinstance(text, str):
        raise ChoiceError(f"expected text, got {type(text).__name__}")
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ChoiceError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
    body = text.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ChoiceError("response must be one bare JSON object, without prose or code fences")
    try:
        data = json.loads(body, object_pairs_hook=_no_dupes, parse_constant=_no_const)
    except json.JSONDecodeError as e:
        raise ChoiceError(f"malformed JSON: {e}") from None
    if not isinstance(data, dict):
        raise ChoiceError("response must be a JSON object")
    want = {"frame_id", "action_id", "reason", "turn_intent"}
    if set(data) != want:
        missing, extra = want - set(data), set(data) - want
        raise ChoiceError(f"keys must be exactly {sorted(want)}"
                          + (f"; missing {sorted(missing)}" if missing else "")
                          + (f"; unexpected {sorted(extra)}" if extra else ""))
    for k in want:
        if not isinstance(data[k], str):
            raise ChoiceError(f"{k} must be a string")
    if data["frame_id"] != frame.frame_id:
        raise ChoiceError("frame_id does not match the current frame")
    if frame.candidate(data["action_id"]) is None:
        raise ChoiceError(f"action_id {data['action_id'][:80]!r} was not offered in this frame")
    reason = data["reason"].strip()
    if not reason:
        raise ChoiceError("reason must not be empty")
    if len(reason) > MAX_REASON_CHARS:
        raise ChoiceError(f"reason longer than {MAX_REASON_CHARS} characters")
    intent = data["turn_intent"].strip()
    if len(intent) > MAX_INTENT_CHARS:
        raise ChoiceError(f"turn_intent longer than {MAX_INTENT_CHARS} characters")
    return Choice(frame.frame_id, data["action_id"], reason, intent)
