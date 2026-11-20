"""Switch the game's Spire Mentor between its packaged advisor and the pilot's.

The mod starts ``SimAdvisor.cmd`` next to SpireMentor.dll when that file exists and its
packaged ``SimAdvisor.exe`` otherwise. ``sync`` writes or removes that one file in the
pinned game copy; nothing of the mod itself is changed. The mod starts its advisor
afresh for every fight, so the switch takes effect at the next fight, also in a game
that is already running.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ..game.game_pin import frozen_copy
from .advisor import GAME_DIR_ENV, SOURCE_ENV

LAUNCHER = "SimAdvisor.cmd"
MARK = "rem sts-agent pilot launcher"       # only a file carrying this line is ever replaced or removed


def mod_dir(cfg) -> Path:
    return frozen_copy(cfg.pinned_game_dir) / "mods" / "SpireMentor"


def source_dir(cfg) -> Path:
    return Path(cfg.autobattle.pilot_source).resolve() / "sts2-pycli" / "python"


def _ours(path: Path) -> bool:
    try:
        return MARK in path.read_text(encoding="mbcs" if os.name == "nt" else "utf-8", errors="replace")
    except OSError:
        return False


def launcher_text(cfg) -> str:
    mod, logs = mod_dir(cfg).resolve(), r"%LOCALAPPDATA%\SpireMentor\logs"
    return "\r\n".join([
        "@echo off",
        MARK + " (written by sts-agent; [autobattle] pilot = false removes it)",
        f'set "{SOURCE_ENV}={source_dir(cfg)}"',
        f'set "{GAME_DIR_ENV}={Path(cfg.pinned_game_dir).resolve()}"',
        'set "PYTHONIOENCODING=utf-8"',
        # The mod's own arguments (%*) come last, so its port always wins.
        f'"{sys.executable}" -m sts_agent.pilot.advisor --log-dir "{logs}\\realgame_advisor" '
        f'--workbench-logs "{logs}" --workbench off --workbench-dir "{mod / "workbench"}" %*',
        ""])


def status(cfg) -> tuple[bool, str]:
    """(ready, why): whether a game started now would fight with the pilot."""
    path = mod_dir(cfg) / LAUNCHER
    if not cfg.autobattle.pilot:
        return False, "off ([autobattle] pilot = false); Mentor's packaged advisor decides combat"
    if not (source_dir(cfg) / "realgame_advisor.py").is_file():
        return False, f"Mentor's advisor source is missing at {source_dir(cfg)}; run python tools/fetch_mentor_source.py"
    if not _ours(path):
        return False, f"{path} is not installed yet; it is written when the agent starts the game"
    return True, f"the mod starts the pilot's advisor ({path})"


def sync(cfg, log=print) -> bool:
    """Make the launcher match ``[autobattle] pilot``. Returns whether the pilot is installed."""
    path = mod_dir(cfg) / LAUNCHER
    wanted = bool(cfg.autobattle.enabled and cfg.autobattle.pilot)
    if not wanted:
        if _ours(path):
            path.unlink()
            log("pilot: removed the launcher; Spire Mentor uses its packaged advisor again")
        return False
    if not (source_dir(cfg) / "realgame_advisor.py").is_file():
        log(f"pilot: Mentor's advisor source is missing at {source_dir(cfg)} (python tools/fetch_mentor_source.py); "
            "Mentor's packaged advisor decides combat")
        if _ours(path):
            path.unlink()
        return False
    if path.exists() and not _ours(path):
        log(f"pilot: {path} was not written by sts-agent and is left alone; Mentor's own launcher decides combat")
        return False
    if not path.parent.is_dir():
        log(f"pilot: Spire Mentor is not installed at {path.parent}")
        return False
    text = launcher_text(cfg)
    try:
        data = text.encode("mbcs" if os.name == "nt" else "utf-8")   # cmd.exe reads the file in the system code page
    except UnicodeEncodeError:
        log("pilot: a path cannot be written in this system's code page; Mentor's packaged advisor decides combat")
        return False
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
        log(f"pilot: installed {path}; it applies from the next fight")
    return True
