"""Configuration: defaults < TOML file < command-line flags."""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_FILE = "sts_agent.toml"


@dataclass
class Limits:
    max_actions: int = 300                 # per play-combat session (card plays + end turns)
    max_run_actions: int = 3000            # per play-run session (every gameplay action)
    max_elapsed_s: float = 1200.0          # wall-clock cap for one play-combat session
    max_no_progress_s: float = 90.0        # stop if the observed state does not change this long
    poll_interval_s: float = 0.25          # between state polls while waiting
    ready_timeout_s: float = 60.0          # waiting for the player's play phase (covers enemy turns)
    settle_polls: int = 2                  # identical consecutive ready observations required
    play_resolution_timeout_s: float = 10.0
    end_turn_resolution_timeout_s: float = 60.0
    max_consecutive_rejections: int = 3
    max_total_rejections: int = 10


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str | None = None               # REQUIRED for policy="llm"; no default model is assumed
    max_output_tokens: int = 1024
    timeout_s: float = 60.0                # whole-call budget; SDK retries are disabled
    max_calls_per_run: int | None = 2000
    max_input_tokens_per_run: int | None = None
    max_output_tokens_per_run: int | None = None
    price_input_usd_per_mtok: float | None = None   # only for an optional cost estimate
    price_output_usd_per_mtok: float | None = None
    max_input_bytes: int = 96 * 1024       # decision prompt size; oldest history is trimmed first
    objective: str | None = None           # None = ordinary victory at current character/difficulty
    strategic_planner: bool = True


@dataclass
class ExpertStatsConfig:
    enabled: bool = True
    cohort_size: int = 5
    min_character_runs: int = 30
    ascension: int | None = None          # None = observed ascension; never silently widened
    match_version: bool = True            # uses Config.game_build when known
    lang: str = "eng"                     # event title matching uses this catalog language
    cache_dir: str = "traces/spire_codex_cache"
    cache_ttl_s: float = 86400.0
    request_timeout_s: float = 3.0
    max_fetch_s: float = 12.0
    history_enabled: bool = True
    history_runs_per_player: int = 100     # bounded local corpus, not every submitted run
    history_new_runs_per_decision: int = 3
    floor_window: int = 3
    min_context_samples: int = 20
    deck_similarity_min: float = 0.5      # multiset Jaccard over base card IDs
    deck_advice_enabled: bool = True
    route_planning_enabled: bool = True    # map screens: HP-loss model + path enumeration
    route_candidates: int = 5              # non-dominated paths shown to the model
    route_max_routes: int = 2000           # distinct node-type sequences kept per map node
    encounter_stats_bracket: str = "solo"  # /api/runs/encounter-stats fallback bracket
    replan_hp_tolerance_pct: float = 15.0  # % of max HP away from the projection
    replan_gold_tolerance: int = 100


@dataclass
class Config:
    policy: str = "llm"                    # "llm" (default) or "baseline" (explicit comparison mode)
    base_url: str = "http://127.0.0.1:15526"
    read_timeout_s: float = 5.0
    action_timeout_s: float = 10.0
    trace_dir: str = "traces"
    record_raw: bool = True                # write debug_raw.jsonl (not training data)
    game_build: str | None = None          # declare your STS2 build; the API does not report it
    game_dir: str | None = None            # optional, used by `doctor` to read the mod manifest
    limits: Limits = field(default_factory=Limits)
    llm: LLMConfig = field(default_factory=LLMConfig)
    expert_stats: ExpertStatsConfig = field(default_factory=ExpertStatsConfig)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | None) -> tuple[Config, str | None]:
    """Load config. Returns (config, path_used or None)."""
    cfg = Config()
    p = Path(path) if path else Path(DEFAULT_CONFIG_FILE)
    if path and not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    if not p.exists():
        return cfg, None
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    _apply(cfg, data, p)
    return cfg, str(p)


_SECRET_KEYS = {"api_key", "anthropic_api_key", "openai_api_key", "deepseek_api_key", "key", "token", "secret"}


def _apply(cfg: Config, data: dict, p: Path) -> None:
    tables = {"limits": cfg.limits, "llm": cfg.llm, "expert_stats": cfg.expert_stats}
    top = {f.name for f in fields(Config)} - set(tables)
    for k, v in data.items():
        if k in tables:
            if not isinstance(v, dict):
                raise ValueError(f"{p}: [{k}] must be a table")
            obj = tables[k]
            names = {f.name for f in fields(obj)}
            for lk, lv in v.items():
                if lk.lower() in _SECRET_KEYS:
                    raise ValueError(f"{p}: credentials must not be stored in config; use .env or provider environment variables")
                if lk not in names:
                    raise ValueError(f"{p}: unknown [{k}] key {lk!r}")
                cur = getattr(obj, lk)
                setattr(obj, lk, lv if cur is None or lv is None or isinstance(cur, str) else type(cur)(lv))
        elif k in top:
            setattr(cfg, k, v)
        else:
            raise ValueError(f"{p}: unknown config key {k!r}")
    if cfg.policy not in ("llm", "baseline"):
        raise ValueError(f"{p}: policy must be 'llm' or 'baseline'")
