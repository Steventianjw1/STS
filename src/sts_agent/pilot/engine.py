"""Spire Mentor's combat engine, loaded from the installed mod.

The mod ships its simulator as a CPython 3.13 extension (``sts2_rs.pyd``) next to its
packaged advisor. It is imported in-process from there, so the pilot always simulates
with the engine build the installed mod plays with. Only the extension is loaded; none
of Mentor's Python is.

A machine without the game (one that only tunes) names a folder holding the module in
``STS_AGENT_ENGINE_DIR``, or keeps a build from Mentor's source in ``data/engine``.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from ..game.game_pin import frozen_copy
from .profiles import HP_WEIGHT

ENGINE_SUBDIR = Path("mods") / "SpireMentor" / "SimAdvisor" / "_internal" / "sts2_rs"
ENGINE_PYTHON = (3, 13)                 # what the mod's own sts2_rs.pyd is built for
ENGINE_ENV = "STS_AGENT_ENGINE_DIR"     # a folder holding the sts2_rs module, used instead of the installed mod's
BUILT_DIR = Path("data") / "engine"     # where a build from Mentor's source is kept
HP_WEIGHT_ENV = "STS2_RUNAI_HP_WEIGHT"   # the engine's own setting; read once, at its first search
ROOM_TIERS = {"Monster": ("Weak", "Normal"), "Elite": ("Elite",), "Boss": ("Boss",)}

_engine = None


class EngineError(Exception):
    pass


def engine_dir(pinned_game_dir="data/game") -> Path:
    return frozen_copy(pinned_game_dir) / ENGINE_SUBDIR


def load(pinned_game_dir="data/game"):
    """The ``sts2_rs`` module: the installed Spire Mentor's, else a build kept in ``data/engine``."""
    global _engine
    if _engine is None:
        named = os.environ.get(ENGINE_ENV)
        folders = [Path(named)] if named else [engine_dir(pinned_game_dir), BUILT_DIR]
        folder = next((f for f in folders if any(f.glob("sts2_rs*"))), None)
        if folder is None:
            raise EngineError("Spire Mentor's simulator (sts2_rs) was not found in " + " or ".join(str(f) for f in folders)
                              + f"; install the mod in the pinned game copy, or name the module's folder in {ENGINE_ENV}")
        if (folder / "sts2_rs.pyd").is_file() and sys.version_info[:2] != ENGINE_PYTHON:   # an untagged file: Python cannot tell
            raise EngineError(f"the mod's simulator is built for Python {ENGINE_PYTHON[0]}.{ENGINE_PYTHON[1]}, "
                              f"this is {sys.version_info[0]}.{sys.version_info[1]}")
        if HP_WEIGHT != 1.0:                   # a value already in the environment wins
            os.environ.setdefault(HP_WEIGHT_ENV, str(HP_WEIGHT))
        sys.path.insert(0, str(folder))
        try:
            _engine = importlib.import_module("sts2_rs")
        except ImportError as exc:
            raise EngineError(f"could not load the simulator from {folder}: {exc}") from exc
        finally:
            sys.path.remove(str(folder))
    return _engine


def fingerprint(rs) -> str:
    """A digest of one fixed search. Two machines that print the same one simulate and search alike."""
    deck = ["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 4 + ["BASH"]
    state = rs.build_scenario_state(1, "Ironclad", deck, 80, 80, ["BURNING_BLOOD"], "VANTOM_BOSS", 10)
    result = rs.mcts_recommend(state, sims=3000, threads=1, seed=7, hidden_info="beliefdraw")
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[:16]


def kind_key(name) -> str:
    """One spelling for a monster kind: the engine says ``LeafSlimeS``, the game's logs ``LEAF_SLIME_S``."""
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def encounters(rs) -> list[dict]:
    """The engine's encounter table, each entry with its roster as a multiset of kind keys."""
    return [{**e, "roster": Counter(kind_key(m) for m in e["monsters"])} for e in rs.list_encounters()]


def find_encounter(table, kinds, room_type) -> dict | None:
    """The encounter a fight belongs to, from the enemies on the board and the room type.

    An exact roster match wins. The engine's live state names some monsters by their family
    (three ``DecimillipedeSegment`` where the table lists Front, Middle and Back), so the next
    try reads each table kind as the board kind it starts with. A fight whose roster is rolled
    (slimes) falls back to the encounter of the right tier with the same set of kinds.
    """
    tiers = ROOM_TIERS.get(room_type)
    roster = Counter(kind_key(k) for k in kinds)
    pool = [e for e in table if tiers is None or e["tier"] in tiers]
    for e in pool:
        if e["roster"] == roster:
            return e

    def family(kind):
        return max((k for k in roster if kind.startswith(k)), key=len, default=kind)
    for e in pool:
        if Counter(family(k) for k in e["roster"].elements()) == roster:
            return e
    for e in pool:
        if set(e["roster"]) == set(roster):
            return e
    return None
