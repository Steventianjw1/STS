"""Enemy move-pattern reference: current pattern position and next-turn predictions.

Reads a local copy of spire-mentor's extracted ``monsters.json`` (static data for one
game build; fetch it with ``tools/fetch_monster_data.py``). The observed next move
stays authoritative. Offsets 2-3 are derived from the data's transition table, starting
at the enemy's current move, and are labeled as predictions. Never game actions.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

DATA_VERSION = "v0.107.1"
DATA_SOURCE = "tfriedel/spire-mentor sts2-wiki/data/v0.107.1/monsters.json"
DEFAULT_PATH = "data/monster_reference/monsters.json"
FORECAST_TURNS = 3
_MIN_PROB = 1e-9


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _kind(itype) -> str:
    k = _norm(itype)
    return "attack" if k in ("attack", "multiattack", "singleattack", "singleattackintent") else k


class MonsterReference:
    def __init__(self, records: list, *, path: str, game_build: str | None = None):
        self.path, self.game_build = path, game_build
        self.by_id, self.by_title = {}, {}
        for rec in records:
            if not isinstance(rec, dict) or not isinstance(rec.get("moves"), list):
                continue
            for key in (rec.get("loc_key"), rec.get("class_name")):
                if key:
                    self.by_id.setdefault(_norm(key), rec)
            if rec.get("title"):
                self.by_title.setdefault(_norm(rec["title"]), rec)

    @classmethod
    def load(cls, path: str | None, *, game_build: str | None = None) -> "MonsterReference | None":
        if not path or not Path(path).is_file():
            return None
        try:
            records = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(records, path=path, game_build=game_build) if isinstance(records, list) else None

    # -- lookup ---------------------------------------------------------------
    def _record(self, enemy: dict):
        if enemy.get("monster_id") and _norm(enemy["monster_id"]) in self.by_id:
            return self.by_id[_norm(enemy["monster_id"])], "mod_monster_id"
        base = re.sub(r"_\d+$", "", enemy.get("entity_id") or "")
        if base and _norm(base) in self.by_id:
            return self.by_id[_norm(base)], "entity_id"
        if enemy.get("name") and _norm(enemy["name"]) in self.by_title:
            return self.by_title[_norm(enemy["name"])], "display_name"
        return None, None

    # -- pattern model --------------------------------------------------------
    @staticmethod
    def _graph(rec: dict):
        pattern = rec.get("move_pattern") if isinstance(rec.get("move_pattern"), dict) else {}
        follow: dict[str, set] = {}
        for edge in pattern.get("follow_ups") or []:
            a, sep, b = str(edge).partition(" -> ")
            if sep:
                follow.setdefault(a.strip(), set()).add(b.strip())
        weights: dict[str, float] = {}
        no_repeat = set()
        for br in pattern.get("random_branches") or []:
            if isinstance(br, dict) and br.get("move"):
                weights[br["move"]] = weights.get(br["move"], 0.0) + float(br.get("weight") or 1)
                if br.get("repeat") == "CannotRepeat":
                    no_repeat.add(br["move"])
        return pattern, follow, weights, no_repeat

    @staticmethod
    def _next(move: str, follow, weights, no_repeat) -> dict | None:
        """Distribution of the move after ``move``; None = not described by the data."""
        nxt = follow.get(move)
        if nxt and len(nxt) == 1:
            return {next(iter(nxt)): 1.0}
        if nxt:  # several listed follow-ups: a condition the data does not describe
            return None
        allowed = {m: w for m, w in weights.items() if not (m == move and m in no_repeat)}
        total = sum(allowed.values())
        return {m: w / total for m, w in allowed.items()} if total > 0 else None

    @staticmethod
    def _intents(move: dict, ascension: int | None) -> list:
        out = []
        for it in move.get("intents") or []:
            row = {"type": it.get("type")}
            for key in ("damage", "hits"):
                if key not in it:
                    continue
                high, base, asc = it[key], it.get(f"{key}_base", it[key]), it.get("ascension")
                if asc is None or base == high:
                    row[key] = high
                elif ascension is None:
                    row[key] = {"below_ascension": base, f"ascension_{asc}_plus": high}
                else:
                    row[key] = high if ascension >= asc else base
            out.append(row)
        return out

    def _current(self, enemy: dict, moves: dict) -> tuple[str | None, str]:
        nid = enemy.get("next_move_id")
        if nid:
            return (nid, "mod_next_move_id") if nid in moves else (None, "next_move_id_not_in_reference")
        observed = enemy.get("intents")
        if not observed:
            return None, "next_move_not_exposed"
        sig = sorted((_kind(i.get("type")), (i.get("hits") or 1) if _kind(i.get("type")) == "attack" else 0)
                     for i in observed)
        hits = [mid for mid, mv in moves.items()
                if sorted((_kind(i.get("type")), (i.get("hits") or 1) if _kind(i.get("type")) == "attack" else 0)
                          for i in mv.get("intents") or []) == sig]
        if len(hits) == 1:
            return hits[0], "inferred_from_intent_types"
        return None, "ambiguous_intent_match" if hits else "no_intent_match"

    def annotate(self, enemy: dict, *, ascension: int | None) -> tuple[list, dict]:
        """Return (intent_forecast, move_pattern) for one normalized enemy."""
        observed = {"enemy_turn_offset": 1,
                    "source": "observed_next_move" if enemy.get("intents") is not None else "not_exposed",
                    "intents": enemy.get("intents")}
        rec, matched_by = self._record(enemy)
        if rec is None:
            unknown = [{"enemy_turn_offset": k, "source": "unknown", "reason": "enemy_not_in_reference_data"}
                       for k in range(2, FORECAST_TURNS + 1)]
            return [observed, *unknown], {"available": False, "reason": "enemy_not_in_reference_data"}
        moves = {m["id"]: m for m in rec["moves"] if isinstance(m, dict) and m.get("id")}
        title = lambda mid: (moves.get(mid) or {}).get("title") or mid  # noqa: E731
        pattern, follow, weights, no_repeat = self._graph(rec)
        current, position_source = self._current(enemy, moves)
        if current:
            observed.update({"move_id": current, "move_title": title(current)})

        forecast = [observed]
        dist = {current: 1.0} if current else None
        for k in range(2, FORECAST_TURNS + 1):
            if dist is None:
                forecast.append({"enemy_turn_offset": k, "source": "unknown",
                                 "reason": f"current pattern position unknown ({position_source})" if k == 2
                                 else "previous turn not predictable from the data"})
                continue
            nxt, unknown_p = {}, 0.0
            for m, p in dist.items():
                step = self._next(m, follow, weights, no_repeat)
                if step is None:
                    unknown_p += p
                    continue
                for m2, p2 in step.items():
                    nxt[m2] = nxt.get(m2, 0.0) + p * p2
            nxt = {m: p for m, p in nxt.items() if p > _MIN_PROB}
            if not nxt:
                forecast.append({"enemy_turn_offset": k, "source": "unknown",
                                 "reason": "the data gives no single next move (conditional or missing transition)"})
                dist = None
                continue
            entry = {"enemy_turn_offset": k, "source": "reference_pattern_prediction"}
            if len(nxt) == 1 and unknown_p <= _MIN_PROB:
                mid = next(iter(nxt))
                entry.update({"certainty": "deterministic_per_data", "move_id": mid, "move_title": title(mid),
                              "intents": self._intents(moves.get(mid, {}), ascension),
                              "effects": (moves.get(mid) or {}).get("effects")})
            else:
                entry["certainty"] = "random"
                entry["possible_moves"] = [
                    {"move_id": mid, "move_title": title(mid), "probability": round(p, 3),
                     "intents": self._intents(moves.get(mid, {}), ascension),
                     "effects": (moves.get(mid) or {}).get("effects")}
                    for mid, p in sorted(nxt.items(), key=lambda kv: -kv[1])]
                if unknown_p > _MIN_PROB:
                    entry["unpredicted_probability"] = round(unknown_p, 3)
            forecast.append(entry)
            dist = nxt if unknown_p <= _MIN_PROB else None

        info = {
            "available": True, "data_version": DATA_VERSION, "matched_by": matched_by,
            "monster": rec.get("title") or rec.get("class_name"),
            "description": rec.get("move_pattern_desc"),
            "current_move": {"move_id": current, "move_title": title(current) if current else None,
                             "position_source": position_source},
            "pattern_position": self._position(current, follow, weights, no_repeat, title),
            "recent_moves": [title(m) for m in enemy["move_history"]] if enemy.get("move_history") is not None else None,
            "starts_with": title(pattern["starts_with"]) if pattern.get("starts_with") else None,
            "transitions": [f"{title(a)} -> {title(b)}" for a in follow for b in sorted(follow[a])],
            "random_choices": [{"move_title": title(m), "weight": w, "cannot_repeat": m in no_repeat}
                               for m, w in weights.items()],
        }
        if self.game_build and self.game_build != DATA_VERSION:
            info["version_warning"] = f"reference data is {DATA_VERSION}; configured game_build is {self.game_build}"
        return forecast, info

    def _position(self, current, follow, weights, no_repeat, title) -> dict | None:
        """Where ``current`` sits: the deterministic path from it, and its index in a loop."""
        if not current:
            return None
        path = [current]
        while True:
            step = self._next(path[-1], follow, weights, no_repeat)
            if step is None or len(step) != 1:
                ends = "unknown_transition" if step is None else "random_choice"
                return {"path_from_current": [title(m) for m in path], "then": ends}
            nxt = next(iter(step))
            if nxt in path:
                loop = path[path.index(nxt):]
                return {"cycle": [title(m) for m in loop], "length": len(loop),
                        "current_index": loop.index(current) if current in loop else None,
                        "opening_moves_before_cycle": [title(m) for m in path[:path.index(nxt)]]}
            path.append(nxt)


_ACTIVE: MonsterReference | None = None


def configure(path: str | None, *, game_build: str | None = None) -> MonsterReference | None:
    global _ACTIVE
    _ACTIVE = MonsterReference.load(path, game_build=game_build)
    return _ACTIVE


def active() -> MonsterReference | None:
    return _ACTIVE
