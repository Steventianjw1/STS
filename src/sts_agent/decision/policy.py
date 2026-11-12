"""LLM action selection: one frame in, one validated choice (or a typed failure) out.

At most one corrective retry for malformed output or an invalid action id.
Provider failures are not retried. No heuristic fallback exists.

The system prompt is assembled per screen (``prompts.system_instruction``). When the caller
supplies what-if tools, the model may spend one extra call on one tool request before it
chooses; the tool result is appended to the same frame's message. That call is not a retry.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import MAX_RESPONSE_BYTES, Choice, ChoiceError, DecisionFrame, parse_choice, parse_tool_request
from .prompts import DECISION_PROMPT_VERSION, DEFAULT_MAX_INPUT_BYTES, build_user_message, system_instruction

POLICY_NAME = "llm"
POLICY_VERSION = "1.0.0"


@dataclass
class ChoiceResult:
    choice: Choice | None
    error_kind: str | None = None        # provider | invalid_output | input_too_large
    diagnostic: str | None = None
    attempts: list = field(default_factory=list)   # [{"response_text", "error", "call"}]
    history_dropped: int = 0
    support_trim: str | None = None      # decision_support reduction step used for this prompt
    source: str = "llm"                  # "llm" or "spire_mentor_search"
    search: dict | None = None           # combat-search record (hit, or the miss that led to the LLM)
    prompt_modules: list = field(default_factory=list)   # system-prompt modules used for this screen

    @property
    def ok(self) -> bool:
        return self.choice is not None


def model_identity(model) -> dict:
    ident = model.identity() if hasattr(model, "identity") else {"provider": type(model).__name__}
    ident.setdefault("scripted", bool(getattr(model, "is_scripted", False)))
    return ident


class LLMDecisionPolicy:
    name = POLICY_NAME
    version = POLICY_VERSION
    prompt_version = DECISION_PROMPT_VERSION

    def __init__(self, model, *, max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES):
        self.model = model
        self.max_input_bytes = max_input_bytes

    @property
    def scripted(self) -> bool:
        return bool(getattr(self.model, "is_scripted", False))

    def choose(self, frame: DecisionFrame, tools=None) -> ChoiceResult:
        res = ChoiceResult(None)
        offered = tools.available(frame) if tools is not None else []
        system, res.prompt_modules = system_instruction(frame, tools=bool(offered))
        correction, tool_result, attempt = None, None, 0
        while attempt < 2:
            attempt += 1
            try:
                user, res.history_dropped, res.support_trim = build_user_message(
                    frame, correction=correction, max_bytes=self.max_input_bytes,
                    tools=offered if tool_result is None else None, tool_result=tool_result)
            except ValueError as e:
                res.error_kind, res.diagnostic = "input_too_large", str(e)
                return res
            try:
                text = self.model.complete(system=system, user=user)
            except Exception as e:
                res.attempts.append({"attempt": attempt, "response_text": None, "error": f"{type(e).__name__}: {e}"[:300],
                                     "call": _call(self.model)})
                res.error_kind = "provider"
                res.diagnostic = f"{getattr(e, 'kind', type(e).__name__)}: {e}"[:300]
                return res
            rec = {"attempt": attempt,
                   "response_text": text[:MAX_RESPONSE_BYTES] if isinstance(text, str) else None,
                   "error": None, "call": _call(self.model)}
            res.attempts.append(rec)
            request = parse_tool_request(text) if offered and tool_result is None else None
            if request is not None:   # one what-if call per decision; it does not use up the corrective retry
                tool_result = tools.run(request["tool"], request["args"], frame)
                rec["tool_call"] = {"request": request, "result": tool_result}
                attempt -= 1
                continue
            try:
                res.choice = parse_choice(text, frame)
                return res
            except ChoiceError as e:
                rec["error"] = str(e)[:300]
                correction = str(e)
        res.error_kind = "invalid_output"
        res.diagnostic = f"model output invalid after one corrective retry: {res.attempts[-1]['error']}"
        return res


def _call(model):
    lc = getattr(model, "last_call", None)
    return lc.to_dict() if hasattr(lc, "to_dict") else None
