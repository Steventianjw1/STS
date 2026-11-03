"""Trace recorder: append-only JSONL plus a session manifest.

Layout of one session (``<trace_dir>/<session_id>/``)::

    manifest.json     session metadata; rewritten atomically at start and end
    decisions.jsonl   one JSON record per line (see docs/TRACE_FORMAT.md)
    debug_raw.jsonl   optional raw API responses; NOT training data

Each decision produces two records with the same ``seq``:

* ``decision`` - written *before* the action is sent; contains only what the
  policy saw (observation + candidates) and what it chose.
* ``outcome``  - written after resolution; execution response, timing, the
  post-action observation and the verdict.

Keeping them separate means a training loader can take ``decision`` records
alone and never see the future. Every line is flushed and fsynced.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from . import AGENT_VERSION, STS2MCP_SOURCE_COMMIT
from .observation import OBS_SCHEMA_VERSION

TRACE_FORMAT_VERSION = "sts2-trace-1"        # baseline (legacy) traces
LLM_TRACE_FORMAT_VERSION = "sts2-trace-2"    # LLM decision traces
PROVENANCE = {
    "data_source": "agent_generated",
    "behavior": "heuristic_baseline_policy",
    "is_expert_demonstration": False,
    "note": "Decisions made by an automated heuristic agent. Not human or expert play.",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_session_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


class TraceRecorder:
    def __init__(self, trace_dir: str | Path, *, command: str, policy_name: str, policy_version: str,
                 mod_version: str | None, game_build: str | None, run_id: str | None,
                 config_snapshot: dict, record_raw: bool = False, session_id: str | None = None,
                 trace_format: str = TRACE_FORMAT_VERSION, provenance: dict | None = None,
                 extra_manifest: dict | None = None):
        self.session_id = session_id or new_session_id()
        self.dir = Path(trace_dir) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.decisions_path = self.dir / "decisions.jsonl"
        self.raw_path = self.dir / "debug_raw.jsonl" if record_raw else None
        self._fh = open(self.decisions_path, "a", encoding="utf-8")
        self._raw_fh = open(self.raw_path, "a", encoding="utf-8") if self.raw_path else None
        self.seq = 0
        self.manifest = {
            "trace_format": trace_format,
            "session_id": self.session_id,
            "run_id": run_id,
            "command": command,
            "started_at": utc_now(),
            "ended_at": None,
            "agent_version": AGENT_VERSION,
            "observation_schema": OBS_SCHEMA_VERSION,
            "policy": {"name": policy_name, "version": policy_version},
            "game_build": game_build,   # user-declared; the API does not report it
            "mod_version": mod_version,  # from GET / hello message
            "mod_source_commit_adapter_written_against": STS2MCP_SOURCE_COMMIT,
            "provenance": provenance or PROVENANCE,
            "config": config_snapshot,
            "files": {"decisions": "decisions.jsonl", "debug_raw": "debug_raw.jsonl" if record_raw else None},
            "outcome": None,
            "counts": {"decisions": 0, "outcomes": 0, "errors": 0},
        }
        if extra_manifest:
            self.manifest.update(extra_manifest)
        self._write_manifest()
        self.event("session_start", {})

    # -- writing -------------------------------------------------------------
    def _append(self, fh, record: dict) -> None:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:  # pragma: no cover
            pass

    def _write_manifest(self) -> None:
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / "manifest.json")

    def _base(self, rtype: str) -> dict:
        return {"type": rtype, "session_id": self.session_id, "ts": utc_now(), "mono": round(time.monotonic(), 4)}

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def decision(self, seq: int, observation: dict, candidates: list[dict], excluded: list[dict], decision: dict) -> None:
        rec = self._base("decision")
        rec.update({
            "seq": seq,
            "provenance": "agent_generated_heuristic",
            "input": {"observation": observation, "candidates": candidates},
            "excluded_cards": excluded,
            "decision": {
                "action_id": decision["action_id"],
                "payload": decision["action"]["payload"],
                "policy": decision["policy"],
                "rule": decision["rule"],
                "reason": decision["reason"],
                "scores": decision["scores"],
            },
        })
        self._append(self._fh, rec)
        self.manifest["counts"]["decisions"] += 1

    def record(self, rtype: str, **fields) -> None:
        """Generic record (used by LLM traces: ``llm_decision``, ``strategic_update``)."""
        rec = self._base(rtype)
        rec.update(fields)
        self._append(self._fh, rec)
        if rtype == "llm_decision":
            self.manifest["counts"]["decisions"] += 1

    def update_manifest(self, **fields) -> None:
        self.manifest.update(fields)
        self._write_manifest()

    def outcome(self, seq: int, **fields) -> None:
        rec = self._base("outcome")
        rec["seq"] = seq
        rec.update(fields)
        self._append(self._fh, rec)
        self.manifest["counts"]["outcomes"] += 1

    def event(self, name: str, data: dict) -> None:
        rec = self._base("event")
        rec.update({"event": name, "data": data})
        self._append(self._fh, rec)

    def error(self, message: str, data: dict | None = None) -> None:
        rec = self._base("error")
        rec.update({"message": message, "data": data or {}})
        self._append(self._fh, rec)
        self.manifest["counts"]["errors"] += 1

    def raw(self, label: str, body: object) -> None:
        if self._raw_fh is None:
            return
        rec = self._base("raw")
        rec.update({"label": label, "not_for_training": True, "body": body})
        self._append(self._raw_fh, rec)

    def close(self, outcome: dict) -> None:
        rec = self._base("session_end")
        rec["outcome"] = outcome
        self._append(self._fh, rec)
        self.manifest["ended_at"] = utc_now()
        self.manifest["outcome"] = outcome
        self._write_manifest()
        self._fh.close()
        if self._raw_fh:
            self._raw_fh.close()
