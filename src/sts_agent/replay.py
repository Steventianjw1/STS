"""Replay decisions: re-run the policy on recorded inputs, offline.

This reproduces *policy choices* from saved observations. It does not simulate
the game and cannot restore a live game state.
"""
from __future__ import annotations

import json
from pathlib import Path

from .actions import build_candidates
from .observation import fingerprint


def iter_records(path: Path):
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield n, json.loads(line)
            except json.JSONDecodeError as e:
                yield n, {"type": "_corrupt", "error": str(e)}


def resolve_decisions_file(path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir():
        p = p / "decisions.jsonl"
    return p


def replay_trace(path: str | Path, policy) -> dict:
    p = resolve_decisions_file(path)
    records = list(iter_records(p))
    corrupt = sum(1 for _, r in records if r.get("type") == "_corrupt")
    # Candidates rejected earlier for the same state are filtered out before the
    # policy sees them; the runner logs them as a ``blocked_candidates`` event.
    blocked_by_seq = {r["data"]["seq"]: set(r["data"]["action_ids"]) for _, r in records
                      if r.get("type") == "event" and r.get("event") == "blocked_candidates"}
    llm = [(n, r) for n, r in records if r.get("type") == "llm_decision"]
    if llm:
        results = [_validate_llm_decision(r, n) for n, r in llm]
        matches = sum(1 for r in results if r["match"])
        return {"file": str(p), "mode": "recorded_decision_validation (no model call; the LLM is not re-sampled)",
                "decisions": len(results), "matches": matches,
                "mismatches": [r for r in results if not r["match"]], "corrupt_lines": corrupt, "results": results}
    results = [_replay_one(r, policy, blocked_by_seq.get(r["seq"], set()), n)
               for n, r in records if r.get("type") == "decision"]
    matches = sum(1 for r in results if r["match"])
    return {
        "file": str(p),
        "decisions": len(results),
        "matches": matches,
        "mismatches": [r for r in results if not r["match"]],
        "corrupt_lines": corrupt,
        "results": results,
    }


def _replay_one(rec: dict, policy, blocked: set, line_no: int) -> dict:
    obs = rec["input"]["observation"]
    recorded_cands = rec["input"]["candidates"]
    recorded = rec["decision"]
    problems = []

    if fingerprint(obs) != obs.get("fingerprint"):
        problems.append("observation fingerprint does not match its content")

    rebuilt, _ = build_candidates(obs)
    rebuilt_ids = [c["action_id"] for c in rebuilt if c["action_id"] not in blocked]
    recorded_ids = [c["action_id"] for c in recorded_cands]
    builder_match = rebuilt_ids == recorded_ids
    if not builder_match:
        problems.append(f"candidate builder differs: rebuilt={rebuilt_ids} recorded={recorded_ids}")

    try:
        d = policy.decide(obs, recorded_cands)
    except Exception as e:
        problems.append(f"policy raised {type(e).__name__}: {e}")
        d = None
    same = d is not None and d["action_id"] == recorded["action_id"] and d["rule"] == recorded["rule"]
    if d is not None and not same:
        problems.append(f"policy chose {d['action_id']} ({d['rule']}) but trace has {recorded['action_id']} ({recorded['rule']})")
    version_note = None
    if d is not None and recorded["policy"] != d["policy"]:
        version_note = f"recorded with {recorded['policy']}, replayed with {d['policy']}"
    return {
        "seq": rec["seq"],
        "line": line_no,
        "recorded_action": recorded["action_id"],
        "replayed_action": d["action_id"] if d else None,
        "rule": d["rule"] if d else None,
        "reason": d["reason"] if d else None,
        "builder_match": builder_match,
        "match": same and builder_match and not problems,
        "problems": problems,
        "policy_version_note": version_note,
    }


def _validate_llm_decision(rec: dict, line_no: int) -> dict:
    """Check a recorded LLM decision without calling any model:
    frame_id integrity, recorded choice offered, recorded response text parses to that choice."""
    from .decision.models import ChoiceError, DecisionFrame, parse_choice
    problems, chosen = [], (rec.get("choice") or {}).get("action_id")
    try:
        frame = DecisionFrame.from_record(rec["frame"])
    except (KeyError, ValueError, TypeError) as e:
        frame = None
        problems.append(f"frame invalid: {e}")
    if frame is not None and chosen is not None:
        if frame.candidate(chosen) is None:
            problems.append(f"recorded action {chosen!r} was not offered in the frame")
        last = (rec.get("attempts") or [{}])[-1].get("response_text")
        try:
            c = parse_choice(last, frame)
            if c.action_id != chosen:
                problems.append(f"recorded response selects {c.action_id!r}, trace says {chosen!r}")
        except ChoiceError as e:
            problems.append(f"recorded response does not validate: {e}")
    if chosen is None and not rec.get("error_kind"):
        problems.append("no choice and no error recorded")
    return {"seq": rec.get("seq"), "line": line_no, "recorded_action": chosen, "replayed_action": chosen,
            "rule": "llm", "reason": None, "builder_match": True, "match": not problems, "problems": problems,
            "policy_version_note": f"provenance: {rec.get('provenance')}; model: {(rec.get('model') or {}).get('model')}"}


def decide_from_observation_file(path: str | Path, policy) -> dict:
    """Run the policy on a single observation saved by ``observe --save``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    obs = data.get("observation", data)
    cands, excluded = build_candidates(obs)
    if not cands:
        return {"observation_file": str(path), "ready": False, "phase": obs.get("phase"),
                "note": "observation is not ready for an action; no candidates"}
    d = policy.decide(obs, cands)
    return {"observation_file": str(path), "ready": True, "candidates": [c["action_id"] for c in cands],
            "excluded": excluded, "decision": {k: d[k] for k in ("action_id", "rule", "reason", "scores", "policy")}}
