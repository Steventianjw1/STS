"""The game builds the agent has a version for, and which one is in use.

A version of the agent belongs to one game build: its deck plans and guidance text
(``evidence.deck_balance``, ``strategic``), its pilot tuning and fight starts
(``pilot/builds/<build>``), the Spire Mentor release whose simulator and advisor play that
build, and the public player statistics that describe it. Nothing of one build is used for
another: a patch changes cards, enemies and what good decks look like.

One build is in use at a time (``active``): the one named by ``select`` (the configured
``game_build``, when it names a build listed here), else by the environment variable
``STS_AGENT_GAME_BUILD``, else ``DEFAULT``. ``game_pin`` keeps the running game on it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

ENV = "STS_AGENT_GAME_BUILD"


@dataclass(frozen=True)
class Build:
    key: str                # the agent's name for the build, and the folder name of everything kept for it
    same_as: tuple          # other builds with the same rules and cards (hotfixes, the beta it shipped as)
    engine_version: str | None      # what Spire Mentor's simulator calls it; None: the simulator's only build
    codex_builds: tuple     # Spire Codex build ids whose runs describe it, the ones read first first
    mentor_release: str     # the Spire Mentor mod release whose simulator and advisor go with it
    mentor_commit: str      # the commit of tfriedel/spire-mentor that released it
    engine_dir: str         # where a build of that simulator from source is kept (ignored by Git)
    source_dir: str         # where a full checkout of that commit is kept, for whole runs and card text
    live: str               # what has been established against the running game


BUILDS = {
    "v0.107.1": Build(
        key="v0.107.1", same_as=("v0.107.0",), engine_version=None, codex_builds=("v0.107.1", "v0.107.0"),
        mentor_release="0.10.6", mentor_commit="fe9cde7f9a377d8846e14853bae1f3b3d64cb65b",
        engine_dir="data/engine", source_dir="data/mentor_build",
        live="mod build, startup, API reads and played runs recorded on v0.107.1 (docs/LOCAL_VALIDATION.md)"),
    "v0.111.0": Build(
        key="v0.111.0", same_as=("v0.111.1",), engine_version="v0.111.0", codex_builds=("v0.111.1", "v0.111.0"),
        mentor_release="0.11.8", mentor_commit="4c4641612d2992926e89a97d38220eaef66b3658",
        engine_dir="data/engine_0.11.8", source_dir="data/mentor_build_0.11.8",
        live="nothing yet: the mod port is source-level and has not been built against or run on v0.111"),
}
DEFAULT = "v0.111.0"

_selected: str | None = None


def _norm(value) -> str:
    return str(value or "").strip().lower().removeprefix("v")


def find(build) -> Build | None:
    """The agent's build for a build id as the game, the configuration or Spire Codex spells it; None for others."""
    wanted = _norm(build)
    if not wanted:
        return None
    for entry in BUILDS.values():
        if wanted in (_norm(entry.key), *map(_norm, entry.same_as)):
            return entry
    return None


def select(build) -> Build | None:
    """Use the version for ``build`` from here on; returns it, or None (and changes nothing) for an unknown build."""
    global _selected
    entry = find(build)
    if entry is not None:
        _selected = entry.key
    return entry


def active() -> Build:
    """The build in use: the selected one, else the environment's, else the default."""
    return BUILDS[_selected] if _selected else (find(os.environ.get(ENV)) or BUILDS[DEFAULT])


def get(build=None) -> Build:
    """``build`` as a Build (a key, an alias or a Build); the active one for None. Unknown builds raise."""
    if build is None:
        return active()
    if isinstance(build, Build):
        return build
    entry = find(build)
    if entry is None:
        raise KeyError(f"no version of the agent for game build {build!r}; it has {', '.join(BUILDS)}")
    return entry
