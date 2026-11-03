"""DeepSeek Chat Completions adapter with a shared budget and no retries.

Uses the OpenAI-compatible SDK protocol, but only DEEPSEEK_* credentials and
configuration; no OpenAI account or key is used.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .anthropic import CallRecord, ProviderConfigError, ProviderError, UsageBudget

API_KEY_ENV = "DEEPSEEK_API_KEY"


class DeepSeekTextModel:
    provider = "deepseek"
    is_scripted = False

    def __init__(self, model: str | None, *, max_output_tokens: int = 4096,
                 timeout_s: float = 60.0, budget: UsageBudget | None = None,
                 client: Any = None, clock: Callable[[], float] = time.monotonic):
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigError("set DEEPSEEK_MODEL in .env, [llm] model, or --model")
        self.model = model.strip()
        self.max_output_tokens = int(max_output_tokens)
        self.timeout_s = float(timeout_s)
        self.budget = budget or UsageBudget()
        self.clock = clock
        self.last_call: CallRecord | None = None
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
                "sdk_retries": 0, "scripted": False}

    def complete(self, *, system: str, user: str) -> str:
        self.budget.check_before_call()
        self.budget.calls += 1
        start = self.clock()
        try:
            response = self._client.chat.completions.create(
                model=self.model, messages=[{"role": "system", "content": system},
                                            {"role": "user", "content": user}],
                max_tokens=self.max_output_tokens, stream=False,
                response_format={"type": "json_object"})
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            kind = "timeout" if "Timeout" in type(exc).__name__ else (f"http_{status}" if status else "transport")
            # Never put raw SDK exceptions or response bodies in diagnostics/traces.
            message = f"DeepSeek request failed ({kind})"
            self.last_call = CallRecord(round(self.clock() - start, 3), None, None, None, None, message)
            raise ProviderError(kind, message) from None
        usage = getattr(response, "usage", None)
        it, ot = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
        self.budget.record(it, ot)
        choices = getattr(response, "choices", None) or []
        stop = getattr(choices[0], "finish_reason", None) if choices else None
        self.last_call = CallRecord(round(self.clock() - start, 3), it, ot, stop,
                                    getattr(response, "_request_id", None))
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
        # reasoning_content is deliberately excluded from decisions and traces.
        return text

    def _fail(self, kind: str, message: str) -> ProviderError:
        if self.last_call:
            self.last_call.error = kind
        return ProviderError(kind, message)
