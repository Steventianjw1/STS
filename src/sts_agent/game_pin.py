"""Keep the agent on the one game build its mod and combat simulator support.

The build is read from the running game's own files, never from configuration.
A frozen copy of that build is kept outside Steam's library so an update cannot
replace it; the start script launches that copy, after confirming the mod is
installed in it, and then confirms that build is what is running. Change
PINNED_GAME_BUILD only after the mod and the simulator/reference data have been
ported to a newer patch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .client import GameClientError

PINNED_GAME_BUILD = "v0.107.1"
STEAM_APP_ID = "2868840"
GAME_FOLDER = "Slay the Spire 2"
RELEASE_INFO = "release_info.json"
MARKER = "sts_agent_pin.json"
MOD_NAME = "STS2_MCP"
_VERSION = re.compile(r"v?\d+(\.\d+)+", re.I)


class GamePinError(Exception):
    pass


def _norm(build) -> str | None:
    return str(build).strip().lower().removeprefix("v") if build else None


def is_pinned(build) -> bool:
    return _norm(build) == _norm(PINNED_GAME_BUILD)


def _strings(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, str(k).lower())
    elif isinstance(node, str):
        yield key, node.strip()


def read_build(game_dir) -> str | None:
    """Version from the game's release_info.json; None when it cannot be read."""
    try:
        data = json.loads((Path(game_dir) / RELEASE_INFO).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    found = [(k, v) for k, v in _strings(data) if _VERSION.match(v)]
    for wanted in (lambda k: k == "version", lambda k: "version" in k, lambda k: True):
        for k, v in found:
            if wanted(k):
                return v
    return None


def _process_executables() -> list[str]:
    if sys.platform != "win32" and os.path.isdir("/proc/self"):
        out = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    out.append(os.readlink(f"/proc/{pid}/exe"))
                except OSError:
                    pass
        return out
    cmd = ["ps", "-axo", "comm="] if sys.platform != "win32" else [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Process | ForEach-Object { $_.Path }"]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def _pick_exe(paths) -> Path | None:
    paths = [p for p in paths if not any(word in p.name.lower() for word in ("console", "crash"))]
    paths = [p for p in paths if "spire" in p.name.lower()] or paths
    return paths[0] if len(paths) == 1 else None


def running_games() -> list[tuple[Path, Path | None]]:
    """(install folder, executable) for every running process started from a game install."""
    folders: dict[Path, list[Path]] = {}
    for exe in map(Path, _process_executables()):
        for folder in list(exe.parents)[:3]:
            if (folder / RELEASE_INFO).is_file():
                folders.setdefault(folder, []).append(exe)
                break
    return [(folder, _pick_exe(sorted(set(exes)))) for folder, exes in folders.items()]


def verify_running() -> list[tuple[Path, Path | None]]:
    """Running games, after refusing any that is not the pinned build."""
    games = running_games()
    for folder, _ in games:
        build = read_build(folder)
        if not is_pinned(build):
            raise GamePinError(
                f"the running game at {folder} is {build or 'an unreadable build'}, but the mod and simulator only "
                f"support {PINNED_GAME_BUILD}. Close it and run ./start, which launches the saved {PINNED_GAME_BUILD} copy.")
    return games


def require_running() -> Path:
    """Folder of the running game, confirmed to be the pinned build. Fails closed."""
    games = verify_running()
    if not games:
        raise GamePinError(
            f"the mod answers, but no local game process could be identified, so its build cannot be confirmed as "
            f"{PINNED_GAME_BUILD}. Close the game and run ./start, which launches the saved {PINNED_GAME_BUILD} copy.")
    return games[0][0]


def installed_dirs(game_dir=None) -> list[Path]:
    """Game folders on disk: the configured one, then every Steam library."""
    out = [Path(game_dir)] if game_dir else []
    home = Path.home()
    roots = [Path(os.environ[name]) / "Steam" for name in ("ProgramFiles(x86)", "ProgramFiles") if os.environ.get(name)]
    roots += [home / ".steam" / "steam", home / ".local" / "share" / "Steam",
              home / "Library" / "Application Support" / "Steam"]
    for root in roots:
        libraries = [root]
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
            libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        except OSError:
            pass
        out += [library / "steamapps" / "common" / GAME_FOLDER for library in libraries]
    return [d for d in dict.fromkeys(out) if (d / RELEASE_INFO).is_file()]


def frozen_copy(copy_root) -> Path:
    return Path(copy_root) / PINNED_GAME_BUILD


def _marker(copy: Path) -> dict:
    try:
        data = json.loads((copy / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mod_files(game_dir) -> tuple[Path | None, Path | None]:
    """The mod's DLL and manifest under <game>/mods: top level, or one folder down."""
    mods = Path(game_dir) / "mods"

    def find(name):
        places = [mods / name, *sorted(mods.glob(f"*/{name}"))] if mods.is_dir() else []
        return next((p for p in places if p.is_file()), None)
    return find(MOD_NAME + ".dll"), find(MOD_NAME + ".json")


def describe_mod(game_dir) -> str | None:
    """Which mod build a game folder holds; None when the DLL is not there."""
    dll, manifest = mod_files(game_dir)
    if dll is None:
        return None
    try:
        version = json.loads(manifest.read_text(encoding="utf-8-sig")).get("version") if manifest else None
    except (OSError, ValueError, AttributeError):
        version = None
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(dll.stat().st_mtime))
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()[:12]
    label = f"version {version}" if version else ("manifest unreadable" if manifest else f"no {MOD_NAME}.json manifest")
    return f"{MOD_NAME} mod ({label}, file dated {built}, sha256 {digest}) at {dll}"


def require_mod(game_dir) -> str:
    """Description of the mod in a game folder. Fails closed when the game would start without it."""
    found = describe_mod(game_dir)
    if found is None:
        raise GamePinError(
            f"{Path(game_dir) / 'mods'} has no {MOD_NAME}.dll, so this {PINNED_GAME_BUILD} copy would start without "
            f"the mod. Copy the {MOD_NAME}.dll built for {PINNED_GAME_BUILD} with this repository's extension, and "
            f"its manifest {MOD_NAME}.json, into that folder, then run ./start again.")
    return found


def save_copy(copy: Path, games, game_dir=None, log=print) -> None:
    """Create the frozen copy when it is missing, from the running or installed pinned game."""
    if copy.exists():
        if is_pinned(read_build(copy)):
            return
        raise GamePinError(f"{copy} exists but is not a {PINNED_GAME_BUILD} game folder; move it away and retry.")
    source, exe = games[0] if games else (None, None)
    if source is None:
        installs = installed_dirs(game_dir)
        source = next((d for d in installs if is_pinned(read_build(d))), None)
        if source is None:
            seen = ", ".join(f"{d} is {read_build(d) or 'an unreadable build'}" for d in installs) or "no install found"
            raise GamePinError(
                f"there is no saved {PINNED_GAME_BUILD} copy at {copy} and no installed {PINNED_GAME_BUILD} game "
                f"({seen}). Put a {PINNED_GAME_BUILD} game folder, with the mod installed, at {copy} "
                f"or set game_dir to one.")
    partial = copy.with_name(copy.name + ".partial")
    log(f"pin: saving {PINNED_GAME_BUILD} from {source} to {copy} (one-time full copy; this can take minutes)")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        shutil.copytree(source, partial, symlinks=True)
        if not is_pinned(read_build(partial)):
            raise GamePinError(f"{source} changed while it was being copied; nothing was saved.")
        relative = exe.relative_to(source).as_posix() if exe else None
        (partial / MARKER).write_text(json.dumps({"build": PINNED_GAME_BUILD, "exe": relative}), encoding="utf-8")
        os.replace(partial, copy)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if describe_mod(copy) is None:
        log(f"pin: WARNING: {copy / 'mods'} has no {MOD_NAME}.dll; install the mod there before launching this copy")
    log(f"pin: saved; a Steam update can no longer replace {PINNED_GAME_BUILD}")


def _game_exe(copy: Path) -> Path:
    name = _marker(copy).get("exe")
    if name and (copy / name).is_file():
        return copy / name
    if sys.platform == "win32":
        found = list(copy.glob("*.exe"))
    else:
        found = [p for p in copy.iterdir() if p.is_file() and os.access(p, os.X_OK) and p.suffix in ("", ".x86_64", ".sh")]
    exe = _pick_exe(sorted(found))
    if exe is None:
        raise GamePinError(f'cannot tell which file starts the game in {copy}; name it in {copy / MARKER} '
                           f'as {{"exe": "<file name>"}}.')
    return exe


def launch(copy: Path, log=print) -> None:
    """Start the frozen copy directly, so Steam neither updates nor substitutes it."""
    exe = _game_exe(copy)
    (exe.parent / "steam_appid.txt").write_text(STEAM_APP_ID, encoding="ascii")
    log(f"pin: launching {PINNED_GAME_BUILD} from {exe}")
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen([str(exe)], cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env={**os.environ, "SteamAppId": STEAM_APP_ID,
                                                     "SteamGameId": STEAM_APP_ID}, **detach)


def _reachable(client) -> bool:
    try:
        client.hello()
        return True
    except GameClientError:
        return False


def _await_mod(client, wait_s: float, launched: bool) -> None:
    """Wait for the mod to answer; stop early when the game closes or another build takes its place."""
    started = time.monotonic()
    next_look = started + 15.0
    seen, misses = False, 0
    while not _reachable(client):
        now = time.monotonic()
        if now >= next_look:
            next_look = now + 10.0
            try:
                games = verify_running()
            except GamePinError as exc:
                if not launched:
                    raise
                raise GamePinError(f"{exc} It appeared after the saved copy was launched; if that repeats, the "
                                   f"launch is being handed to Steam's own install.") from None
            seen, misses = seen or bool(games), 0 if games else misses + 1
            if launched and seen and misses >= 2:
                raise GamePinError(
                    f"the {PINNED_GAME_BUILD} game closed before the mod answered at {client.base_url}. "
                    f"Start Steam first, then run ./start again; if the game still closes, read the game's own log.")
        if now >= started + wait_s:
            state = ("is running but the mod never answered: mods are disabled in the game (enable them in the "
                     "game's mod settings and restart it), the mod failed to load, or it listens on another port "
                     f"than {client.base_url}" if seen else
                     f"did not bring up the mod at {client.base_url}: check that Steam is running and mods are enabled")
            raise GamePinError(f"after {wait_s:.0f}s the {PINNED_GAME_BUILD} game {state}.")
        time.sleep(1.0)


def start_pinned(client, *, game_dir=None, copy_root="data/game", wait_s=180.0, log=print) -> Path:
    """Start-script gate: save the pinned build, launch it with the mod when no game runs, verify what runs."""
    copy = frozen_copy(copy_root)
    games = verify_running()
    try:
        save_copy(copy, games, game_dir, log)
    except (GamePinError, OSError) as exc:
        if not games:
            raise GamePinError(str(exc)) from None
        log(f"pin: WARNING: no frozen copy was saved ({exc}); a Steam update can still replace this install")
    if not _reachable(client):
        if games:
            log(f"pin: {PINNED_GAME_BUILD} is running; waiting for the mod")
        else:
            log(f"pin: {require_mod(copy)}")   # never launch a copy that would come up without the mod
            launch(copy, log)
        _await_mod(client, wait_s, launched=not games)
    folder = require_running()
    log(f"pin: game build {PINNED_GAME_BUILD} verified at {folder}; the mod answers at {client.base_url}")
    if folder.resolve() != copy.resolve():
        log(f"pin: this game runs from {folder}, not the saved copy; "
            f"{describe_mod(folder) or 'its mods folder has no ' + MOD_NAME + '.dll'}")
    return folder
