# Interface

`src/sts_agent/ui/` shows a running session and lets you pause, step, stop, and
start runs. It is one window: the game sits inside it and the panels are drawn
around the game by a local web page that the agent serves on localhost. It
observes and gates the session; it never chooses an action. The same
`run_llm_session` runs underneath as on the command line.

Status: the single window was run against the pinned game on 2026-11-20 —
starting the game inside it, adopting a game that was already open, the Full
screen button, closing, and continuing an open run with **Continue run**
(a delegated fight, the map plan and streamed reasoning were shown). New run,
`start-run` opening the window, Pause, Step, Stop, and pausing a real fight are
still source and synthetic checks only. The tiled fallback has not been run
against a live game. See [validation records](LOCAL_VALIDATION.md).

## Starting it

```powershell
.\start.cmd            # Windows
```

```sh
./start                # Git Bash / Linux / macOS
sts-agent ui           # the same, from an activated environment
```

`start run ...` (`sts-agent start-run [--character C] [--ascension N]`) opens
the same window and, once the game is up, begins a new run in it without a
button press, replacing the run that is open. `--console` keeps the earlier
flow with no interface. `play-run`, `play-combat` and `step` stay console
commands.

| Option | Effect |
| --- | --- |
| `--no-launch` | Do not start the game; attach to one that is already running. |
| `--no-browser` | Do not open any window; print the address of the single-page view. |
| `--port N` | Localhost port (default: a free one). |
| `--game-fraction F` | Share of the window's width given to the game (default 0.70). |

On start the interface opens the window and brings the game into it:

- **No game running:** the pinned game copy is launched inside the window (the
  same gate as `start-run`, see `game_pin.py`). The game never shows a window
  of its own.
- **A game already open:** its window is adopted into the interface window.
- **A game that opens its own window later** (started by hand, or still loading
  when the interface opened) is adopted after about four seconds.

Opened without `run`, it does not start playing: a run begins when **Continue
run** or **New run** is pressed.

Closing the window ends the interface. A game that was started inside the
window is asked to quit first; a game that was adopted gets its own window back
and keeps running. Ctrl+C in the console closes the window the same way. A
running session is asked to stop first, and Autobattle is switched off if the
session does not stop in time. The console minimises itself once the window is
up when it has a window of its own; inside a terminal application it stays
where it is.

## The single window

```text
+--------------------------------------------------------+
| control bar                                            |
+------------------------------------+-------------------+
|                                    |  map plan         |
|   game                             +-------------------+
|   (game_fraction of the width,     |  current step,    |
|    16:9)                           |  delay, status    |
+------------------------------------+-------------------+
| combat log                         |  thinking         |
+------------------------------------+-------------------+
```

The control bar spans the window. The game takes `game_fraction` of the width
(clamped to 0.4–0.9) at 16:9; its height is reduced only when the combat log
would otherwise get less than 150 px. The combat log takes what is left under
the game and the right column takes the remaining width. Resizing or maximising
the window resizes the game with it. **Full screen** makes the window
borderless and fills the screen; the same button leaves it.

