"""What every model provider shares: error types, secret scrubbing, the per-run budget and the per-call record."""
from __future__ import annotations

import re
from dataclasses import dataclass


class ProviderError(Exception):
    """A provider call failed. ``kind`` is a short machine-readable category."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class ProviderConfigError(ProviderError):
    def __init__(self, message: str):
        super().__init__("config", message)


_SECRET_PATTERNS = [re.compile(r"sk-[A-Za-z0-9_\-]+"), re.compile(r"(?i)(x-api-key|authorization)\s*[:=]\s*\S+")]


def sanitize(text: str, secret: str | None = None, limit: int = 300) -> str:
    out = text or ""
    if secret:
        out = out.replace(secret, "[REDACTED]")
    for p in _SECRET_PATTERNS:
        out = p.sub("[REDACTED]", out)
    return out[:limit]


@dataclass
class UsageBudget:
    """Per-run limits on model calls and tokens; ``None`` = unlimited."""

    max_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    price_input_usd_per_mtok: float | None = None
    price_output_usd_per_mtok: float | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0      # input tokens the provider served from its prompt-prefix cache
    reasoning_tokens: int = 0      # output tokens spent on reasoning, where the provider reports them

    def check_before_call(self) -> None:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ProviderError("budget_exhausted", f"model call budget exhausted ({self.calls}/{self.max_calls})")
        if self.max_input_tokens is not None and self.input_tokens >= self.max_input_tokens:
            raise ProviderError("budget_exhausted", f"input token budget exhausted ({self.input_tokens})")
        if self.max_output_tokens is not None and self.output_tokens >= self.max_output_tokens:
            raise ProviderError("budget_exhausted", f"output token budget exhausted ({self.output_tokens})")

    def record(self, input_tokens: int | None, output_tokens: int | None, *,
               cache_hit_tokens: int | None = None, reasoning_tokens: int | None = None) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cache_hit_tokens += cache_hit_tokens or 0
        self.reasoning_tokens += reasoning_tokens or 0

    def summary(self) -> dict:
        d = {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
             "input_cache_hit_tokens": self.cache_hit_tokens,
             "input_cache_hit_pct": round(self.cache_hit_tokens / self.input_tokens * 100, 1) if self.input_tokens else None,
             "reasoning_tokens": self.reasoning_tokens,
             "limits": {"max_calls": self.max_calls, "max_input_tokens": self.max_input_tokens,
                        "max_output_tokens": self.max_output_tokens}}
        if self.price_input_usd_per_mtok is not None and self.price_output_usd_per_mtok is not None:
            d["estimated_cost_usd"] = round(self.input_tokens / 1e6 * self.price_input_usd_per_mtok
                                            + self.output_tokens / 1e6 * self.price_output_usd_per_mtok, 6)
            d["cost_basis"] = "configured prices; cache hits are counted at the ordinary input price"
        return d


@dataclass
class CallRecord:
    elapsed_s: float
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str | None
    request_id: str | None
    error: str | None = None
    # Where the time went, when the provider reports it (None = not reported).
    cache_hit_tokens: int | None = None    # input tokens read from the provider's prompt-prefix cache
    reasoning_tokens: int | None = None    # part of output_tokens spent on reasoning
    first_token_s: float | None = None     # streamed calls: seconds until the first reasoning or answer text
    longest_quiet_s: float | None = None   # streamed calls: longest wait for more text after the first
    reasoning_effort: str | None = None    # effort this call was sent with

    def to_dict(self) -> dict:
        return dict(self.__dict__)
