"""Combat search through the Spire Mentor mod's advisor (tfriedel/spire-mentor, MIT).

Spire Mentor runs beside STS2MCP in its Advisor ("coach") mode. Its advisor
process searches every combat board with MCTS over a Rust re-implementation of
the game and answers the mod. This module only *reads* that advisor:

* ``GET <control_url>/status``  -> ``{connected, log_path, ...}``
* ``GET <control_url>/state``   -> the latest ``advice_request`` (the board the
  advisor was asked about, with ``state_id`` / ``request_id``)
* the advisor's raw JSONL log (``advisor-*.jsonl``), whose ``mcts`` records hold
  the ``advice`` it sent for each ``state_id`` / ``request_id``

In per-action search mode, nothing is sent to Spire Mentor and it never executes anything for us: the
recommended action is mapped onto one of this agent's own STS2MCP candidates
and executed by the normal validated path. A recommendation is used only when
Spire Mentor's board equals the board this agent observed; anything else is a
miss with a reason, and the caller falls back to the LLM.

MentorAutobattle instead controls native Autobattle through the game-thread
bridge. The coordinator yields whole fights and never reads non-combat advice.

Field names follow ``spire-mentor-mod/PROTOCOL.md`` and
``sts2-pycli/python/realgame_{advisor,control}.py`` as read at source. Not
validated against a live game.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .client import GameClientError

SOURCE = "spire_mentor_search"
_TAIL_BYTES = 4 * 1024 * 1024      # how far back to read a log that already exists
_MAX_ADVICE = 128
_TIMEOUT_STRIKES = 3               # consecutive timeouts before the cooldown starts


class MentorAutobattle:
    """Control native Autobattle through STS2MCP's game-thread bridge. No advice reads."""

    def __init__(self, client):
        self.client = client

    def control(self, command: str) -> dict:
        """``hold_offers`` makes Mentor hold in-fight choose-a-card screens (its ``pause_on``);
        ``ai_pick`` has Mentor play its own recommendation on the screen it is holding, once."""
        if command not in ("status", "on", "off", "hold_offers", "ai_pick"):
            raise ValueError("Unknown Autobattle command")
        response = self.client.post_action({"action": "menu_select", "option": f"agent_autobattle_{command}"})
        body = response.body
        if not response.accepted or not isinstance(body, dict) or body.get("autobattle_version") != 1:
            raise GameClientError(f"Spire Mentor Autobattle unavailable: {response.error or body}. "
                                  "Rebuild/install the agent mod extension and enable Spire Mentor.")
        return body


@dataclass
class Recommendation:
    action_id: str | None = None
    miss: str | None = None            # why no action was produced
    label: str | None = None
    advice: dict = field(default_factory=dict)   # trimmed copy of the advisor's advice
    state_id: object = None
    request_id: object = None
    waited_s: float = 0.0

    def record(self) -> dict:
        return {"source": SOURCE, "action_id": self.action_id, "miss": self.miss, "label": self.label,
                "state_id": self.state_id, "request_id": self.request_id, "waited_s": round(self.waited_s, 3),
                "advice": self.advice}


def _norm(v) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _alive(e: dict) -> bool:
    return e.get("is_alive") is not False and (e.get("hp") is None or e["hp"] > 0)


def board_mismatch(facts: dict, state: dict) -> str | None:
    """Why Spire Mentor's board is not the board we observed (None = same).

    Only values both sides report are compared; an unknown (None) never matches
    or mismatches by itself.
    """
    if (state.get("advice_for") or state.get("type")) != "combat_action":
        return f"advisor is on {state.get('advice_for') or state.get('type')!r}, not a combat board"
    ours, theirs = facts.get("player") or {}, state.get("player") or {}
    for k in ("hp", "block", "energy"):
        if ours.get(k) is not None and theirs.get(k) is not None and ours[k] != theirs[k]:
            return f"player {k} differs (ours {ours[k]}, advisor {theirs[k]})"
    our_hand = sorted(_norm(c.get("id")) for c in facts.get("hand") or [])
    their_hand = sorted(_norm(c.get("id")) for c in state.get("hand") or [])
    if our_hand != their_hand:
        return f"hand differs (ours {len(our_hand)} cards, advisor {len(their_hand)})"
    our_en = {e["combat_id"]: e for e in facts.get("enemies") or [] if e.get("combat_id") is not None and _alive(e)}
    their_en = {e["combat_id"]: e for e in state.get("enemies") or [] if e.get("combat_id") is not None and _alive(e)}
    if set(our_en) != set(their_en):
        return f"living enemies differ (ours {sorted(our_en)}, advisor {sorted(their_en)})"
    for cid, e in our_en.items():
        for k in ("hp", "block"):
            if e.get(k) is not None and their_en[cid].get(k) is not None and e[k] != their_en[cid][k]:
                return f"enemy #{cid} {k} differs (ours {e[k]}, advisor {their_en[cid][k]})"
    return None


