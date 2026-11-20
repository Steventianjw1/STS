"""The interface process: panels, game window, health checks and the session thread.

Started by ``sts-agent ui`` (``./start``). By default the game and the panels are one
window: Spire Mentor's frame host shows this interface's page and holds the game window
inside it (``windows.find_frame_host``). A game that is already open is adopted and gets
its window back when the window is closed; otherwise the game is started inside the
window, and closing the window closes the game. Closing the window ends the interface.
Without the host, or with ``[ui].single_window = false``, separate panel windows are
tiled around the game window as before.

Opened by ``sts-agent ui`` it never plays by itself: a run starts
when "Continue run" or "New run" is pressed, in a worker thread that runs the same
``run_llm_session`` as the command line, with the hub attached as its hooks.
Health checks only read (``GET /`` on the mod, the Autobattle status command, a
process listing); they never read the game state, because a state read can open a
shop or a chest inside the mod.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from .. import cli
from ..game import game_pin
from ..game.autobattle import MentorAutobattle
from ..game.client import GameClient, GameClientError, parse_mod_version
from ..pilot import install as pilot_install
from ..llm import API_KEY_ENV
from ..game.startup import CHARACTERS, RunStartup, StartupError, ascension_level, character_id
from . import windows
from .hub import UiHub
from .server import UiServer

TITLES = {"controls": "STS Agent · Controls", "log": "STS Agent · Combat log", "side": "STS Agent · Run"}


class UiApp:
    def __init__(self, cfg, client, *, launch_game: bool = True, open_panels: bool = True, autostart=None,
                 setup_timeout_s: float | None = None):
        self.cfg, self.client = cfg, client
        self.launch_game, self.open_panels = launch_game, open_panels
        # ``sts-agent start-run``: ("new", character, ascension) begins that run once the game is up.
        self.autostart, self.setup_timeout_s = autostart, setup_timeout_s
        self.hub = UiHub()
        # Health checks use their own client so they never share a connection with the session.
        self.probe = GameClient(cfg.base_url, cfg.read_timeout_s, cfg.action_timeout_s)
        self.server: UiServer | None = None
        self.layout: dict | None = None
        self._quit = threading.Event()
        self._session: threading.Thread | None = None
        self._lock = threading.Lock()
        self._placed = None            # (window handle, time placed, confirmed)
        self._build_ok_for = None      # mod greeting the build check was made for
        self.frame_exe: str | None = None   # frame host; None = tiled panel windows
        self._frame = None             # the frame host process showing the single window
        self._frame_hwnd = None
        self._frame_attached = False   # True: it adopted a running game; False: it waits for one started inside it
        self._frame_lock = threading.Lock()
        self._outside_since = None     # when a game window outside the single window was first seen
        self._booting = False

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> int:
        self.server = UiServer(self.hub, self.command, self.cfg.ui.port)
        self.server.start()
        if self.open_panels and self.cfg.ui.single_window:
            self.frame_exe = windows.find_frame_host(game_pin.frozen_copy(self.cfg.pinned_game_dir), self.cfg.game_dir)
            if self.frame_exe is None and windows.WINDOWS:
                print(f"interface: no frame host ({windows.FRAME_HOST}) in the game folder; "
                      "using separate panel windows")
        area = None if self.frame_exe else windows.work_area()
        self.layout = windows.compute_layout(area, self.cfg.ui.game_fraction) if area else None
        llm = self.cfg.llm
        self.hub.info = {"provider": llm.provider, "model": llm.model, "autobattle": self.cfg.autobattle.enabled,
                         "characters": list(CHARACTERS), "build": game_pin.PINNED_GAME_BUILD,
                         "live_thinking": bool(self.cfg.ui.live_thinking and llm.provider == "deepseek"),
                         "tiled": self.layout is not None, "single_window": self.frame_exe is not None}
        self._check_model()
        if not self.cfg.autobattle.enabled:
            self.hub.set_health("autobattle", "off", "disabled: the model plays the fights")
        self.hub.set_health("evidence", "unknown" if self.cfg.expert_stats.enabled else "off",
                            "checked at the first decision that uses it" if self.cfg.expert_stats.enabled
                            else "disabled in the configuration")
        print(f"interface: {self.server.url('all')}")
        if self.frame_exe:
            self._booting = self.launch_game
            threading.Thread(target=self._boot_frame, name="ui-frame", daemon=True).start()
        else:
            if self.open_panels:
                self._open_panels()
            if self.launch_game or self.autostart:
                threading.Thread(target=self._boot, name="ui-game", daemon=True).start()
        threading.Thread(target=self._health_loop, name="ui-health", daemon=True).start()
        print("interface: running. " + ("The run starts by itself; " if self.autostart else
                                        "Start a run from the control bar; ")
              + ("close the window or " if self.frame_exe else "") + "press Ctrl+C here to close.")
        try:
            while not self._quit.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        self._shutdown()
        return 0

    def _shutdown(self):
        session = self._session
        if session is not None and session.is_alive():
            self.hub.request_stop()
            print("interface: waiting for the session to stop after its current step (Ctrl+C again to leave now)")
            try:
                session.join(timeout=300)
            except KeyboardInterrupt:
                pass
            if session.is_alive() and self.cfg.autobattle.enabled:
                try:   # do not leave Autobattle playing a fight nobody is watching
                    MentorAutobattle(self.probe).control("off")
                except GameClientError:
                    pass
        self._close_frame()
        if self.frame_exe is None:
            for marker in TITLES.values():
                windows.close(windows.find_window(marker))
        if self.server is not None:
            self.server.stop()

    # ------------------------------------------------------------------ single window
    def _boot_frame(self):
        """Open the single window; adopt a game that is already open, else start the game inside it."""
        try:
            hwnd = windows.game_window()
            self._open_frame((windows.window_pid(hwnd), hwnd) if hwnd else None)
        except OSError as exc:
            self.hub.log(f"interface: could not open the window ({exc})")
            self.hub.set_health("game", "down", f"the window did not open: {exc}")
        self._boot()

    def _open_frame(self, attach=None):
        fraction = min(0.9, max(0.4, float(self.cfg.ui.game_fraction)))
        url = self.server.url("frame", gf=f"{fraction:.3f}")
        with self._frame_lock:
            proc = windows.open_frame(self.frame_exe, url, game_pin.frozen_copy(self.cfg.pinned_game_dir), attach)
            self._frame, self._frame_hwnd, self._frame_attached, self._outside_since = proc, None, bool(attach), None
        threading.Thread(target=self._watch_frame, args=(proc,), name="ui-frame-watch", daemon=True).start()

    def _watch_frame(self, proc):
        """Name the window once it is up; when the user closes it, the interface ends with it."""
        deadline = time.monotonic() + 30.0
        while proc.poll() is None and self._frame is proc and time.monotonic() < deadline:
            hwnd = windows.frame_window(proc.pid)
            if hwnd:
                windows.set_title(hwnd, "STS Agent")
                self._frame_hwnd = hwnd
                windows.minimize_console()
                break
            time.sleep(0.2)
        proc.wait()
        if self._frame is proc and not self._quit.is_set():
            print("interface: the window was closed")
            self._quit.set()

    def _frame_handle(self, wait_s: float = 0.0):
        deadline = time.monotonic() + wait_s
        while True:
            proc = self._frame
            if proc is None or proc.poll() is not None or self._frame_hwnd or time.monotonic() >= deadline:
                return self._frame_hwnd
            time.sleep(0.2)

    def _close_frame(self, wait_s: float = 20.0):
        """Close the single window. An adopted game gets its window back; one started inside is asked to quit."""
        with self._frame_lock:
            proc, hwnd, self._frame, self._frame_hwnd = self._frame, self._frame_hwnd, None, None
        if proc is None or proc.poll() is not None:
            return
        windows.close(hwnd or windows.frame_window(proc.pid))
        try:
            proc.wait(wait_s)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _launch_in_frame(self, copy, log):
        """Launcher for ``game_pin.start_pinned``: the game opens as part of the single window."""
        hwnd = self._frame_handle(wait_s=20.0)
        if hwnd:
            game_pin.launch(copy, log, args=("--wid", str(int(hwnd))))
        else:   # no window to open in: start it normally; the health check adopts its window
            game_pin.launch(copy, log)

    def _check_frame(self):
        proc, frame_hwnd = self._frame, self._frame_hwnd
        if proc is None or proc.poll() is not None:
            self.hub.set_health("game", "down", "the window is not open")
            return
        if windows.embedded_game(frame_hwnd):
            self._outside_since = None
            self.hub.set_health("game", "ok", "inside the STS Agent window")
            return
        outside = windows.game_window()
        if not outside:
            self._outside_since = None
            self.hub.set_health("game", "down", "Starting the game…" if self._booting else "The game is not running")
            return
        now = time.monotonic()
        self._outside_since = self._outside_since or now
        self.hub.set_health("game", "warn", "Bringing the game into this window…")
        # A game that opened its own window (started by hand, or it ignored --wid): adopt that window.
        # An adoption in progress gets longer before it is tried again.
        if frame_hwnd and now - self._outside_since >= (15.0 if self._frame_attached else 4.0):
            self.hub.log("interface: adopting the game window")
            self._close_frame(wait_s=5.0)
            self._open_frame((windows.window_pid(outside), outside))

    # ------------------------------------------------------------------ windows
    def _open_panels(self):
        browser = windows.find_browser(self.cfg.ui.browser)
        if browser is None or self.layout is None:
            print("interface: no Edge or Chrome found for separate panel windows; opening one page instead")
            webbrowser.open(self.server.url("all"))
            return
        profile = str(Path(self.cfg.pinned_game_dir).resolve().parent / "ui_browser")
        try:
            for name in TITLES:
                windows.open_panel(browser, self.server.url(name), self.layout[name], profile)
        except OSError as exc:
            print(f"interface: could not open the panel windows ({exc}); opening one page instead")
            webbrowser.open(self.server.url("all"))
            return
        threading.Thread(target=self._place_panels, name="ui-panels", daemon=True).start()

    def _place_panels(self, wait_s: float = 20.0):
        """The browser applies its own remembered sizes; put each window where the layout wants it."""
        deadline, pending = time.monotonic() + wait_s, dict(TITLES)
        while pending and time.monotonic() < deadline and not self._quit.is_set():
            for name, marker in list(pending.items()):
                hwnd = windows.find_window(marker)
                if hwnd and windows.place(hwnd, self.layout[name])[0]:
                    del pending[name]
            time.sleep(0.4)

    def _place_game(self) -> tuple[bool, str]:
        if self.layout is None:
            return False, "window placement needs Windows"
        hwnd = windows.game_window()
        if not hwnd:
            self.hub.set_health("game", "down", "the game is not running")
            return False, "the game is not running"
        ok, message = windows.place(hwnd, self.layout["game"])
        self._placed = (hwnd, time.monotonic(), self._placed is not None and self._placed[0] == hwnd)
        self.hub.set_health("game", "ok" if ok else "warn", ("placed " if ok else "not placed: ") + message)
        return ok, message

    def snap(self) -> tuple[bool, str]:
        """Put the game and every panel window back on the layout."""
        if self.frame_exe:
            return False, "The game and the panels are one window; there is nothing to snap"
        if self.layout is None:
            return False, "window placement needs Windows"
        ok, message = self._place_game() if self.cfg.ui.place_game_window else (True, "game window left alone")
        for name, marker in TITLES.items():
            windows.place(windows.find_window(marker), self.layout[name])
        return ok, message

    def _boot_game(self):
        """Launch the pinned game copy when no game is running, as ``./start`` always did."""
        self._booting = True
        try:
            pilot_install.sync(self.cfg, self.hub.log)
            game_pin.start_pinned(self.client, game_dir=self.cfg.game_dir, copy_root=self.cfg.pinned_game_dir,
                                  log=self.hub.log, launcher=self._launcher())
        except (game_pin.GamePinError, GameClientError, OSError) as exc:
            self.hub.log(f"game: {exc}")
            self.hub.set_health("build", "down", str(exc))
        finally:
            self._booting = False

    def _boot(self):
        """Bring the game up, then begin the run the command line asked for. One after the other,
        because both go through the pinned-game gate and only one of them may launch the game."""
        if self.launch_game:
            self._boot_game()
        if self.autostart and not self._quit.is_set():
            _, message = self._start_session(*self.autostart)
            self.hub.log(f"interface: {message}")

    def _launcher(self):
        return self._launch_in_frame if self.frame_exe else game_pin.launch

    # ------------------------------------------------------------------ health
    def _check_model(self):
        llm = self.cfg.llm
        key_env = "DEEPSEEK_API_KEY" if llm.provider == "deepseek" else API_KEY_ENV
        sdk = "openai" if llm.provider == "deepseek" else "anthropic"
        problem = (f"unknown provider {llm.provider!r}" if llm.provider not in ("deepseek", "anthropic") else
                   "no model configured (DEEPSEEK_MODEL or [llm] model)" if not llm.model else
                   f"{key_env} is not set" if not os.environ.get(key_env) else
                   f"the {sdk} package is not installed" if importlib.util.find_spec(sdk) is None else None)
        self.hub.set_health("model", "down" if problem else "ok",
                            problem or f"{llm.provider}:{llm.model} configured; not called yet")

    def _health_loop(self):
        last_autobattle = 0.0
        while not self._quit.is_set():
            try:
                self._check_window()
                greeting = self._check_mod()
                if greeting is not None and greeting != self._build_ok_for:
                    self._check_build(greeting)
                if greeting is None:
                    self._build_ok_for = None
                if (self.cfg.autobattle.enabled and greeting is not None and not self.hub.session_active()
                        and time.monotonic() - last_autobattle >= 15.0):
                    last_autobattle = time.monotonic()
                    self._check_autobattle()
            except Exception as exc:   # a failed probe is a status, never a crash of the interface
                self.hub.log(f"interface: health check failed ({type(exc).__name__}: {exc})")
            self._quit.wait(2.0)

    def _check_window(self):
        if self.frame_exe:
            return self._check_frame()
        if self.layout is None:
            self.hub.set_health("game", "unknown", "window placement needs Windows")
            return
        hwnd = windows.game_window()
        if not hwnd:
            self._placed = None
            self.hub.set_health("game", "down", "the game is not running")
            return
        if not self.cfg.ui.place_game_window:
            self.hub.set_health("game", "ok", "running; placement is turned off")
            return
        placed = self._placed
        if placed is None or placed[0] != hwnd:
            self._place_game()            # a new game window: place it once
        elif not placed[2] and time.monotonic() - placed[1] >= 6.0:
            self._place_game()            # once more after the game finished starting; then leave it to the user

    def _check_mod(self):
        try:
            greeting = self.probe.hello().get("message")
        except GameClientError:
            self.hub.set_health("mod", "down", f"no answer at {self.cfg.base_url}")
            return None
        self.hub.set_health("mod", "ok", f"STS2MCP {parse_mod_version(greeting) or 'unknown version'} answering")
        return greeting or ""

    def _check_build(self, greeting):
        try:
            folder = game_pin.require_running()
        except game_pin.GamePinError as exc:
            self.hub.set_health("build", "down", str(exc))
            return
        self._build_ok_for = greeting
        self.hub.set_health("build", "ok", f"{game_pin.PINNED_GAME_BUILD} at {folder}")

    def _check_autobattle(self):
        try:
            status = MentorAutobattle(self.probe).control("status")
        except GameClientError as exc:
            self.hub.set_health("autobattle", "down", str(exc))
            return
        self.hub.autobattle(status)

    # ------------------------------------------------------------------ commands
    def command(self, cmd: str, body: dict) -> tuple[bool, str]:
        hub = self.hub
        if cmd == "pause":
            hub.pause()
            return True, "Pausing after the step in progress"
        if cmd == "resume":
            hub.resume()
            return True, "Resumed"
        if cmd == "step":
            hub.step_once()
            return True, "One decision, then paused again"
        if cmd == "stop":
            if not hub.session_active():
                return False, "No session is running"
            hub.request_stop()
            return True, "Stopping after the step in progress"
        if cmd == "continue":
            return self._start_session("continue")
        if cmd == "new_run":
            try:
                character = character_id(str(body.get("character", "")))
                ascension = ascension_level(str(body.get("ascension", "")))
            except ValueError as exc:
                return False, str(exc)
            return self._start_session("new", character, ascension)
        if cmd == "snap":
            return self.snap()
        if cmd == "launch":
            threading.Thread(target=self._boot_game, name="ui-game", daemon=True).start()
            return True, "Starting the game"
        if cmd == "traces":
            folder = hub.snapshot()["session"].get("trace_dir") or self.cfg.trace_dir
            try:
                os.startfile(os.path.abspath(folder))   # noqa: S606 - opens the local trace folder in Explorer
            except (OSError, AttributeError) as exc:
                return False, f"cannot open {folder}: {exc}"
            return True, f"Opened {folder}"
        if cmd == "quit":
            self._quit.set()
            return True, "Closing the interface"
        return False, f"unknown command {cmd!r}"

    def _start_session(self, mode, character=None, ascension=None) -> tuple[bool, str]:
        with self._lock:
            if self.hub.session_active() or (self._session is not None and self._session.is_alive()):
                return False, "A session is already running"
            detail = (f"Starting a new {character.title()} run at ascension {ascension}" if mode == "new"
                      else "Continuing the run that is open in the game")
            self.hub.begin_session(mode, detail)
            self._session = threading.Thread(target=self._run_session, args=(mode, character, ascension),
                                             name="agent-session", daemon=True)
            self._session.start()
            return True, detail

    def _run_session(self, mode, character, ascension):
        hub, cfg, client = self.hub, self.cfg, self.client
        try:
            model = cli._build_model(cfg)   # fails before anything touches the game
            if mode == "new":
                pilot_install.sync(cfg, hub.log)
                game_pin.start_pinned(client, game_dir=cfg.game_dir, copy_root=cfg.pinned_game_dir, log=hub.log,
                                      launcher=self._launcher())
                if cfg.autobattle.enabled:
                    MentorAutobattle(client).control("off")
                timeout = {"timeout_s": self.setup_timeout_s} if self.setup_timeout_s else {}
                RunStartup(client, poll_interval_s=cfg.limits.poll_interval_s, log=hub.log,
                           **timeout).start(character, ascension)
            else:
                client.hello()
                game_pin.require_running()   # never act on a build the mod and Autobattle do not support
            cli.run_llm_session(cfg, client, model, "play-run", log=hub.log, hooks=hub)
        except (cli.SetupError, StartupError, game_pin.GamePinError, GameClientError, OSError, ValueError) as exc:
            hub.log(f"session: {exc}")
            hub.set_session("error", str(exc)[:300])
        except Exception as exc:   # the session already closed its trace; show why it ended
            hub.log(f"session crashed: {type(exc).__name__}: {exc}")
            hub.set_session("error", f"{type(exc).__name__}: {exc}"[:300])
        finally:
            if hub.session_active():
                hub.set_session("stopped", "The session ended")
