"""Offline test doubles: a scripted game client and a scripted text model.

Both are SYNTHETIC. Scripted model replies are canned text, not LLM reasoning;
a scripted game is a list of canned states, not a simulator. They are never
used unless explicitly injected.
"""
from __future__ import annotations

import copy
import json
from typing import Callable

from ..client import ActionResponse


class ScriptedTextModel:
    """Returns queued replies (or raises queued exceptions). ``reply`` may also be a
    callable taking (system, user) and returning text - e.g. to echo the frame_id."""

    provider = "scripted"
    is_scripted = True

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.last_call = None

    def identity(self) -> dict:
        return {"provider": "scripted", "model": "scripted-replies", "scripted": True,
                "note": "SYNTHETIC test double; not an LLM"}

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if not self.replies:
            raise RuntimeError("scripted model has no replies left")
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r(system, user) if callable(r) else r


def pick(action_id: str, reason: str = "Scripted choice.", intent: str = "") -> Callable[[str, str], str]:
    """Scripted reply that echoes the frame_id from the user message and picks ``action_id``."""
    def reply(system, user):
        msg = json.loads(user)
        if "frame_id" not in msg:  # strategic planner call, not a decision
            raise RuntimeError("pick() used for a non-decision call")
        return json.dumps({"frame_id": msg["frame_id"], "action_id": action_id, "reason": reason,
                           "turn_intent": intent})
    return reply


class ScriptedGameClient:
    """Canned game: ``get_state`` returns the head of a queue (the last state repeats).
    ``on_post(payload) -> (ActionResponse | Exception, next_states | None)``."""

    data_source = "synthetic"

    def __init__(self, states: list[dict], on_post: Callable | None = None, run_id: str | None = None):
        self.queue = list(states)
        self.on_post = on_post or (lambda p: (ActionResponse(True, 200, {"status": "ok"}, 0.0), None))
        self.posts: list[dict] = []
        self.run_id = run_id

    def hello(self):
        return {"message": "Hello from SCRIPTED (synthetic) game client", "status": "ok"}

    def get_optional(self, path):
        if path.endswith("/compendium") and self.run_id:
            return {"current_run": {"run_id": self.run_id}}
        return None

    def get_state(self) -> dict:
        st = self.queue[0] if len(self.queue) == 1 else self.queue.pop(0)
        return copy.deepcopy(st)

    def post_action(self, payload: dict) -> ActionResponse:
        self.posts.append(copy.deepcopy(payload))
        result, nxt = self.on_post(payload)
        if nxt is not None:
            self.queue = list(nxt)
        if isinstance(result, BaseException):
            raise result
        return result