def _our_card(facts: dict, state: dict, card_index) -> tuple[dict | None, str]:
    their_hand = state.get("hand") or []
    if not isinstance(card_index, int) or not 0 <= card_index < len(their_hand):
        return None, f"card_index {card_index!r} is outside the advisor's hand"
    theirs = their_hand[card_index]
    hand = facts.get("hand") or []
    same_slot = next((c for c in hand if c.get("index") == card_index), None)
    if same_slot is not None and _norm(same_slot.get("id")) == _norm(theirs.get("id")):
        return same_slot, ""
    # The two mods order the hand differently: take an interchangeable copy
    # (same card, same upgrade state, same cost) that the game says is playable.
    up = bool(theirs.get("upgrade_level") or theirs.get("upgraded"))
    for c in hand:
        if (_norm(c.get("id")) == _norm(theirs.get("id")) and c.get("can_play") is True
                and c.get("is_upgraded") in (None, up)
                and (c.get("cost") is None or theirs.get("cost") is None or c["cost"] == theirs["cost"])):
            return c, ""
    return None, f"no playable copy of {theirs.get('id')!r} in our hand"


def _target_cid(state: dict, target_index) -> tuple[int | None, str]:
    if target_index is None:
        return None, ""
    enemies = state.get("enemies") or []
    if not isinstance(target_index, int) or not 0 <= target_index < len(enemies):
        return None, f"target_index {target_index!r} is outside the advisor's enemy list"
    cid = enemies[target_index].get("combat_id")
    return (cid, "") if isinstance(cid, int) else (None, "advisor enemy has no combat_id")


def _pick(ids: set, base: str, cid) -> str | None:
    """The offered action id for ``base`` with an optional target."""
    if cid is not None and f"{base}_t{cid}" in ids:
        return f"{base}_t{cid}"
    if base in ids:                      # untargeted on our side (target ignored by the game)
        return base
    if cid is None:                      # advisor omitted the target: accept only an unambiguous one
        targeted = [i for i in ids if i.startswith(base + "_t")]
        if len(targeted) == 1:
            return targeted[0]
    return None


def map_best(facts: dict, candidates: list, state: dict, best: dict) -> tuple[str | None, str]:
    """Map the advisor's executable ``best`` to one of our candidate action ids."""
    ids = {c["action_id"] for c in candidates}
    action = best.get("action")
    if best.get("ally_index") is not None:
        return None, "recommendation targets a co-op ally"
    if action == "end_turn":
        return ("end_turn", "") if "end_turn" in ids else (None, "end_turn is not offered")
    cid, err = _target_cid(state, best.get("target_index"))
    if err:
        return None, err
    if action == "play":
        card, err = _our_card(facts, state, best.get("card_index"))
        if card is None:
            return None, err
        aid = _pick(ids, f"play_{card['index']}", cid)
        return (aid, "") if aid else (None, f"play of hand card {card['index']} ({card.get('name')}) is not offered")
    if action == "potion":
        theirs = next((p for p in state.get("potions") or [] if p.get("slot") == best.get("slot")), None)
        if theirs is None or theirs.get("empty"):
            return None, f"advisor potion slot {best.get('slot')!r} is empty"
        ours = [p for p in facts.get("potions") or [] if _norm(p.get("id")) == _norm(theirs.get("id"))]
        ours.sort(key=lambda p: p.get("slot") != theirs.get("slot"))   # same slot first
        for p in ours:
            aid = _pick(ids, f"potion_{p.get('slot')}", cid)
            if aid:
                return aid, ""
        return None, f"potion {theirs.get('id')!r} is not offered"
    return None, f"recommendation {action!r} has no mapping to our actions"


def _trim_advice(advice: dict) -> dict:
    keep = {k: advice.get(k) for k in ("note", "p_win", "p_win_raw", "low_confidence", "best", "pick", "pv",
                                       "hand_back", "unsupported_game_version") if k in advice}
    keep["options"] = [{k: o.get(k) for k in ("label", "q", "p_win", "visits", "visit_share", "is_best", "in_band")}
                       for o in (advice.get("options") or [])[:8]]
    return keep


