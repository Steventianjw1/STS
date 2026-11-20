"""Claude text-completion adapter implementing ``TextModel.complete(system=, user=)``.

* The ``anthropic`` SDK is an optional extra (``pip install -e ".[llm]"``) and is
  imported lazily, so offline use and the baseline policy never need it.
* Credentials come only from the ``ANTHROPIC_API_KEY`` environment variable.
  They are never stored in config, traces, prompts or diagnostics.
* SDK retries are disabled (``max_retries=0``) so every request is counted and
  timed by this adapter; the configured timeout is the whole call budget.
* The model id must be configured explicitly; no default model is assumed.
* Truncated (``max_tokens``), refused, and non-text responses are rejected.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from .base import CallRecord, ProviderConfigError, ProviderError, UsageBudget, sanitize

API_KEY_ENV = "ANTHROPIC_API_KEY"


class AnthropicTextModel:
    provider = "anthropic"
    is_scripted = False

    def __init__(self, model: str | None, *, max_output_tokens: int | None = 1024, timeout_s: float = 60.0,
                 budget: UsageBudget | None = None, client: Any = None,
                 clock: Callable[[], float] = time.monotonic):
        if not model or not isinstance(model, str):
            raise ProviderConfigError("no model configured: set [llm] model in sts_agent.toml or pass --model "
                                      "(use a current model id from the Claude API documentation)")
        self.model = model
        # Anthropic requires max_tokens; retain the existing configured default
        # when the shared config delegates output length to the provider.
        self.max_output_tokens = 16384 if max_output_tokens is None else int(max_output_tokens)
        self.timeout_s = float(timeout_s)
        self.budget = budget or UsageBudget()
        self.clock = clock
        self.last_call: CallRecord | None = None
        self._secret: str | None = None
        if client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise ProviderConfigError(f"{API_KEY_ENV} is not set")
            self._secret = key
            try:
                import anthropic  # lazy: optional dependency
            except ImportError:
                raise ProviderConfigError('the anthropic SDK is not installed: pip install -e ".[llm]"') from None
            client = anthropic.Anthropic(api_key=key, timeout=self.timeout_s, max_retries=0)
        self._client = client

    def identity(self) -> dict:
        return {"provider": self.provider, "model": self.model, "max_output_tokens": self.max_output_tokens,
                "timeout_s": self.timeout_s, "sdk_retries": 0, "scripted": False}

    def complete(self, *, system: str, user: str) -> str:
        self.budget.check_before_call()
        self.budget.calls += 1
        t0 = self.clock()
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=self.max_output_tokens, system=system,
                messages=[{"role": "user", "content": user}])
        except Exception as e:  # SDK errors: APITimeoutError, APIConnectionError, APIStatusError, ...
            msg = sanitize(f"{type(e).__name__}: {e}", self._secret)
            status = getattr(e, "status_code", None)
            kind = "timeout" if "Timeout" in type(e).__name__ else (f"http_{status}" if status else "transport")
            self.last_call = CallRecord(round(self.clock() - t0, 3), None, None, None, None, msg)
            raise ProviderError(kind, msg) from None
        elapsed = round(self.clock() - t0, 3)

        usage = getattr(resp, "usage", None)
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        self.budget.record(it, ot)
        stop = getattr(resp, "stop_reason", None)
        self.last_call = CallRecord(elapsed, it, ot, stop, getattr(resp, "_request_id", None))

        details = getattr(resp, "stop_details", None)
        dtype = details.get("type") if isinstance(details, dict) else getattr(details, "type", None)
        if stop == "refusal" or dtype == "refusal":
            raise self._fail("refused", "the model declined the request")
        if stop == "max_tokens":
            raise self._fail("truncated", f"response truncated at max_output_tokens={self.max_output_tokens}")
        if stop not in ("end_turn", "stop_sequence"):
            raise self._fail("unexpected_stop", f"unexpected stop_reason {stop!r}")
        blocks = list(getattr(resp, "content", None) or [])
        if not blocks or any(getattr(b, "type", None) != "text" for b in blocks):
            raise self._fail("non_text", "response contained no text or non-text content blocks")
        return "".join(b.text for b in blocks)

    def _fail(self, kind: str, msg: str) -> ProviderError:
        if self.last_call:
            self.last_call.error = kind
        return ProviderError(kind, msg)
