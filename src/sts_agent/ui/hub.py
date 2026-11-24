"""The interface's live state: what the session reports, and the pause/step/stop switch.

One ``UiHub`` lives as long as the interface. The session thread calls its hooks
(``SessionHooks``); HTTP threads read ``snapshot`` and call the control methods.
Everything is guarded by one condition variable, which also wakes waiting readers.
A hook that fails is reported on the console and otherwise ignored, so a display
problem cannot stop a run.
"""
from __future__ import annotations

import functools
import threading
import time
from collections import deque

from ..session.hooks import SessionHooks
from .combat_log import CombatLog

HEALTH = (("game", "Game window"), ("mod", "Game mod"), ("build", "Game build"),
          ("model", "Model"), ("autobattle", "Autobattle"), ("evidence", "Expert evidence"))
_LIVE_KEEP = 40000        # characters of the streaming reasoning kept in memory
_REASONING_KEEP = 12000   # characters of a finished call's reasoning kept as an entry


def _safe(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:   # never let the display break the session
            self._hook_failed(method.__name__, exc)
    return wrapper


def _clip(value, limit):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


class UiHub(SessionHooks):
    def __init__(self, *, echo=print):
        self._cv = threading.Condition()
        self._version = 0
        self._echo = echo
        self._log: deque = deque(maxlen=800)
        self._think: deque = deque(maxlen=160)
        self._ids = {"log": 0, "think": 0}
        self._console: deque = deque(maxlen=200)
        self._combat = CombatLog()
        self._live = {"id": 0, "purpose": None, "text": "", "dropped": 0, "answer_chars": 0, "active": False}
        now = time.time()
        self._session = {"state": "idle", "detail": "No session yet", "since": now, "stop_reason": None,
                         "trace_dir": None, "mode": None}
        self._step = {"phase": "idle", "detail": "", "since": now}
        self._model = {"status": "idle", "purpose": None, "attempt": None, "since": None, "error": None,
                       "screen": None, "revision": None, "retry_kind": None}
        self._calls: dict = {}
        self._run: dict = {}
        self._route = None
        self._route_rev = 0
        self._health = {key: {"state": "unknown", "detail": "not checked yet"} for key, _ in HEALTH}
        self._last_action = None
        self._failed_hooks: set = set()
        self.info: dict = {}            # static facts for the page: model name, config path, layout
        # control
        self._pause = False
        self._stop = False
        self._pass = False              # one decision may run although the session is paused
        self._tokens = 0

    # ------------------------------------------------------------------ internals
    def _bump(self):
        self._version += 1
        self._cv.notify_all()

    def _add(self, kind: str, entry: dict):
        self._ids[kind] += 1
        entry = {"id": self._ids[kind], "t": time.time(), **entry}
        (self._log if kind == "log" else self._think).append(entry)

    def _hook_failed(self, name, exc):
        if name not in self._failed_hooks:
            self._failed_hooks.add(name)
            self._echo(f"interface: display hook {name} failed ({type(exc).__name__}: {exc}); the run is unaffected")

    # ------------------------------------------------------------------ readers
    def wait(self, version: int, timeout: float) -> None:
        """Return when the state is newer than ``version`` or after ``timeout`` seconds."""
        with self._cv:
            if self._version <= version:
                self._cv.wait(timeout)

    def snapshot(self, *, log_after=0, think_after=0, live_id=0, live_len=0, route_rev=-1) -> dict:
        with self._cv:
            live = self._live
            if live_id != live["id"]:
                live_len = 0
            total = live["dropped"] + len(live["text"])
            reset = live_len < live["dropped"] or live_len > total
            start = 0 if reset else live_len - live["dropped"]
            session = dict(self._session)
            if session["state"] in ("running", "starting") and self._pause:
                session["state"] = "paused" if self._step["phase"] == "paused" else "pausing"
            out = {
                "version": self._version, "now": time.time(), "session": session,
                "control": {"paused": self._pause, "stopping": self._stop},
                "step": dict(self._step), "model": dict(self._model),
                "calls": {k: dict(v) for k, v in self._calls.items()},
                "run": dict(self._run), "last_action": self._last_action,
                "health": [{"key": key, "label": label, **self._health[key]} for key, label in HEALTH],
                "log": [e for e in self._log if e["id"] > log_after],
                "think": [e for e in self._think if e["id"] > think_after],
                "live": {"id": live["id"], "purpose": live["purpose"], "active": live["active"],
                         "reset": reset, "append": live["text"][start:], "len": total,
                         "answer_chars": live["answer_chars"]},
                "console": self._console[-1] if self._console else "",
                "route_rev": self._route_rev, "info": dict(self.info),
            }
            if route_rev != self._route_rev:
                out["route"] = self._route
            return out

    # ------------------------------------------------------------------ control
    def pause(self):
        with self._cv:
            self._pause, self._tokens = True, 0
            self._bump()

    def resume(self):
        with self._cv:
            self._pause, self._tokens = False, 0
            self._bump()

    def step_once(self):
        """While paused: let the agent finish one decision, then hold again."""
        with self._cv:
            if self._pause:
                self._tokens = 1
                self._bump()

    def request_stop(self):
        with self._cv:
            self._stop = True
            if self._session["state"] in ("running", "starting"):
                self._session.update(state="stopping", detail="Stopping after the step in progress",
                                     since=time.time())
            self._bump()

    def begin_session(self, mode: str, detail: str):
        with self._cv:
            self._pause = self._stop = self._pass = False
            self._tokens = 0
            self._session = {"state": "starting", "detail": detail, "since": time.time(), "stop_reason": None,
                             "trace_dir": None, "mode": mode}
            self._step = {"phase": "starting", "detail": detail, "since": time.time()}
            self._add("think", {"k": "note", "text": detail})
            self._bump()

    def set_session(self, state: str, detail: str, **extra):
        with self._cv:
            self._session.update(state=state, detail=detail, since=time.time(), **extra)
            if state in ("stopped", "error", "idle"):
                self._step = {"phase": state, "detail": detail, "since": time.time()}
                self._model.update(status="idle", since=None)
                self._live["active"] = False
            self._bump()

    def session_active(self) -> bool:
        with self._cv:
            return self._session["state"] in ("starting", "running", "stopping")

    def gate(self, point: str) -> float:
        with self._cv:
            if point == "step_start":
                self._pass = False
            if self._stop or not self._pause or self._pass:
                return 0.0
            started = time.monotonic()
            before = dict(self._step)
            self._step = {"phase": "paused", "detail": "Paused. The game is yours until you resume.",
                          "since": time.time(), "held": before.get("detail")}
            self._bump()
            while self._pause and not self._stop:
                if self._tokens > 0:
                    self._tokens -= 1
                    self._pass = True
                    break
                self._cv.wait(0.5)
            self._step = {**before, "since": time.time()}
            self._bump()
            return max(time.monotonic() - started, 1e-6)

    def pause_requested(self) -> bool:
        with self._cv:
            return self._pause and not self._pass and self._tokens == 0 and not self._stop

    def stop_requested(self) -> bool:
        with self._cv:
            return self._stop

    def set_info(self, **values):
        """Change facts the page shows (the model in use) while the interface is open."""
        with self._cv:
            self.info = {**self.info, **values}
            self._bump()

    # ------------------------------------------------------------------ health
    def set_health(self, key: str, state: str, detail: str):
        with self._cv:
            new = {"state": state, "detail": _clip(detail, 220)}
            if self._health.get(key) != new:
                self._health[key] = new
                self._bump()

    # ------------------------------------------------------------------ hooks
    @_safe
    def step(self, phase, detail="", **info):
        with self._cv:
            self._step = {"phase": phase, "detail": _clip(detail, 300), "since": time.time(), **info}
            if self._session["state"] == "starting":
                self._session.update(state="running", detail="Session running", since=time.time())
            self._bump()

    @_safe
    def observed(self, view):
        facts = view.facts if isinstance(view.facts, dict) else {}
        run, player = facts.get("run") or {}, facts.get("player") or {}
        with self._cv:
            new = dict(self._run)
            new.update(screen=view.kind, in_combat=bool(view.in_combat), round=view.combat_round)
            for key in ("character", "ascension", "act", "floor"):
                if run.get(key) is not None:
                    new[key] = run[key]
            for key in ("hp", "max_hp", "gold", "block", "energy"):
                if key in player:
                    new[key] = player[key]
            entries = self._combat.update(view)
            for entry in entries:
                self._add("log", entry)
            if entries or new != self._run:
                self._run = new
                self._bump()

    @_safe
    def model_event(self, phase, data):
        purpose, now = data.get("purpose"), time.time()
        with self._cv:
            if phase == "call_started":
                self._model = {"status": "calling", "purpose": purpose, "attempt": data.get("attempt"),
                               "since": now, "error": None, "screen": data.get("screen"),
                               "revision": data.get("revision"), "retry_kind": data.get("retry_kind")}
                self._live = {"id": self._live["id"] + 1, "purpose": purpose, "text": "", "dropped": 0,
                              "answer_chars": 0, "active": True}
            elif phase == "call_finished":
                stopped = data.get("status") == "interrupted"   # ended by a requested stop: not a model failure
                ok = stopped or data.get("status") == "response received"
                elapsed = float(data.get("elapsed_s") or 0)
                row = self._calls.setdefault(purpose, {"calls": 0, "total_s": 0.0, "last_s": None, "failed": 0})
                row["calls"] += 1
                row["total_s"] = round(row["total_s"] + elapsed, 3)
                row["last_s"] = round(elapsed, 1)
                row["failed"] += 0 if ok else 1
                self._model.update(status="idle" if ok else "failed", since=None, last_s=round(elapsed, 1),
                                   error=None if ok else data.get("error"))
                live = self._live
                if live["text"].strip():
                    self._add("think", {"k": "reasoning", "purpose": purpose, "seconds": round(elapsed, 1),
                                        "text": live["text"][-_REASONING_KEEP:],
                                        "clipped": live["dropped"] > 0 or len(live["text"]) > _REASONING_KEEP})
                live["active"] = False
                if not stopped:
                    self._health["model"] = ({"state": "ok", "detail": f"last {purpose} call answered in {elapsed:.1f}s"}
                                             if ok else
                                             {"state": "warn", "detail": f"last {purpose} call failed "
                                                                         f"({data.get('error') or 'unknown'})"})
            elif phase in ("retry_scheduled", "reply_rejected"):
                self._model.update(status="retrying", error=_clip(data.get("error"), 160),
                                   retry_kind=data.get("retry_kind") or self._model.get("retry_kind"))
            else:
                return   # call_waiting and the rest: the page counts elapsed time itself
            self._bump()

    @_safe
    def thinking(self, kind, delta):
        with self._cv:
            live = self._live
            if not live["active"]:
                return
            if kind == "answer":
                live["answer_chars"] += len(delta)
            else:
                live["text"] += delta
                extra = len(live["text"]) - _LIVE_KEEP
                if extra > 0:
                    live["text"], live["dropped"] = live["text"][extra:], live["dropped"] + extra
            self._bump()

    @_safe
    def decision(self, seq, screen, description, reason, turn_intent="", source="llm"):
        with self._cv:
            self._last_action = time.time()
            self._add("think", {"k": "decision", "seq": seq, "screen": screen, "action": _clip(description, 300),
                                "reason": _clip(reason, 700), "intent": _clip(turn_intent, 400), "source": source})
            self._bump()

    @_safe
    def strategy(self, result, reasons):
        assessment = getattr(result, "assessment", None)
        content = getattr(assessment, "content", None) or {}
        entry = {"k": "strategy", "status": getattr(result, "status", None),
                 "revision": getattr(assessment, "input_revision", None), "reasons": list(reasons)[:8],
                 "called": bool(getattr(result, "model_called", False)),
                 "diagnostic": _clip(getattr(result, "diagnostic", None), 300)}
        if entry["status"] == "updated" and isinstance(content, dict):
            entry.update(summary=_clip(content.get("summary"), 700), change=_clip(content.get("change_note"), 300),
                         priorities=[_clip(p.get("need"), 200) for p in content.get("priorities") or []
                                     if isinstance(p, dict)][:3],
                         plan=content.get("standing_plan") if isinstance(content.get("standing_plan"), dict) else None,
                         uncertainties=[_clip(u, 200) for u in content.get("uncertainties") or []][:5])
        with self._cv:
            if entry["called"] or entry["status"] != "unchanged":
                self._add("think", entry)
                self._bump()

    @_safe
    def route(self, plan, map_memory):
        memory = map_memory or {}
        plan = plan or {}
        route = {
            "act": memory.get("act"), "floor": memory.get("floor"),
            "nodes": [{"col": n.get("col"), "row": n.get("row"), "type": n.get("type"),
                       "children": [list(c) for c in n.get("children") or [] if len(c) == 2]}
                      for n in memory.get("nodes") or []],
            "bosses": [{"name": b.get("name") or b.get("id"), "col": b.get("col"), "row": b.get("row")}
                       for b in memory.get("bosses") or []],
            "visited": [[p.get("col"), p.get("row")] for p in memory.get("visited") or [] if isinstance(p, dict)],
            "current": memory.get("current"), "chosen": memory.get("chosen"),
            "plan": None if not plan else {
                "text": _clip(plan.get("plan"), 400), "change_if": _clip(plan.get("change_course_if"), 400),
                "act": plan.get("act"), "set_at_floor": plan.get("floor"), "start": plan.get("start"),
                "coords": [list(c) for c in plan.get("coords") or []],
                "trajectory": [{"type": s.get("type"), "hp_pct": s.get("expected_hp_pct"),
                                "gold": s.get("estimated_gold")} for s in plan.get("trajectory") or []],
                "hp_pct": plan.get("hp_pct")},
        }
        with self._cv:
            if route != self._route:
                self._route, self._route_rev = route, self._route_rev + 1
                self._bump()

    @_safe
    def evidence(self, screen, support):
        if not isinstance(support, dict):
            return
        status = support.get("status")
        state = {"available": "ok", "partial": "warn", "unavailable": "down"}.get(status, "ok")
        detail = f"{screen} screen: {status or 'attached'}"
        if support.get("reason"):
            detail += f" ({support['reason']})"
        self.set_health("evidence", state, detail)

    @_safe
    def combat_finished(self, item, outcome):
        with self._cv:
            for entry in self._combat.finish(item, outcome):
                self._add("log", entry)
            self._bump()

    @_safe
    def autobattle(self, status):
        fighting = bool(status.get("combat_in_progress"))
        playing = bool(status.get("autobattle") and status.get("autoplay"))
        self.set_health("autobattle", "ok", "playing this fight" if fighting and playing else
                        "fight open, Autobattle idle" if fighting else "ready, off between fights")

    @_safe
    def log(self, message):
        text = str(message)
        with self._cv:
            self._console.append(_clip(text.splitlines()[0] if text else "", 240))
            self._bump()
        self._echo(text)

    @_safe
    def finished(self, summary, trace_dir):
        reason = summary.get("stop_reason")
        detail = _clip(summary.get("detail"), 300)
        with self._cv:
            self._add("think", {"k": "note", "text": f"Session ended: {reason}" + (f" — {detail}" if detail else "")})
        self.set_session("stopped", f"{reason}" + (f": {detail}" if detail else ""),
                         stop_reason=reason, trace_dir=trace_dir)
