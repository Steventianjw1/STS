"""Command-line interface.

    sts-agent doctor          check config, connectivity, versions, LLM setup (no model calls)
    sts-agent observe         print the current state (read-only, no model calls)
                              --save-frame F: save a decision frame; --preview-decision: ask the
                              configured model what it WOULD do (billable; never executes)
    sts-agent step            execute exactly ONE decision on the current screen and record it
    sts-agent play-combat     act until the current fight ends or a stop condition triggers
    sts-agent play-run        continue an already-started run across supported screens (LLM only)
    sts-agent start-run       prompt for character/ascension, replace the old run, then play from Neow
    sts-agent decide-frame F  choose from a saved decision frame offline (never executes)
    sts-agent replay PATH     baseline: re-run the policy; LLM: validate recorded decisions (no model call)
    sts-agent capabilities    print the screen capability table

The gameplay policy defaults to "llm". "baseline" is an explicit comparison mode
(--policy baseline or policy = "baseline"); it is never used as a fallback.
Nothing is sent to the game unless you run step, play-combat, play-run or start-run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from . import AGENT_VERSION, STS2MCP_SOURCE_DESCRIBE
from .client import GameClient, GameClientError, parse_mod_version
from .config import load_config
from . import enemy_patterns
from .decision.coordinator import LLM_PROVENANCE_LIVE, LLM_PROVENANCE_SCRIPTED, RunCoordinator
from .decision.models import DecisionFrame
from .decision.offline import ScriptedTextModel
from .decision.policy import LLMDecisionPolicy, model_identity
from .decision.search_policy import SearchCombatPolicy
from .spire_mentor import SpireMentorAdvisor
from .decision.prompts import DECISION_PROMPT_VERSION, DEFAULT_OBJECTIVE
from .decision.screens import CAPABILITIES, MOD_REVISION, build_view, capability_table
from .observation import normalize, summarize
from .policy import BaselinePolicy
from .providers import API_KEY_ENV, AnthropicTextModel, ProviderConfigError, UsageBudget
from .providers.deepseek import DeepSeekTextModel
from .env import load_env
from .expert_stats import ExpertDecisionStats, StatsUnavailable
from .replay import decide_from_observation_file, replay_trace
from .runner import Runner
from .strategic import StrategicPlanner
from .startup import RunStartup, StartupError, ascension_level, character_id, prompt_settings
from .trace import LLM_TRACE_FORMAT_VERSION, TraceRecorder, new_session_id

# What this adapter has actually been checked against.
SOURCE_VERIFIED_MOD = "0.4.0 + commits through 55e0648 (STS2 v0.107 compatibility fix)"


def _build(args):
    load_env(Path(args.config).resolve().parent / ".env" if args.config else ".env")
    cfg, used = load_config(args.config)
    if cfg.llm.provider == "deepseek" and not cfg.llm.model:
        cfg.llm.model = os.environ.get("DEEPSEEK_MODEL") or None
    if args.base_url:
        cfg.base_url = args.base_url
    if getattr(args, "trace_dir", None):
        cfg.trace_dir = args.trace_dir
    for name in ("max_actions", "max_elapsed_s", "max_no_progress_s"):
        v = getattr(args, name, None)
        if v is not None:
            target = "max_run_actions" if (name == "max_actions" and getattr(args, "cmd", None) in ("play-run", "start-run")) else name
            setattr(cfg.limits, target, v)
    if getattr(args, "no_raw", False):
        cfg.record_raw = False
    if getattr(args, "policy", None):
        cfg.policy = args.policy
    if getattr(args, "model", None):
        cfg.llm.model = args.model
    enemy_patterns.configure(cfg.monster_data_path, game_build=cfg.game_build)
    client = GameClient(cfg.base_url, cfg.read_timeout_s, cfg.action_timeout_s)
    return cfg, used, client


class SetupError(Exception):
    pass


def _build_model(cfg):
    """The single configured provider instance shared by decisions and the strategic planner."""
    providers = {"anthropic": AnthropicTextModel, "deepseek": DeepSeekTextModel}
    if cfg.llm.provider not in providers:
        raise SetupError(f"unknown LLM provider {cfg.llm.provider!r} (supported: deepseek, anthropic)")
    budget = UsageBudget(max_calls=cfg.llm.max_calls_per_run, max_input_tokens=cfg.llm.max_input_tokens_per_run,
                         max_output_tokens=cfg.llm.max_output_tokens_per_run,
                         price_input_usd_per_mtok=cfg.llm.price_input_usd_per_mtok,
                         price_output_usd_per_mtok=cfg.llm.price_output_usd_per_mtok)
    try:
        options = {}
        if cfg.llm.provider == "deepseek" and cfg.llm.deepseek_reasoning_effort is not None:
            options["reasoning_effort"] = cfg.llm.deepseek_reasoning_effort
        return providers[cfg.llm.provider](cfg.llm.model, max_output_tokens=cfg.llm.max_output_tokens,
                                  timeout_s=cfg.llm.timeout_s, budget=budget, **options)
    except ProviderConfigError as e:
        raise SetupError(f"LLM policy is not set up: {e}. Nothing was sent. "
                         "(Use --policy baseline only if you explicitly want the heuristic comparison mode.)") from None


def _advisor(cfg) -> SpireMentorAdvisor:
    cs = cfg.combat_search
    return SpireMentorAdvisor(cs.control_url, advisor_log_dir=cs.advisor_log_dir,
                              advice_timeout_s=cs.advice_timeout_s, poll_interval_s=cs.poll_interval_s,
                              request_timeout_s=cs.request_timeout_s, cooldown_s=cs.cooldown_s)


def _doctor_combat_search(cfg, line) -> None:
    adv = _advisor(cfg)
    status = adv.status()
    if status is None:
        line("WARN", f"combat search: no Spire Mentor advisor at {cfg.combat_search.control_url}. Combat will "
                     "fall back to the LLM. Is the Spire Mentor mod installed with its coach turned on?")
        return
    line("ok" if status.get("connected") else "WARN",
         f"combat search: advisor reachable; connected to the mod: {bool(status.get('connected'))}")
    log = adv.find_log()
    line("ok" if log else "WARN", "combat search: advisor log " + (f"found ({log.name})" if log else
         "not found yet (it appears after the first advice; else set [combat_search].advisor_log_dir)"))


def _objective(cfg) -> str:
    return cfg.llm.objective or DEFAULT_OBJECTIVE


def _frame_from_view(view, *, run_id, run_id_source, facts_source, objective, expert_stats=None) -> DecisionFrame:
    cap = CAPABILITIES.get(view.kind, {})
    return DecisionFrame(
        run_id=run_id, run_id_source=run_id_source, objective=objective,
        screen={"kind": view.kind, "state_type": view.state_type, "encounter_key": view.encounter_key,
                "capability": {"status": cap.get("status"), "live_validated": False, "mod_revision": MOD_REVISION}},
        observation_fingerprint=view.fingerprint, facts_source=facts_source, facts=view.facts,
        candidates=view.candidates, bindings=view.bindings, limitations=view.limitations,
        assessment={"status": "not_requested_in_preview"}, previous_turn_intent=None, history=[],
        decision_support=expert_stats.for_view(view) if expert_stats is not None else None)


def _versions(client) -> tuple[str | None, str | None]:
    mod_version = run_id = None
    try:
        mod_version = parse_mod_version(client.hello().get("message"))
    except GameClientError:
        pass
    try:
        comp = client.get_optional("/api/v1/compendium")
        if comp and isinstance(comp.get("current_run"), dict):
            run_id = comp["current_run"].get("run_id")  # seed deliberately not read
    except GameClientError:
        pass
    return mod_version, run_id


# -- doctor --------------------------------------------------------------------
def cmd_doctor(args) -> int:
    ok = True

    def line(status, msg):
        print(f"[{status:4}] {msg}")

    line("info", f"sts-agent {AGENT_VERSION}; adapter written against STS2MCP {STS2MCP_SOURCE_DESCRIBE}")
    if sys.version_info < (3, 11):
        line("FAIL", f"Python {sys.version.split()[0]} (need 3.11+)")
        ok = False
    else:
        line("ok", f"Python {sys.version.split()[0]}")
    try:
        cfg, used, client = _build(args)
    except Exception as e:
        line("FAIL", f"config: {e}")
        return 1
    line("ok", f"config: {used or 'defaults (no sts_agent.toml found)'}; base_url={cfg.base_url}")
    line("info", "limits: " + ", ".join(f"{k}={v}" for k, v in cfg.limits.__dict__.items()))
    line("info", f"declared game_build: {cfg.game_build or 'not set (the API does not report the game build)'}")
    line("info", f"gameplay policy: {cfg.policy}" + (" (explicit comparison mode)" if cfg.policy == "baseline" else ""))
    if cfg.policy == "llm":
        provider = cfg.llm.provider
        sdk_name = "openai" if provider == "deepseek" else provider
        sdk = provider in ("deepseek", "anthropic") and importlib.util.find_spec(sdk_name) is not None
        key_env = "DEEPSEEK_API_KEY" if provider == "deepseek" else API_KEY_ENV
        key = bool(os.environ.get(key_env))
        line("ok" if cfg.llm.model else "FAIL", f"llm.model: {cfg.llm.model or 'NOT SET (required)'} "
             f"(provider {cfg.llm.provider}, timeout {cfg.llm.timeout_s}s, max_output_tokens {cfg.llm.max_output_tokens})")
        extra = "deepseek" if provider == "deepseek" else "llm"
        line("ok" if sdk else "FAIL", f"{provider} SDK " + ("importable" if sdk else f'missing: pip install -e ".[{extra}]"'))
        line("ok" if key else "FAIL", f"{key_env} " + ("is set" if key else "is not set"))
        line("info", f"budget per run: calls={cfg.llm.max_calls_per_run}, input_tokens={cfg.llm.max_input_tokens_per_run}, "
                     f"output_tokens={cfg.llm.max_output_tokens_per_run}")
        ok = ok and bool(cfg.llm.model) and sdk and key
        line("info", "doctor made no model calls")

    if cfg.game_dir:
        _doctor_game_dir(Path(cfg.game_dir), line)
    if cfg.policy == "llm" and cfg.combat_search.enabled:
        _doctor_combat_search(cfg, line)

    try:
        hello = client.hello()
    except GameClientError as e:
        line("FAIL", f"cannot reach the mod at {cfg.base_url}: {e}")
        print("       Is the game running with mods enabled and STS2_MCP installed? See README 'Install the mod'.")
        return 1
    mod_version = parse_mod_version(hello.get("message"))
    line("ok", f"mod reachable: {hello.get('message')!r} (version {mod_version or 'unknown'})")
    if mod_version == "0.4.0":
        has_comp = client.get_optional("/api/v1/compendium") is not None
        if has_comp:
            line("ok", "mod build includes post-0.4.0 endpoints (/compendium) -> built from source after the release")
        else:
            line("WARN", "this looks like the 0.4.0 release DLL, which predates the STS2 v0.107 fix (commit 55e0648). "
                         "If your game is v0.107+, combat reads/actions may fail; build the mod from source.")
    elif mod_version:
        line("WARN", f"mod version {mod_version} has not been checked by this adapter (checked: {SOURCE_VERIFIED_MOD})")

    try:
        raw = client.get_state()
    except GameClientError as e:
        line("FAIL", f"GET /api/v1/singleplayer failed: {e}")
        return 1
    obs = normalize(raw)
    line("ok", f"state readable: state_type={obs['state_type']} phase={obs['phase']}")
    char, asc = obs["run"]["character"], obs["run"]["ascension"]
    if char is not None and "ironclad" not in char.lower():
        line("WARN", f"character is {char!r}; Day-1 target is the Ironclad")
    if asc not in (None, 0):
        line("WARN", f"ascension {asc}; Day-1 target is A0")
    texts = [c["description"] or "" for c in obs["hand"] or []] + [c["name"] or "" for c in obs["hand"] or []]
    if any(not t.isascii() for t in texts):
        line("WARN", "card text is not English. The Day-1 effect parser only reads English descriptions "
                     "('Deal N damage.', 'Gain N Block.'). Set the game language to English.")
    unknown_cards = [c["name"] for c in obs["hand"] or [] if not c["effects"]["fully_supported"]]
    if unknown_cards:
        line("info", "cards in hand without supported effect text: " + ", ".join(unknown_cards))
    line("info", "doctor sent no gameplay actions")
    return 0 if ok else 1


def _doctor_game_dir(gd: Path, line) -> None:
    if not gd.exists():
        line("WARN", f"game_dir {gd} does not exist")
        return
    manifest = gd / "mods" / "STS2_MCP.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            line("ok", f"installed mod manifest: version {m.get('version')} ({manifest})")
        except Exception as e:
            line("WARN", f"cannot read {manifest}: {e}")
    else:
        line("WARN", f"no mod manifest at {manifest}")
    for name in ("release_info.json", "version.txt", "version.json"):
        f = gd / name
        if f.exists():
            line("info", f"{name}: {f.read_text(encoding='utf-8', errors='replace')[:300].strip()}")


# -- observe -------------------------------------------------------------------
def cmd_observe(args) -> int:
    cfg, _, client = _build(args)
    try:
        raw = client.get_state()
    except GameClientError as e:
        print(f"cannot read state: {e}", file=sys.stderr)
        return 2
    obs = normalize(raw)
    print(json.dumps(obs, indent=2, ensure_ascii=False) if args.json else summarize(obs))
    from .actions import build_candidates
    cands, excluded = build_candidates(obs)
    if cands and not args.json:
        print("Candidate actions: " + ", ".join(c["action_id"] for c in cands))
        if excluded:
            print("Not offered: " + "; ".join(f"[{x['index']}] {x['name']}: {x['reason']}" for x in excluded))
        d = BaselinePolicy().decide(obs, cands)
        print(f"Baseline heuristic (comparison mode) would choose: {d['action_id']} ({d['rule']}) - "
              f"{d['reason']}  [not executed; use --preview-decision for the LLM]")
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(json.dumps({"observation": obs}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"saved observation -> {args.save}")
    if args.save_raw:
        Path(args.save_raw).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_raw).write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"saved raw API response (debug/fixture use, not policy input) -> {args.save_raw}")
    if args.save_frame or args.preview_decision:
        view = build_view(raw)
        print(f"Screen: {view.kind} (ready={view.ready}, capability={view.capability['status']})"
              + (f"  UNSUPPORTED: {view.unsupported}" if view.unsupported else ""))
        if not view.ready or not view.candidates:
            print("No decision frame: the screen is not ready or offers no supported actions.")
            return 0
        _, run_id = _versions(client)
        frame = _frame_from_view(view, run_id=run_id or "preview", run_id_source="mod_compendium" if run_id else
                                 "preview_only", facts_source="observed", objective=_objective(cfg),
                                 expert_stats=ExpertDecisionStats(cfg.expert_stats, game_build=cfg.game_build))
        if args.save_frame:
            Path(args.save_frame).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save_frame).write_text(json.dumps(frame.to_record(), indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"saved decision frame {frame.frame_id} -> {args.save_frame} (no model call)")
        if args.preview_decision:
            return _decide_and_print(cfg, frame, scripted_reply=None)
    return 0


def _decide_and_print(cfg, frame: DecisionFrame, scripted_reply: str | None) -> int:
    if scripted_reply is not None:
        model = ScriptedTextModel([scripted_reply])
        print("NOTE: using a SCRIPTED reply (synthetic test double; not LLM reasoning).")
    else:
        try:
            model = _build_model(cfg)
        except SetupError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"Calling {cfg.llm.provider}:{cfg.llm.model} (billable). Nothing will be executed.")
    res = LLMDecisionPolicy(model, max_input_bytes=cfg.llm.max_input_bytes).choose(frame)
    out = {"frame_id": frame.frame_id, "model": model_identity(model), "executed": False,
           "ok": res.ok, "error_kind": res.error_kind, "diagnostic": res.diagnostic,
           "attempts": len(res.attempts)}
    if res.ok:
        c = res.choice
        out["choice"] = {"action_id": c.action_id, "description": frame.candidate(c.action_id)["description"],
                         "reason": c.reason, "turn_intent": c.turn_intent}
    budget = getattr(model, "budget", None)
    if budget is not None:
        out["usage"] = budget.summary()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if res.ok else 1


def cmd_decide_frame(args) -> int:
    cfg, _, _ = _build(args)
    try:
        frame = DecisionFrame.from_record(json.loads(Path(args.frame).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"invalid frame file: {e}", file=sys.stderr)
        return 2
    return _decide_and_print(cfg, frame, args.scripted_reply)


def cmd_capabilities(args) -> int:
    print(f"STS2MCP source revision inspected: {MOD_REVISION} (read-only source review; no live validation)")
    for row in capability_table():
        print(f"- {row['screen']:15} {row['status']:17} live_validated={row['live_validated']}  "
              f"actions={','.join(row['actions']) or '-'}\n    {row['notes']}")
    return 0


# -- step / play-combat ----------------------------------------------------------
def _run(args, command: str) -> int:
    cfg, _, client = _build(args)
    if command == "play-run" and cfg.policy != "llm":
        print("play-run requires policy 'llm'; the baseline only plays combat.", file=sys.stderr)
        return 2
    model = None
    if cfg.policy == "llm":
        try:
            model = _build_model(cfg)   # fail before touching the game
        except SetupError as e:
            print(str(e), file=sys.stderr)
            return 2
    try:
        client.hello()
    except GameClientError as e:
        print(f"cannot reach the mod at {cfg.base_url}: {e}", file=sys.stderr)
        return 2
    if cfg.policy == "llm":
        return run_llm_session(cfg, client, model, command)
    mod_version, run_id = _versions(client)
    policy = BaselinePolicy()
    rec = TraceRecorder(cfg.trace_dir, command=command, policy_name=policy.name, policy_version=policy.version,
                        mod_version=mod_version, game_build=cfg.game_build, run_id=run_id,
                        config_snapshot=_shareable_config(cfg), record_raw=cfg.record_raw)
    print(f"session {rec.session_id} -> {rec.dir}")
    runner = Runner(client, policy, rec, cfg.limits)
    try:
        summary = runner.play_combat() if command == "play-combat" else runner.run_single_step()
    except BaseException as e:
        # Unexpected crash: record it and close the trace before re-raising.
        rec.error("unexpected_exception", {"type": type(e).__name__, "message": str(e)})
        rec.close({"stop_reason": "crashed", "detail": f"{type(e).__name__}: {e}"})
        raise
    rec.close(summary)
    print(json.dumps(summary, indent=2))
    good = {"play-combat": {"combat_ended"}, "step": {"step_done", "combat_ended"}}[command]
    return 0 if summary["stop_reason"] in good else 1


def _shareable_config(cfg) -> dict:
    """Config for the manifest, minus local filesystem paths."""
    d = cfg.to_dict()
    d.pop("game_dir", None)
    d.pop("trace_dir", None)
    d.get("expert_stats", {}).pop("cache_dir", None)
    d.get("combat_search", {}).pop("advisor_log_dir", None)
    return d


def _run_state_path(cfg, run_id: str) -> str:
    """Same-run resume file (route plan, fight outcomes, encounter memory). Local, under the trace directory."""
    import os
    import re
    return os.path.join(cfg.trace_dir, "run_state", re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id))[:120] + ".json")


def run_llm_session(cfg, client, model, command: str, *, facts_source: str = "observed", log=print) -> int:
    """LLM session over any client (live GameClient or an injected scripted client)."""
    mod_version, run_id = _versions(client)
    session_id = new_session_id()
    run_id_source = "mod_compendium"
    if not run_id:
        run_id, run_id_source = f"session:{session_id}", "session_scoped"
    scripted = bool(getattr(model, "is_scripted", False))
    if facts_source == "synthetic" or getattr(client, "data_source", None) == "synthetic":
        facts_source = "synthetic"
    policy = LLMDecisionPolicy(model, max_input_bytes=cfg.llm.max_input_bytes)
    # Live observed combat only: synthetic or scripted sessions never consult a running advisor.
    if cfg.combat_search.enabled and facts_source != "synthetic" and not scripted:
        policy = SearchCombatPolicy(policy, _advisor(cfg))
        log(f"combat search: Spire Mentor advisor at {cfg.combat_search.control_url} (LLM on any miss)")
    planner = StrategicPlanner(model) if cfg.llm.strategic_planner else None
    ident = model_identity(model)
    rec = TraceRecorder(cfg.trace_dir, command=command, policy_name=policy.name, policy_version=policy.version,
                        mod_version=mod_version, game_build=cfg.game_build, run_id=run_id,
                        config_snapshot=_shareable_config(cfg), record_raw=cfg.record_raw, session_id=session_id,
                        trace_format=LLM_TRACE_FORMAT_VERSION,
                        provenance=LLM_PROVENANCE_SCRIPTED if scripted else {**LLM_PROVENANCE_LIVE,
                                                                             "facts_source": facts_source,
                                                                             "live_validated": False},
                        extra_manifest={"model": ident, "decision_prompt_version": DECISION_PROMPT_VERSION,
                                        "run_id_source": run_id_source, "facts_source": facts_source,
                                        "capabilities_mod_revision": MOD_REVISION})
    log(f"session {rec.session_id} -> {rec.dir}  (policy llm, model {ident.get('model')}"
        f"{', SCRIPTED' if scripted else ''})")
    scope = {"step": "step", "play-combat": "combat", "play-run": "run"}[command]
    coord = RunCoordinator(client, policy, planner=planner, recorder=rec, limits=cfg.limits, scope=scope,
                           expert_stats=ExpertDecisionStats(cfg.expert_stats, game_build=cfg.game_build)
                           if facts_source != "synthetic" and not scripted else None,
                           run_id=run_id, run_id_source=run_id_source, facts_source=facts_source,
                           objective=_objective(cfg), model_timeout_s=getattr(model, "timeout_s", 0.0), log=log,
                           state_path=_run_state_path(cfg, run_id) if run_id_source == "mod_compendium"
                           and facts_source == "observed" and not scripted else None)
    try:
        summary = coord.run()
    except BaseException as e:
        rec.error("unexpected_exception", {"type": type(e).__name__, "message": str(e)[:300]})
        rec.close({"stop_reason": "crashed", "detail": f"{type(e).__name__}: {e}"[:300]})
        raise
    rec.update_manifest(model_usage=summary.get("model_usage"))
    rec.close(summary)
    log(json.dumps(summary, indent=2, ensure_ascii=False))
    good = {"step": {"step_done", "combat_ended"}, "play-combat": {"combat_ended"},
            "play-run": {"run_over_unconfirmed"}}[command]
    return 0 if summary["stop_reason"] in good else 1


def cmd_step(args) -> int:
    return _run(args, "step")


def cmd_play(args) -> int:
    return _run(args, "play-combat")


def cmd_play_run(args) -> int:
    return _run(args, "play-run")


def cmd_start_run(args) -> int:
    try:
        character, ascension = prompt_settings(args.character, args.ascension)
        cfg, _, client = _build(args)
        if cfg.policy != "llm":
            raise SetupError("start-run requires policy 'llm'.")
        model = _build_model(cfg)  # Validate provider before replacing the current run; no model call.
        client.hello()
        RunStartup(client, timeout_s=args.setup_timeout_s,
                   poll_interval_s=cfg.limits.poll_interval_s).start(character, ascension)
    except (EOFError, KeyboardInterrupt):
        print("Run setup cancelled.", file=sys.stderr)
        return 130
    except (StartupError, SetupError, GameClientError, OSError, ValueError) as exc:
        print(f"Cannot start run: {exc}", file=sys.stderr)
        return 2
    return run_llm_session(cfg, client, model, "play-run")


# -- replay ----------------------------------------------------------------------
def cmd_replay(args) -> int:
    p = Path(args.path)
    policy = BaselinePolicy()
    if p.is_file() and p.suffix == ".json":
        out = decide_from_observation_file(p, policy)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    rep = replay_trace(p, policy)
    for r in rep["results"]:
        if args.verbose or not r["match"]:
            mark = "ok  " if r["match"] else "DIFF"
            print(f"{mark} seq={r['seq']} recorded={r['recorded_action']} replayed={r['replayed_action']} ({r['rule']})")
            for prob in r["problems"]:
                print(f"       {prob}")
            if r["policy_version_note"]:
                print(f"       note: {r['policy_version_note']}")
    print(f"{rep['matches']}/{rep['decisions']} decisions reproduced; corrupt lines: {rep['corrupt_lines']} ({rep['file']})")
    return 0 if rep["matches"] == rep["decisions"] and rep["corrupt_lines"] == 0 else 1


def cmd_sync_history(args) -> int:
    cfg, _ = load_config(args.config)
    stats = ExpertDecisionStats(cfg.expert_stats, game_build=cfg.game_build)
    if not cfg.expert_stats.enabled or not cfg.expert_stats.history_enabled:
        print("expert_stats history is disabled in config")
        return 1

    def progress(total, result):
        print(f"  indexed {total} new run(s); " + ", ".join(
            f"{p['username']}={p.get('indexed_runs', '?')}:{p['status']}" for p in result["players"]))
    try:
        result = stats.bulk_sync(args.character, args.ascension, max_new_runs=args.max_new_runs,
                                 max_seconds=args.max_seconds, progress=progress)
    except StatsUnavailable as exc:
        print(f"sync unavailable: {exc}")
        return 1
    print(f"done: {result['new_runs']} new run(s) for {', '.join(result['cohort'])} "
          f"(character={result['scope']['character']}, A{result['scope']['ascension']}, "
          f"version={result['scope']['version'] or 'all'})")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # never crash on a non-UTF-8 Windows console (e.g. GBK)
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="sts-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="TOML config file (default: ./sts_agent.toml if present)")
    ap.add_argument("--base-url", help="mod API address, e.g. http://127.0.0.1:15526")
    ap.add_argument("--policy", choices=["llm", "baseline"],
                    help="gameplay policy (default from config: llm). baseline = explicit heuristic comparison mode")
    ap.add_argument("--model", help="provider model ID (overrides [llm] model and DEEPSEEK_MODEL)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check config, connectivity, versions and LLM setup (no model calls)").set_defaults(fn=cmd_doctor)

    o = sub.add_parser("observe", help="print the current state (read-only)")
    o.add_argument("--save", help="write the normalized observation to this JSON file")
    o.add_argument("--save-raw", help="write the raw API response to this JSON file (debug/fixtures)")
    o.add_argument("--json", action="store_true", help="print the normalized observation as JSON")
    o.add_argument("--save-frame", help="write the LLM decision frame for this screen (no model call)")
    o.add_argument("--preview-decision", action="store_true",
                   help="ask the configured model what it would choose (BILLABLE; never executes)")
    o.set_defaults(fn=cmd_observe)

    for name, fn, hlp in (("step", cmd_step, "execute exactly one decision (any supported screen with llm; combat with baseline)"),
                          ("play-combat", cmd_play, "play until the current fight ends or a stop condition"),
                          ("play-run", cmd_play_run, "LLM plays the already-started run across supported screens")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--trace-dir")
        s.add_argument("--no-raw", action="store_true", help="do not write debug_raw.jsonl")
        if name != "step":
            s.add_argument("--max-actions", type=int)
            s.add_argument("--max-elapsed-s", type=float)
            s.add_argument("--max-no-progress-s", type=float)
        s.set_defaults(fn=fn)

    d = sub.add_parser("decide-frame", help="choose from a saved decision frame (configured model, billable; never executes)")
    d.add_argument("frame")
    d.add_argument("--scripted-reply", help="use this literal reply instead of a model (SYNTHETIC test double)")
    d.set_defaults(fn=cmd_decide_frame)

    r = sub.add_parser("replay", help="baseline traces: re-run policy; LLM traces: validate recorded decisions (no model call)")
    r.add_argument("path")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_replay)

    sub.add_parser("capabilities", help="print the screen capability table").set_defaults(fn=cmd_capabilities)

    h = sub.add_parser("sync-history", help="fill the local Spire Codex run corpus (network; no game or model calls)")
    h.add_argument("--character", required=True, help="e.g. IRONCLAD")
    h.add_argument("--ascension", type=int, required=True)
    h.add_argument("--max-new-runs", type=int, default=500)
    h.add_argument("--max-seconds", type=float, default=600.0)
    h.set_defaults(fn=cmd_sync_history)

    s = sub.add_parser("start-run", help="replace the current run and automatically set up a new run through Neow dialogue")
    for flag in ("config", "base-url", "model"):
        s.add_argument("--" + flag, default=argparse.SUPPRESS)
    s.add_argument("--character", type=character_id, help="character name or number (default: prompt)")
    s.add_argument("--ascension", type=ascension_level, help="0-10 (default: prompt)")
    s.add_argument("--setup-timeout-s", type=float, default=120.0)
    s.add_argument("--trace-dir")
    s.add_argument("--no-raw", action="store_true")
    s.add_argument("--max-actions", type=int)
    s.add_argument("--max-elapsed-s", type=float)
    s.add_argument("--max-no-progress-s", type=float)
    s.set_defaults(fn=cmd_start_run)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