class SpireMentorAdvisor:
    """Read-only client of a running Spire Mentor advisor."""

    def __init__(self, control_url: str = "http://127.0.0.1:8079", *, advisor_log_dir: str | None = None,
                 advice_timeout_s: float = 30.0, poll_interval_s: float = 0.2, request_timeout_s: float = 2.0,
                 cooldown_s: float = 120.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.control_url = control_url.rstrip("/")
        self.advisor_log_dir = advisor_log_dir
        self.advice_timeout_s, self.poll_interval_s = advice_timeout_s, poll_interval_s
        self.request_timeout_s, self.cooldown_s = request_timeout_s, cooldown_s
        self.clock, self.sleep = clock, sleep
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._log_path: Path | None = None
        self._offset = 0
        self._partial = b""
        self._advice: OrderedDict = OrderedDict()     # (state_id, request_id) -> advice
        self._timeouts = 0
        self._cooldown_until = 0.0

    # ------------------------------------------------------------- transport
    def _get(self, path: str):
        """(status, json | None). Raises OSError when the advisor is unreachable."""
        try:
            with self._opener.open(self.control_url + path, timeout=self.request_timeout_s) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except ValueError:
            return 200, None

    def status(self) -> dict | None:
        try:
            code, body = self._get("/status")
        except OSError:
            return None
        return body if code == 200 and isinstance(body, dict) else None

    # ------------------------------------------------------------------ log
    def _log_dirs(self) -> list[Path]:
        if self.advisor_log_dir:
            return [Path(self.advisor_log_dir)]
        dirs = []
        wlog = (self.status() or {}).get("log_path")
        if wlog:
            parent = Path(wlog).parent
            dirs += [parent / "realgame_advisor", parent]
        if os.environ.get("LOCALAPPDATA"):   # the packaged advisor's default
            dirs.append(Path(os.environ["LOCALAPPDATA"]) / "SpireMentor" / "logs" / "realgame_advisor")
        return dirs

    def find_log(self) -> Path | None:
        newest, newest_m = None, -1.0
        for d in self._log_dirs():
            try:
                for f in d.glob("advisor-*.jsonl"):
                    m = f.stat().st_mtime
                    if m > newest_m:
                        newest, newest_m = f, m
            except OSError:
                continue
        return newest

    def _tail(self) -> None:
        path = self.find_log() if self._log_path is None or not self._advice else self._log_path
        if path is None:
            return
        if path != self._log_path:
            self._log_path, self._partial = path, b""
            try:
                self._offset = max(0, path.stat().st_size - _TAIL_BYTES)
            except OSError:
                self._offset = 0
            skip_first = self._offset > 0
        else:
            skip_first = False
        try:
            with open(path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError:
            return
        self._offset += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()
        for line in lines[1:] if skip_first else lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            d = rec.get("data") if isinstance(rec, dict) else None
            if rec.get("type") == "mcts" and isinstance(d, dict) and isinstance(d.get("advice"), dict):
                self._advice[(d.get("state_id"), d.get("request_id"))] = d["advice"]
                while len(self._advice) > _MAX_ADVICE:
                    self._advice.popitem(last=False)

    def _advice_for(self, key):
        self._tail()
        if key not in self._advice:
            # The advisor may have restarted and opened a new log file.
            newest = self.find_log()
            if newest is not None and newest != self._log_path:
                self._log_path = None
                self._tail()
        return self._advice.get(key)

    # ------------------------------------------------------------ recommend
    def recommend(self, facts: dict, candidates: list) -> Recommendation:
        start = self.clock()
        if start < self._cooldown_until:
            return Recommendation(miss="search paused after repeated timeouts")
        rec = Recommendation()
        reason = "no advisor state"
        while True:
            try:
                code, state = self._get("/state")
            except OSError as e:
                rec.miss = f"advisor unreachable at {self.control_url}: {e}"[:300]
                break
            if code == 200 and isinstance(state, dict):
                reason = board_mismatch(facts, state)
                if reason is None:
                    rec.state_id, rec.request_id = state.get("state_id"), state.get("request_id")
                    advice = self._advice_for((rec.state_id, rec.request_id))
                    if advice is not None:
                        self._resolve(rec, facts, candidates, state, advice)
                        self._timeouts = 0
                        break
                    reason = ("advice not logged yet" if self._log_path is not None else
                              "advisor log not found (set [combat_search].advisor_log_dir)")
            elif code == 404:
                reason = "advisor has no decision from the game yet (is Spire Mentor's coach on?)"
            else:
                reason = f"advisor /state returned HTTP {code}"
            if self.clock() - start >= self.advice_timeout_s:
                rec.miss = f"timed out after {self.advice_timeout_s:g}s: {reason}"
                self._timeouts += 1
                if self._timeouts >= _TIMEOUT_STRIKES:
                    self._timeouts, self._cooldown_until = 0, self.clock() + self.cooldown_s
                break
            self.sleep(self.poll_interval_s)
        rec.waited_s = self.clock() - start
        return rec

    def _resolve(self, rec: Recommendation, facts, candidates, state, advice) -> None:
        rec.advice = _trim_advice(advice)
        best = advice.get("best")
        if advice.get("hand_back"):
            rec.miss = f"advisor handed the decision back: {advice['hand_back']}"
        elif advice.get("unsupported_game_version"):
            rec.miss = f"advisor does not support game build {advice['unsupported_game_version']}"
        elif not isinstance(best, dict):
            rec.miss = f"advisor gave no executable move ({advice.get('note')})"
        else:
            rec.label = best.get("label")
            rec.action_id, err = map_best(facts, candidates, state, best)
            rec.miss = err or None