The window is the frame host that ships with the
[Spire Mentor](https://github.com/tfriedel/spire-mentor) mod (MIT):
`mods/SpireMentor/SpireMentorFrame/SpireMentorFrame.exe`, looked for in the
pinned game copy and then in `game_dir`. The host is a plain window holding a
web view; it makes the game window its child and puts it on the rectangle the
page reports. The page is this project's `panels.html`; none of Mentor's own
panels are loaded, and Mentor is not told it is framed, so its in-game toolbar
stays in the game. The agent's mod is not involved and needs no rebuild.

Things that follow from using that host:

- It is Windows-only and needs the Edge WebView2 runtime (part of Windows 11).
- It keeps its own files under `%LOCALAPPDATA%\SpireMentor`: the remembered
  window size (`frame\window.json`), a log (`frame\frame.log`, which records
  the page address including that launch's token), and the web view profile
  (`frame-webview`).
- While the window is in front, the host captures F1 and F4–F10 for Mentor's
  shortcuts and forwards them to a link the agent does not set up, so those
  keys do nothing there. Mentor's in-game toolbar still works with the mouse.
- Mentor's own "panels in their own window" option must stay off: it starts a
  second host that takes the game window (source inspection, not tried).
- Verified with the game's display mode set to Windowed. A game set to
  Fullscreen has not been tried.

### Tiled fallback

Without the frame host, off Windows, or with `[ui].single_window = false`, the
earlier layout is used: the game window is moved to the top-left of the primary
monitor's work area (`game_fraction` of its width and height) and three
chromeless Edge or Chrome `--app` windows are tiled around it — control bar
above (at most 96 px), combat log below (at least 150 px), and one window on
the right holding map plan, current step and thinking. They use a private
browser profile under `data/ui_browser`. The game window is found by its
process (`SlayTheSpire2*.exe`) and moved with Win32 `SetWindowPos`, once when it
appears and once more about six seconds later; **Snap windows** places
everything again. The game must be in **Windowed** mode. Set
`[ui].place_game_window = false` to leave the game window alone. Without such a
browser, or off Windows, one ordinary browser tab shows every panel on a single
page.

## Panels

| Panel | Contents |
| --- | --- |
| Control bar | Continue run, New run (character and ascension), Pause/Resume, Step, Stop, Full screen (tiled: Snap windows), Traces; session state and a run summary (character, act, floor, HP, gold, screen). |
| Combat log | One card per turn: hand, enemy intents, cards played and what changed, the enemy's turn; a header card and a result card per fight. |
| Map plan | The act map as last seen, with the stored route plan highlighted, the current room ringed and visited rooms dimmed; the plan text, projected HP per remaining room, and the change-course conditions. |
| Current step | The phase the agent is in, time in this step, time in the model call in flight (or the last one), average call time, time since the last action; the availability bar and the last console line. |
| Thinking | The model's reasoning while it streams, then each decision with its stated reason and turn intent, and each strategy update with its standing plan. |

The availability bar has one segment per component:

| Component | Checked by |
| --- | --- |
| Game window | Every two seconds. Single window: whether the game window is inside it, or what it is waiting for; the same text is shown where the game will appear. Tiled: window lookup, and where the game was placed or why it was not. |
| Game mod | `GET /` on the mod every two seconds. |
| Game build | The pinned-build check, when the mod starts answering. |
| Model | Configuration at start (model, key, SDK); afterwards the result and latency of the last call. |
| Autobattle | The bridge's status command every fifteen seconds while no session runs; during a session, the coordinator's own status reads. Shown as off when `[autobattle].enabled = false`. |
| Expert evidence | The availability label of the evidence attached to the latest decision. |

Health checks never read the game state, because a state read can open a shop
inventory or a chest inside the mod.

## Controls

- **Continue run** plays the run that is open in the game (`play-run`). At the
  main menu it stops with `unsupported_decision_screen`; load the run first.
- **New run** abandons the open run after a confirmation, sets up a new one
  through Neow (`start-run`), then plays it.
- **Pause** takes effect at the next safe point. A model call in flight finishes,
  but its action is not sent while paused. On **Resume** the usual fresh-state
  validation runs, so a screen you changed by hand makes the pending choice
  stale instead of being sent. Paused time does not count as a game stall.
- During a delegated fight, Pause switches Autobattle off; Resume asks for the
  offer hold again and switches it back on if the fight is still open.
- **Step**, while paused, lets the agent finish one decision and holds again.
  Pressed inside a delegated fight, it runs that fight to its end and then one
  more decision.
- **Stop** ends the session with stop reason `interrupted` at the next safe
  point. It also ends an otherwise unbounded model retry loop.

No pause or stop point lies between sending an action and observing its
resolution. After a button is pressed in the single window, keyboard focus goes
back to the game.

## How the session reports

`hooks.py` defines `SessionHooks`, a no-op observer. The coordinator, the
model-call tracker, and the DeepSeek provider call it; `ui/hub.py` overrides it.
A hook that raises is reported once on the console and ignored, so a display
problem cannot stop a run.

| Module | Responsibility |
| --- | --- |
| `hooks.py` | The observer and pause-gate interface; does nothing by default. |
| `ui/hub.py` | Thread-safe live state, bounded history (800 log entries, 160 thinking entries), and the pause/step/stop switch. |
| `ui/combat_log.py` | Fight log rebuilt by comparing consecutive combat observations. |
| `ui/windows.py` | Finding and starting the frame host, its window and the game inside it; for the tiled fallback, layout arithmetic, game-window placement and panel windows. |
| `ui/server.py` | Localhost server: the page, a long-polled state feed, the commands. |
| `ui/app.py` | Opens the window, launches or adopts the game, runs health checks and the session thread, ends when the window closes. |
| `ui/panels.html` | The page: `panel=frame` is the single window's layout; the other values are the tiled panels and the single-page view. |

The page and the host talk through the web view's message channel: the page
sends `ready`, the game's rectangle in device pixels (`game_rect`, again
whenever the layout changes), `focus_game`, and `fullscreen`. The host needs
nothing else from the page.

The combat log is a reconstruction at the polling rate, not an event stream. The
mod reports states and a delegated fight is played inside the game, so two cards
played between two polls appear as one entry, and which card caused which effect
within that entry is unknown. Cards that leave the hand at the turn boundary are
counted as discarded, not played.

## Model reasoning

With `[ui].live_thinking = true` (default) and the DeepSeek provider, a session
started from the interface streams its model calls so `reasoning_content` can be
shown as it arrives. The answer text, usage, and stop reason are assembled from
the stream and validated exactly as a plain response. If the endpoint rejects a
streamed request (HTTP 400, 404, or 422), the model returns to plain calls for
the rest of the session and shows any returned reasoning in one piece.

Reasoning is displayed only. It is not part of the decision, not written to
traces, and not kept after the interface closes. Provider identity in the
manifest records `streamed: true` for such sessions. Command-line sessions and
the Anthropic provider do not stream; for those the Thinking panel shows each
decision's stated reason when the call returns.

## Access

The server listens on `127.0.0.1` only. Every request must carry the token
generated for that launch and a localhost `Host` header, so another page open in
a browser cannot read the state or press the buttons. The address printed at
start includes the token.

## Configuration

```toml
[ui]
single_window = true       # game and panels in one window; false: separate tiled windows
game_fraction = 0.70       # share of the window's width given to the game (tiled: of the screen)
port = 0                   # 0 picks a free port
# browser = '...'          # tiled layout only; default: Edge, then Chrome
live_thinking = true       # stream model reasoning into the Thinking panel (DeepSeek only)
place_game_window = true   # tiled layout only; false: leave the game window where it is
```

The browser path is removed from the manifest's recorded configuration.
