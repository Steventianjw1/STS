"""Configuration: defaults < TOML file < command-line flags."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_FILE = "sts_agent.toml"


@dataclass
class Limits:
    max_actions: int | None = None         # optional explicit per-combat action cap
    max_run_actions: int | None = None     # optional explicit per-run action cap
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
    provider: str = "deepseek"             # "deepseek" or "anthropic" ("claude" means the same); the interface's
                                           # model selector writes its choice here
    model: str | None = None               # model of the provider named above; unset = DEEPSEEK_MODEL for
                                           # DeepSeek, anthropic_model for Claude
    anthropic_model: str = "claude-opus-5-5"   # the model the Claude option calls
    anthropic_effort: str | None = "medium"    # Claude effort: low, medium, high, xhigh or max; None = its default
    max_output_tokens: int | None = None   # omit DeepSeek max_tokens; use the provider's output limit
    timeout_s: float = 60.0                # SDK timeout: per connect and per wait for data, not a whole-call cap
    # Streamed calls: end a call (kind "timeout") when no reasoning or answer text arrives for this long. The
    # SDK timeout does not notice a connection that stays alive without sending text. 0 = no limit. For Claude
    # the limits apply while its reasoning summary is streamed (the interface with live_thinking).
    stream_idle_timeout_s: float = 60.0            # after the first text; healthy streams pause a few seconds at most
    stream_first_token_timeout_s: float = 180.0    # DeepSeek, before the first text: it may hold a request in its queue
    anthropic_stream_first_token_timeout_s: float = 40.0   # Claude, before the first text (normally 1-17 s)
    provider_retries: int | None = None    # None retries recoverable model failures until recovery or interruption
    corrective_retries: int | None = None # None keeps requesting a valid action without ending the run
    deepseek_reasoning_effort: str | None = "medium"  # DeepSeek maps medium to high; None uses its default
    # The strategic planner's own effort ("same" follows the line above). The planner is about half of all
    # model time and most of that is reasoning tokens; "low" measured roughly a third faster per call.
    deepseek_strategy_reasoning_effort: str | None = "same"
    max_calls_per_run: int | None = None
    max_input_tokens_per_run: int | None = None
    max_output_tokens_per_run: int | None = None
    price_input_usd_per_mtok: float | None = None   # only for an optional cost estimate
    price_output_usd_per_mtok: float | None = None
    max_input_bytes: int | None = None    # no local prompt-size cap; provider context limits still apply
    objective: str | None = None           # None = ordinary victory at current character/difficulty
    strategic_planner: bool = True
    what_if_tools: bool = True             # the model may spend one extra call per decision on a what-if calculation


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
    route_candidates: int = 6              # non-dominated paths shown to the model
    route_max_routes: int = 2000           # distinct node-type sequences kept per map node
    encounter_stats_bracket: str = "solo"  # /api/runs/encounter-stats fallback bracket
    replan_hp_tolerance_pct: float = 15.0  # % of max HP away from the projection
    replan_gold_tolerance: int = 100
    elite_readiness_enabled: bool = True   # elite threats vs. observed deck (needs the patched mod's deck)
    elite_block_attack_pct: float = 20.0   # block gap: elite attack turn >= this % of max HP (or Strength gain)
    elite_trigger_steps: int = 3           # re-plan trigger: planned elite this close while gaps remain
    boss_readiness_enabled: bool = True    # card/shop screens: the act boss's deck demands vs. the deck, fight stats, builds
    ancient_advisor_enabled: bool = True   # ancient screens: relic effects vs. deck/state + offer-conditioned stats


@dataclass
class AutobattleConfig:
    enabled: bool = False                  # Spire Mentor's native Autobattle plays whole fights; no LLM combat fallback
    offer_handoff: bool = True             # Mentor holds in-fight card offers; the LLM decides the ones an enemy
                                           # opens, Mentor's own pick answers the rest
    pilot: bool = False                    # the agent's own per-fight combat search decides inside Mentor's advisor
    pilot_source: str = "data/mentor_source"   # Mentor's advisor source (tools/fetch_mentor_source.py)


@dataclass
class UiConfig:
    game_fraction: float = 0.70            # share of the screen's width and height given to the game window
    port: int = 0                          # localhost port of the interface; 0 picks a free one
    browser: str | None = None             # path of a Chromium browser (Edge/Chrome); None = find one
    live_thinking: bool = True             # stream the model's reasoning into the Thinking panel (DeepSeek)
    place_game_window: bool = True         # tiled layout only: move/resize the game window on launch
    single_window: bool = True             # game and panels in one window (Spire Mentor's frame host)


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
    pinned_game_dir: str = "data/game"     # frozen copy of the supported game build lives in <this>/<build>
    monster_data_path: str | None = "data/monster_reference/monsters.json"  # enemy move patterns; missing = unused
    limits: Limits = field(default_factory=Limits)
    llm: LLMConfig = field(default_factory=LLMConfig)
    expert_stats: ExpertStatsConfig = field(default_factory=ExpertStatsConfig)
    autobattle: AutobattleConfig = field(default_factory=AutobattleConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | None) -> tuple[Config, str | None]:
    """Load config. Returns (config, path_used or None).

    A ``game_build`` that names a build the agent has a version for (``game.builds``) puts that
    version in use; any other value only labels traces and filters statistics, as before.
    """
    from .game import builds        # here, not at the top: the game package reads this module's dataclasses
    cfg, used = _load_config(path)
    if cfg.game_build:
        builds.select(cfg.game_build)
    return cfg, used


def _load_config(path: str | None) -> tuple[Config, str | None]:
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
    tables = {"limits": cfg.limits, "llm": cfg.llm, "expert_stats": cfg.expert_stats,
              "autobattle": cfg.autobattle, "ui": cfg.ui}
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


_TABLE = re.compile(r"\s*\[")


def save_llm_setting(path: str | Path, key: str, value: str) -> None:
    """Write ``key = "value"`` into the file's ``[llm]`` table, leaving every other line as written.

    Used by the interface's model selector, so the choice is the one every later command reads.
    Nothing is written unless the result parses and differs from the file only in that key."""
    p = Path(path)
    text = p.read_bytes().decode("utf-8") if p.exists() else ""   # as written, so its line endings are kept
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    entry = f"{key} = {json.dumps(value)}"
    head = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[llm\]\s*(#.*)?", line)), None)
    if head is None:
        lines += ([""] if lines and lines[-1].strip() else []) + ["[llm]", entry]
    else:
        end = next((i for i in range(head + 1, len(lines)) if _TABLE.match(lines[i])), len(lines))
        at = next((i for i in range(head + 1, end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if at is None:
            lines.insert(head + 1, entry)
        else:
            lines[at] = entry
    new = newline.join(lines) + newline
    want = tomllib.loads(text)
    want.setdefault("llm", {})[key] = value
    if tomllib.loads(new) != want:
        raise ValueError(f"{p}: could not place [llm] {key} safely; set it in the file by hand")
    p.write_text(new, encoding="utf-8", newline="")
