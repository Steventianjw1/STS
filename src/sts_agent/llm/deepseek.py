"""DeepSeek Chat Completions adapter with a shared budget and no SDK retries.

``timeout_s`` is handed to the SDK, where it bounds connecting and each wait for
response data, not the whole call: a response that keeps sending bytes can take
longer. Retrying a failed call is the caller's decision (see LLMDecisionPolicy).

Uses the OpenAI-compatible SDK protocol, but only DEEPSEEK_* credentials and
configuration; no OpenAI account or key is used.

When ``stream_listener`` is set (the interface does this), the same request is
streamed so the model's reasoning can be shown while it is produced. The answer
text, usage and stop reason are assembled from the stream and checked exactly as
a plain response is. If the endpoint rejects a streamed request, the model goes
back to plain calls for the rest of the session. Reasoning text is handed to the
listener only; it is never returned, stored or traced.

Where the time of a call goes is recorded per call (``CallRecord``): input tokens
DeepSeek read from its prompt-prefix cache, output tokens spent on reasoning, and
for streamed calls the wait for the first text. The cache needs no request
parameter: DeepSeek reuses any request prefix it has seen before, so the callers
keep the unchanging part of a prompt at its start (see ``decision/prompts.py``).

``complete(reasoning_effort=...)`` overrides the configured effort for one call;
callers use it for routine screens and, when configured, the strategic planner.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit

from .base import CallRecord, ProviderConfigError, ProviderError, UsageBudget

API_KEY_ENV = "DEEPSEEK_API_KEY"
# "none" switches thinking off for the call. DeepSeek has no distinct medium tier and serves it as high.
REASONING_EFFORTS = ("none", "low", "medium", "high", "max")


def normalize_effort(effort: str | None) -> str | None:
    """The effort value DeepSeek is sent for a configured one; raises on an unknown value."""
    if effort is None:
        return None
    if effort not in REASONING_EFFORTS:
        raise ProviderConfigError("DeepSeek reasoning effort must be none, low, medium, high, or max")
    return "high" if effort == "medium" else effort


class _StreamUnsupported(Exception):
    """The endpoint or SDK cannot serve this request as a stream."""


def _extra(obj, name):
    """A provider-specific field the SDK model keeps outside its declared attributes."""
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        value = extra.get(name) if isinstance(extra, dict) else None
    return value


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


class DeepSeekTextModel:
    provider = "deepseek"
    is_scripted = False
    accepts_reasoning_effort = True   # complete() takes a per-call effort

    def __init__(self, model: str | None, *, max_output_tokens: int | None = None,
                 timeout_s: float = 60.0, budget: UsageBudget | None = None,
                 reasoning_effort: str | None = None,
                 client: Any = None, clock: Callable[[], float] = time.monotonic):
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigError("set DEEPSEEK_MODEL in .env, [llm] model, or --model")
        self.model = model.strip()
        self.max_output_tokens = None if max_output_tokens is None else int(max_output_tokens)
        self.timeout_s = float(timeout_s)
        self.requested_reasoning_effort = reasoning_effort
        self.reasoning_effort = normalize_effort(reasoning_effort)
        self.budget = budget or UsageBudget()
        self.clock = clock
        self.last_call: CallRecord | None = None
        # callable(kind, text) with kind "reasoning" or "answer"; set by the interface, None otherwise.
        self.stream_listener: Callable[[str, str], None] | None = None
        self._stream_unsupported = False
        if client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key or not key.strip():
                raise ProviderConfigError(f"set {API_KEY_ENV} in .env or the environment")
            base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            parsed = urlsplit(base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ProviderConfigError("DEEPSEEK_BASE_URL must be an HTTPS API root without credentials, query or fragment")
            try:
                from openai import OpenAI
            except ImportError:
                raise ProviderConfigError('install the compatible SDK: pip install -e ".[deepseek]"') from None
            client = OpenAI(api_key=key, base_url=base_url, timeout=self.timeout_s, max_retries=0)
        self._client = client

    def identity(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "max_output_tokens": self.max_output_tokens, "timeout_s": self.timeout_s,
                **self._reasoning_options(self.reasoning_effort),
                **({"requested_reasoning_effort": self.requested_reasoning_effort}
                   if self.requested_reasoning_effort is not None else {}),
                "sdk_retries": 0, "scripted": False,
                **({"streamed": True} if self._streaming() else {})}

    def _streaming(self) -> bool:
        return self.stream_listener is not None and not self._stream_unsupported

    def _tell(self, kind: str, text) -> None:
        if self.stream_listener is not None and isinstance(text, str) and text:
            try:
                self.stream_listener(kind, text)
            except Exception:
                pass   # a display problem must never fail a model call

    def _stream(self, request: dict, start: float):
        """Run one streamed request and return it in the shape of a plain response."""
        try:
            stream = self._client.chat.completions.create(stream=True, stream_options={"include_usage": True},
                                                          **request)
        except Exception as exc:
            if getattr(exc, "status_code", None) in (400, 404, 422):
                raise _StreamUnsupported() from None   # the request shape, not the provider, is the problem
            raise
        parts, finish, usage, tools, first = [], None, None, False, None
        try:
            for chunk in stream:
                usage = getattr(chunk, "usage", None) or usage
                for choice in getattr(chunk, "choices", None) or []:
                    if getattr(choice, "index", 0) not in (0, None):
                        raise _StreamUnsupported()
                    delta = getattr(choice, "delta", None)
                    thought = _extra(delta, "reasoning_content")
                    piece = getattr(delta, "content", None)
                    if first is None and ((isinstance(thought, str) and thought) or (isinstance(piece, str) and piece)):
                        first = round(self.clock() - start, 3)
                    self._tell("reasoning", thought)
                    if isinstance(piece, str) and piece:
                        parts.append(piece)
                        self._tell("answer", piece)
                    tools = tools or bool(getattr(delta, "tool_calls", None))
                    finish = getattr(choice, "finish_reason", None) or finish
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        message = SimpleNamespace(content="".join(parts), refusal=None, tool_calls=tools or None)
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason=finish, message=message)],
                               _request_id=None, first_token_s=first)

    @staticmethod
    def _reasoning_options(effort: str | None) -> dict:
        return {"reasoning_effort": effort} if effort is not None else {}

    def complete(self, *, system: str, user: str, reasoning_effort: str | None = None) -> str:
        """One completion. ``reasoning_effort`` replaces the configured effort for this call only."""
        effort = self.reasoning_effort if reasoning_effort is None else normalize_effort(reasoning_effort)
        self.budget.check_before_call()
        self.budget.calls += 1
        start = self.clock()
        request = dict(model=self.model, messages=[{"role": "system", "content": system},
                                                   {"role": "user", "content": user}],
                       **({"max_tokens": self.max_output_tokens} if self.max_output_tokens is not None else {}),
                       response_format={"type": "json_object"}, **self._reasoning_options(effort))
        streamed = False
        try:
            response = None
            if self._streaming():
                try:
                    response, streamed = self._stream(request, start), True
                except _StreamUnsupported:
                    self._stream_unsupported = True
                    self.budget.calls += 1   # the plain call below is a second request
            if response is None:
                response = self._client.chat.completions.create(stream=False, **request)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            kind = "timeout" if "Timeout" in type(exc).__name__ else (f"http_{status}" if status else "transport")
            # Never put raw SDK exceptions or response bodies in diagnostics/traces.
            message = f"DeepSeek request failed ({kind})"
            self.last_call = CallRecord(round(self.clock() - start, 3), None, None, None, None, message,
                                        reasoning_effort=effort)
            raise ProviderError(kind, message) from None
        usage = getattr(response, "usage", None)
        it, ot = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
        cached = _count(_extra(usage, "prompt_cache_hit_tokens"))
        if cached is None:
            cached = _count(getattr(_extra(usage, "prompt_tokens_details"), "cached_tokens", None))
        thought = _count(getattr(_extra(usage, "completion_tokens_details"), "reasoning_tokens", None))
        self.budget.record(it, ot, cache_hit_tokens=cached, reasoning_tokens=thought)
        choices = getattr(response, "choices", None) or []
        stop = getattr(choices[0], "finish_reason", None) if choices else None
        self.last_call = CallRecord(round(self.clock() - start, 3), it, ot, stop,
                                    getattr(response, "_request_id", None), cache_hit_tokens=cached,
                                    reasoning_tokens=thought,
                                    first_token_s=getattr(response, "first_token_s", None) if streamed else None,
                                    reasoning_effort=effort)
        if stop == "length":
            raise self._fail("truncated", "DeepSeek response truncated; no action selected")
        if stop == "content_filter":
            raise self._fail("refused", "the model declined the request")
        if stop != "stop" or len(choices) != 1:
            raise self._fail("unexpected_stop", "DeepSeek response did not complete normally")
        message = choices[0].message
        if getattr(message, "refusal", None):
            raise self._fail("refused", "the model declined the request")
        text = getattr(message, "content", None)
        if getattr(message, "tool_calls", None) or not isinstance(text, str) or not text.strip():
            raise self._fail("non_text", "DeepSeek returned no plain text")
        # reasoning_content is deliberately excluded from decisions and traces; a listener may display it.
        if not streamed:
            self._tell("reasoning", _extra(message, "reasoning_content"))
        return text

    def _fail(self, kind: str, message: str) -> ProviderError:
        if self.last_call:
            self.last_call.error = kind
        return ProviderError(kind, message)
