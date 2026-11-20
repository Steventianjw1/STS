"""The single window (frame host), and the older tiled layout: game-window placement and panel windows.

Single window: Spire Mentor's frame host holds one page (``panels.html?panel=frame``) and the game
window as its child; the page lays the panels out and reports the game's rectangle to the host.

Tiled layout (work area of the primary monitor, taskbar excluded), used when the host is missing or
``[ui].single_window`` is off:

    +------------------------------------+-----------------+
    | control bar                        |  map plan       |
    +------------------------------------+                 |
    |                                    +-----------------+
    |   game  (game_fraction of the      |  current step,  |
    |          width and of the height)  |  delay, status  |
    |                                    +-----------------+
    +------------------------------------+  thinking       |
    | combat log                         |                 |
    +------------------------------------+-----------------+

The right column is one window holding three separate panels. Window handling is
Windows-only (ctypes on user32/dwmapi); elsewhere every function reports that
nothing was placed and the page still works in an ordinary browser tab.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
GAME_EXE_PREFIX = "slaythespire2"
BAR_HEIGHT = 96            # control-bar window, title bar included
MIN_LOG_HEIGHT = 150

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetConsoleWindow.restype = wintypes.HWND
    _user32.EnumChildWindows.argtypes = [wintypes.HWND, _EnumProc, wintypes.LPARAM]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]

FRAME_HOST = Path("mods") / "SpireMentor" / "SpireMentorFrame" / "SpireMentorFrame.exe"
FRAME_HOST_TITLE = "Spire Mentor"      # the host names its own window; the interface renames it
GAME_WINDOW_CLASS = "Engine"           # Godot's window class, as the frame host looks for it
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_SWP_NOZORDER, _SWP_NOACTIVATE = 0x0004, 0x0010
_GWL_STYLE, _WS_CAPTION = -16, 0x00C00000
_WM_CLOSE = 0x0010
_dpi_set = False


def _dpi_aware():
    """Work in real pixels; without this Windows scales the coordinates on high-DPI screens."""
    global _dpi_set
    if _dpi_set or not WINDOWS:
        return
    _dpi_set = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            _user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


def work_area() -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the primary monitor without the taskbar; None off Windows."""
    if not WINDOWS:
        return None
    _dpi_aware()
    rect = wintypes.RECT()
    if not _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):   # SPI_GETWORKAREA
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def compute_layout(area: tuple[int, int, int, int], fraction: float = 0.70) -> dict:
    """Rectangles (x, y, w, h) for the game and the three panel windows."""
    x, y, w, h = area
    fraction = min(0.9, max(0.4, float(fraction)))
    left_w = round(w * fraction)
    bar_h = min(BAR_HEIGHT, max(72, h // 9))
    game_h = min(round(h * fraction), h - bar_h - MIN_LOG_HEIGHT)
    return {
        "controls": (x, y, left_w, bar_h),
        "game": (x, y + bar_h, left_w, game_h),
        "log": (x, y + bar_h + game_h, left_w, h - bar_h - game_h),
        "side": (x + left_w, y, w - left_w, h),
    }


def _windows() -> list[dict]:
    """Visible top-level windows with a title: handle, title, process id, executable name."""
    out = []
    if not WINDOWS:
        return out

    def visit(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, title, 512)
        if not title.value:
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": hwnd, "title": title.value, "pid": pid.value})
        return True

    _user32.EnumWindows(_EnumProc(visit), 0)
    return out


def _exe_path(pid: int) -> str:
    handle = _kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        _kernel32.CloseHandle(handle)


def _exe_name(pid: int) -> str:
    return os.path.basename(_exe_path(pid)).lower()


def window_exe(hwnd) -> str | None:
    """Full path of the program that owns a window; None when it cannot be read."""
    if not WINDOWS or not hwnd:
        return None
    return _exe_path(window_pid(hwnd)) or None


def wait_exit(pid: int, timeout_s: float) -> bool:
    """True once the process has ended; False when it is still running after ``timeout_s``."""
    if not WINDOWS:
        return True
    handle = _kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if not handle:
        return True   # nothing to wait for: it is already gone
    try:
        return _kernel32.WaitForSingleObject(handle, int(max(0.0, timeout_s) * 1000)) == 0
    finally:
        _kernel32.CloseHandle(handle)


def _rect(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _frame(hwnd) -> tuple[int, int, int, int] | None:
    """The window as drawn: Windows 10/11 keep invisible resize borders outside it."""
    r = wintypes.RECT()
    try:
        failed = ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
    except (OSError, AttributeError):
        return None
    return None if failed else (r.left, r.top, r.right - r.left, r.bottom - r.top)


def game_window():
    """Handle of the running game's main window, or None."""
    best, area = None, 0
    for win in _windows():
        name = _exe_name(win["pid"])
        if not name.startswith(GAME_EXE_PREFIX) or "console" in name:
            continue
        _, _, w, h = _rect(win["hwnd"])
        if w * h > area:
            best, area = win["hwnd"], w * h
    return best


def find_window(marker: str):
    """Handle of the first visible window whose title contains ``marker``."""
    for win in _windows():
        if marker in win["title"]:
            return win["hwnd"]
    return None


def place(hwnd, rect: tuple[int, int, int, int]) -> tuple[bool, str]:
    """Move and resize a window so that what is drawn fills ``rect``. Returns (placed, what happened)."""
    if not WINDOWS or not hwnd:
        return False, "no window"
    _dpi_aware()
    x, y, w, h = rect
    if _user32.IsIconic(hwnd) or _user32.IsZoomed(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    flags = _SWP_NOZORDER | _SWP_NOACTIVATE
    if not _user32.SetWindowPos(hwnd, None, x, y, w, h, flags):
        return False, f"Windows refused to move it (error {ctypes.get_last_error()})"
    outer, drawn = _rect(hwnd), _frame(hwnd)
    if drawn and drawn != outer:   # widen by the invisible borders so the visible edges meet
        left, top = drawn[0] - outer[0], drawn[1] - outer[1]
        right = (outer[0] + outer[2]) - (drawn[0] + drawn[2])
        bottom = (outer[1] + outer[3]) - (drawn[1] + drawn[3])
        _user32.SetWindowPos(hwnd, None, x - left, y - top, w + left + right, h + top + bottom, flags)
        drawn = _frame(hwnd) or drawn
    got = drawn or outer
    if abs(got[2] - w) > 24 or abs(got[3] - h) > 24:
        borderless = not _user32.GetWindowLongW(hwnd, _GWL_STYLE) & _WS_CAPTION
        return False, (f"it stayed {got[2]}×{got[3]} instead of {w}×{h}"
                       + ("; the game looks fullscreen: set its display mode to Windowed" if borderless else ""))
    return True, f"{w}×{h} at {x},{y}"


def close(hwnd) -> None:
    if WINDOWS and hwnd:
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


# ------------------------------------------------------------------ single window (frame host)
def find_frame_host(*game_dirs) -> str | None:
    """Spire Mentor's frame host in a game folder's mods; None when it is not installed.

    The host (tfriedel/spire-mentor, MIT) is a plain window holding a web view. It makes the
    game window its child and puts it where the page says (``game_rect``), so the game and the
    panels are one window. The page it shows is ours; nothing of Mentor's own panels is loaded.
    """
    if not WINDOWS:
        return None
    for folder in game_dirs:
        if folder and (Path(folder) / FRAME_HOST).is_file():
            return str(Path(folder) / FRAME_HOST)
    return None


def open_frame(exe: str, url: str, game_dir, attach: tuple[int, int] | None = None) -> subprocess.Popen:
    """Start the frame host on ``url``.

    ``attach`` = (process id, window handle) of a running game: the host adopts that window and
    gives it back when it closes. Without it the host waits for a game started with
    ``--wid <host window>`` and, when its window is closed, asks that game to quit first.
    """
    args = [exe, "--url", url, "--game-dir", str(Path(game_dir).resolve())]   # the window takes the game's icon
    args += ["--attach", str(attach[0]), "--attach-hwnd", str(attach[1])] if attach else ["--no-launch"]
    return subprocess.Popen(args, cwd=str(Path(exe).parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def frame_window(pid: int):
    """The frame host's own window (not its web view's helper windows), or None while it starts."""
    for win in _windows():
        if win["pid"] == pid and win["title"] in (FRAME_HOST_TITLE, "STS Agent"):
            return win["hwnd"]
    return None


def embedded_game(frame_hwnd):
    """Handle of the game window living inside the frame host, or None."""
    found = []
    if not WINDOWS or not frame_hwnd:
        return None

    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, name, 64)
        if name.value == GAME_WINDOW_CLASS:
            found.append(hwnd)
            return False
        return True

    _user32.EnumChildWindows(frame_hwnd, _EnumProc(visit), 0)
    return found[0] if found else None


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def set_title(hwnd, title: str) -> None:
    """Name the window and ask for a dark title bar, to match the page inside it."""
    if not WINDOWS or not hwnd:
        return
    _user32.SetWindowTextW(hwnd, title)
    try:
        dark = ctypes.c_int(1)   # DWMWA_USE_IMMERSIVE_DARK_MODE
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except (OSError, AttributeError):
        pass


def minimize_console() -> None:
    """Tuck the launcher's own console away once the window is up. No effect inside a terminal app."""
    if not WINDOWS:
        return
    hwnd = _kernel32.GetConsoleWindow()
    if hwnd and _user32.IsWindowVisible(hwnd):
        _user32.ShowWindow(hwnd, _SW_MINIMIZE)


# ------------------------------------------------------------------ panel windows
def find_browser(configured: str | None = None) -> str | None:
    """A Chromium browser that can open chromeless ``--app`` windows: Edge first, then Chrome."""
    if configured:
        return configured if Path(configured).is_file() else None
    roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for relative in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            if root and (Path(root) / relative).is_file():
                return str(Path(root) / relative)
    return None


def open_panel(browser: str, url: str, rect: tuple[int, int, int, int], profile_dir: str) -> subprocess.Popen:
    """One chromeless window showing ``url``. The profile is private to the interface."""
    x, y, w, h = rect
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [browser, f"--app={url}", f"--user-data-dir={profile_dir}", f"--window-position={x},{y}",
         f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check", "--disable-sync",
         "--disable-features=Translate,msEdgeSidebarV2"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
