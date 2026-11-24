"""Claude adapter (Anthropic Messages API) implementing ``TextModel.complete(system=, user=)``.

* The ``anthropic`` SDK is an optional extra (``pip install -e ".[llm]"``) and is
  imported lazily, so offline use and the baseline policy never need it.
* Credentials come only from ``ANTHROPIC_API_KEY``: a key made in the Claude Console
  (platform.claude.com), kept in ``.env`` or the environment. It is never stored in
  config, traces, prompts or diagnostics. A key that is not tied to one workspace also
  needs ``ANTHROPIC_WORKSPACE_ID`` (the workspace's ``wrkspc_`` ID), sent as a header.
* SDK retries are disabled (``max_retries=0``) so every request is counted and timed
  here; retrying a failed call is the caller's decision (see LLMDecisionPolicy).
* Every call is streamed. ``timeout_s`` therefore bounds connecting and each wait for
  data, not the whole call, and a long answer cannot run into an HTTP timeout.
* A call is watched while it streams (``stream_guard.py``). A requested stop (``should_stop``,
  set by the interface) ends it with kind ``interrupted``. While the reasoning summary is
  streamed, text arrives all through a healthy call, so a call that sends none for
  ``stream_first_token_timeout_s`` (before the first text) or ``stream_idle_timeout_s`` (after
  it) ends with kind ``timeout``: that is sooner than the SDK notices a dead connection, and
  the SDK never notices one kept alive by pings, which it drops. Without the summary the
  model thinks in silence, so those two limits do not apply.
* Thinking is adaptive (current Claude models always think); ``effort`` sets how much.
  When ``stream_listener`` is set (the interface does this) a summary of the reasoning
  is asked for and handed to the listener only; it is never returned, stored or traced.
* The system prompt is marked as the cacheable prefix: it repeats on every call of one
  kind, and cached input is cheaper and does not count toward the input rate limit.
* A classifier refusal is retried by the API itself on the model Anthropic recommends
  for it (server-side ``fallbacks``, a beta). If the API rejects that option, the
  session goes on without it. A refusal that remains is reported, never worked around.
* Truncated (``max_tokens``), refused, and text-free responses are rejected.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from .base import CallRecord, ProviderConfigError, ProviderError, UsageBudget, sanitize
from .stream_guard import StreamEnded, StreamGuard

API_KEY_ENV = "ANTHROPIC_API_KEY"
# A Console key that is not tied to one workspace must name the workspace on every request.
WORKSPACE_ENV = "ANTHROPIC_WORKSPACE_ID"
WORKSPACE_HEADER = "anthropic-workspace-id"
DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Thinking counts toward max_tokens. The output rate limit counts generated tokens only, so room costs nothing.
DEFAULT_MAX_OUTPUT_TOKENS = 64000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FENCED = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S | re.I)


def normalize_effort(effort: str | None) -> str | None:
    """The configured effort, checked; None leaves the model's own default."""
    if effort is None:
        return None
    if effort not in EFFORTS:
        raise ProviderConfigError("Claude effort must be low, medium, high, xhigh, or max")
    return effort


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _bare_json(text: str) -> str:
    """The reply without a Markdown fence around its JSON object; the parsers take one bare object."""
    fenced = _FENCED.fullmatch(text.strip())
    return fenced.group(1) if fenced else text


class AnthropicTextModel:
    provider = "anthropic"
    is_scripted = False

    def __init__(self, model: str | None, *, max_output_tokens: int | None = None, timeout_s: float = 60.0,
                 budget: UsageBudget | None = None, effort: str | None = "medium",
                 stream_idle_timeout_s: float | None = 60.0, stream_first_token_timeout_s: float | None = 40.0,
                 client: Any = None, clock: Callable[[], float] = time.monotonic):
        if not model or not isinstance(model, str) or not model.strip():
            raise ProviderConfigError("no Claude model configured: set [llm] anthropic_model or pass --model")
        self.model = model.strip()
        self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS if max_output_tokens is None else int(max_output_tokens)
        self.timeout_s = float(timeout_s)
        self.effort = normalize_effort(effort)
        self.budget = budget or UsageBudget()
        self.clock = clock
        self.last_call: CallRecord | None = None
        # callable(kind, text) with kind "reasoning" or "answer"; set by the interface, None otherwise.
        self.stream_listener: Callable[[str, str], None] | None = None
        # Seconds without text before a call is ended, used while the reasoning summary is streamed; None or 0 = no limit.
        self.stream_idle_timeout_s = stream_idle_timeout_s
        self.stream_first_token_timeout_s = stream_first_token_timeout_s
        # Set by the interface: True once a stop was requested, which ends a call in flight (kind "interrupted").
        self.should_stop: Callable[[], bool] | None = None
        self.fallbacks = True         # server-side refusal fallback; off once the API has rejected the option
        self.workspace_named = False   # True when requests carry the workspace header
        self._secret: str | None = None
        if client is None:
            key = (os.environ.get(API_KEY_ENV) or "").strip()
            if not key:
                raise ProviderConfigError(f"{API_KEY_ENV} is not set: make a key in the Claude Console "
                                          "(platform.claude.com) and put it in .env")
            self._secret = key
            try:
                import anthropic  # lazy: optional dependency
            except ImportError:
                raise ProviderConfigError('the anthropic SDK is not installed: pip install -e ".[llm]"') from None
            workspace = (os.environ.get(WORKSPACE_ENV) or "").strip()
            self.workspace_named = bool(workspace)
            client = anthropic.Anthropic(api_key=key, timeout=self.timeout_s, max_retries=0,
                                         default_headers={WORKSPACE_HEADER: workspace} if workspace else None)
        self._client = client

    def identity(self) -> dict:
        return {"provider": self.provider, "model": self.model, "max_output_tokens": self.max_output_tokens,
                "timeout_s": self.timeout_s, "effort": self.effort, "thinking": "adaptive", "streamed": True,
                "refusal_fallbacks": "default" if self.fallbacks else None, "workspace_named": self.workspace_named,
                "sdk_retries": 0, "scripted": False,
                **({"stream_idle_timeout_s": self.stream_idle_timeout_s,
                    "stream_first_token_timeout_s": self.stream_first_token_timeout_s}
                   if self.stream_listener is not None else {})}

    def _tell(self, kind: str, text) -> None:
        if self.stream_listener is not None and isinstance(text, str) and text:
            try:
                self.stream_listener(kind, text)
            except Exception:
                pass   # a display problem must never fail a model call

    def _request(self, system: str, user: str) -> dict:
        request: dict = dict(
            model=self.model, max_tokens=self.max_output_tokens,
            messages=[{"role": "user", "content": user}],
            # The reasoning itself is never returned; a summary of it is requested only when it is shown.
            thinking={"type": "adaptive", **({"display": "summarized"} if self.stream_listener is not None else {})})
        if system:
            request["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if self.effort is not None:
            request["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            request.update(betas=[FALLBACK_BETA], fallbacks="default")
        return request

    def _stream(self, request: dict, start: float):
        """Run one streamed request; returns (final message, seconds to the first text, request id, longest quiet)."""
        first = None
        # The quiet-stream limits need text to arrive while the model thinks, which only the streamed
        # reasoning summary gives; without it a healthy call sends nothing but pings for a long time.
        shown = self.stream_listener is not None
        with StreamGuard(first_text_s=self.stream_first_token_timeout_s if shown else None,
                         idle_s=self.stream_idle_timeout_s if shown else None,
                         should_stop=self.should_stop) as guard:
            try:
                with self._client.beta.messages.stream(**request) as stream:
                    guard.attach(stream)
                    for event in stream:
                        if getattr(event, "type", None) != "content_block_delta":
                            continue
                        delta = event.delta
                        kind = getattr(delta, "type", None)
                        piece = (getattr(delta, "thinking", None) if kind == "thinking_delta" else
                                 getattr(delta, "text", None) if kind == "text_delta" else None)
                        if isinstance(piece, str) and piece:
                            guard.text()
                            if first is None:
                                first = round(self.clock() - start, 3)
                            self._tell("reasoning" if kind == "thinking_delta" else "answer", piece)
                    result = stream.get_final_message(), first, getattr(stream, "request_id", None)
            except Exception:
                if guard.ended is None:
                    raise   # the stream's own failure; a read the guard cut short is reported below
        if guard.ended is not None:
            guard.ended.first_token_s, guard.ended.longest_quiet_s = first, guard.longest_quiet_s
            raise guard.ended
        return (*result, guard.longest_quiet_s)

    def complete(self, *, system: str, user: str) -> str:
        self.budget.check_before_call()
        self.budget.calls += 1
        start = self.clock()
        try:
            try:
                message, first, request_id, quiet = self._stream(self._request(system, user), start)
            except Exception as exc:
                rejected = (self.fallbacks and getattr(exc, "status_code", None) == 400
                            and "fallback" in str(exc).lower())
                if not rejected:
                    raise
                self.fallbacks = False     # this model or account does not take the option
                self.budget.calls += 1     # the call below is a second request
                message, first, request_id, quiet = self._stream(self._request(system, user), start)
        except Exception as exc:  # SDK errors: APITimeoutError, APIConnectionError, APIStatusError, ...
            msg = sanitize(f"{type(exc).__name__}: {exc}", self._secret)
            status = getattr(exc, "status_code", None)
            kind = "timeout" if "Timeout" in type(exc).__name__ else (f"http_{status}" if status else "transport")
            if status == 400 and WORKSPACE_HEADER in str(exc) and not self.workspace_named:
                kind = "config"   # nothing to retry: the key needs a workspace named for it
                msg = (f"this Claude Console key is not tied to one workspace. Put the workspace's ID (Console > "
                       f"Settings > Workspaces, ID column, starts with wrkspc_) in .env as {WORKSPACE_ENV}, or use "
                       "a key created for one workspace")
            if isinstance(exc, StreamEnded):   # ended here by the guard (quiet stream or requested stop)
                kind, msg = exc.kind, f"Claude call ended: {exc}"
            self.last_call = CallRecord(round(self.clock() - start, 3), None, None, None, None, msg,
                                        first_token_s=getattr(exc, "first_token_s", None),
                                        longest_quiet_s=getattr(exc, "longest_quiet_s", None),
                                        reasoning_effort=self.effort)
            raise ProviderError(kind, msg) from None
        elapsed = round(self.clock() - start, 3)

        usage = getattr(message, "usage", None)
        fresh = _count(getattr(usage, "input_tokens", None))
        cached = _count(getattr(usage, "cache_read_input_tokens", None))
        written = _count(getattr(usage, "cache_creation_input_tokens", None))
        # input_tokens is only what follows the cached prefix; the whole input is the three together.
        it = None if fresh is None else fresh + (cached or 0) + (written or 0)
        ot = getattr(usage, "output_tokens", None)
        thought = _count(getattr(getattr(usage, "output_tokens_details", None), "thinking_tokens", None))
        self.budget.record(it, ot, cache_hit_tokens=cached, reasoning_tokens=thought)
        stop = getattr(message, "stop_reason", None)
        self.last_call = CallRecord(elapsed, it, ot, stop, request_id or getattr(message, "_request_id", None),
                                    cache_hit_tokens=cached, reasoning_tokens=thought, first_token_s=first,
                                    longest_quiet_s=quiet,
                                    reasoning_effort=self.effort)

        details = getattr(message, "stop_details", None)
        dtype = details.get("type") if isinstance(details, dict) else getattr(details, "type", None)
        if stop == "refusal" or dtype == "refusal":
            raise self._fail("refused", "the model declined the request")
        if stop == "max_tokens":
            raise self._fail("truncated", f"response truncated at max_output_tokens={self.max_output_tokens}")
        if stop not in ("end_turn", "stop_sequence"):
            raise self._fail("unexpected_stop", f"unexpected stop_reason {stop!r}")
        # Thinking blocks (and a fallback marker) sit beside the answer; only text blocks are the answer.
        text = "".join(b.text for b in getattr(message, "content", None) or []
                       if getattr(b, "type", None) == "text" and isinstance(getattr(b, "text", None), str))
        if not text.strip():
            raise self._fail("non_text", "response contained no text")
        return _bare_json(text)

    def _fail(self, kind: str, msg: str) -> ProviderError:
        if self.last_call:
            self.last_call.error = kind
        return ProviderError(kind, msg)
