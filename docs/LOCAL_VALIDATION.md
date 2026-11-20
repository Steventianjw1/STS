# Validation records and current verification limits

## Single-window interface — 2026-11-20

The interface was changed to one window holding the game and the panels, using
the frame host from the installed Spire Mentor bundle (0.10.6). It was run
against the pinned game copy (v0.107.1, STS2MCP 0.4.0 answering) on a 1920×1200
display at 150% scaling. Except for the continued run noted below, no provider
call was made and no gameplay action was sent; the interface's health checks
sent `GET /` and the Autobattle status command. Results were read from screen
captures, the host's `frame.log`, and
process and window listings.

- **Live, game already open** (`sts-agent ui --no-launch`, game on a game-over
  screen): the host adopted the game window about 1.5 s after it started; the
  page drew the control bar across the window, the game at 16:9, the combat log
  below and the three panels on the right; the Game window check read "inside
  the STS Agent window". Closing the window returned the game window with its
  original style and bounds, and the interface process exited with code 0.
- **Live, no game running** (`sts-agent ui`): the game was started with
  `--wid`, appeared as a window owned by the host, was adopted about 2 s later,
  and reached the main menu inside the window; the mod answered within about
  8 s of the launch and the game window, mod, build, model and Autobattle checks
  were ok. Closing the window made the game quit and the interface exit with
  code 0; no game, host, or interface process was left.
- **Live, Full screen button** (two synthetic mouse clicks): the window became
  borderless and filled the screen, the game resized with the layout, and the
  second click restored the window.
- **Source inspection** (decompiled host and Mentor mod): the page-to-host
  messages, the host's F-key capture, and Mentor's own option that starts a
  second host. None of these were exercised beyond the messages listed above.

- **Live, Continue run on an adopted game:** a run that a console `start-run`
  session had begun was continued from the window (session
  `20261119T173643Z-34842f`). The open fight was delegated to Autobattle and
  appeared in the combat log, the stored route plan was drawn, and reasoning
  streamed into the Thinking panel. The console session could not be stopped
  with Ctrl+C from another process and was killed during a model call.
- **Synthetic, stubbed window, game launch and session:** `start-run` now opens
  the interface and then starts the new run with the given character and
  ascension, in that order, in both the single window and the tiled fallback;
  `ui` starts no run. `start`, `start.cmd`, `start run` and `start.cmd run`
  were checked with `--help` to reach `ui` and `start-run`. `start-run` was not
  run against the game, because a run was in progress.

Not verified: `start-run` opening the window and setting a run up live; New run
setup, Pause, Step and Stop from the window; adoption of a game
that opens its own window after the interface is up, a game set to Fullscreen,
the console minimising itself, a second monitor, and the tiled fallback after
this change.

## Interface and Autobattle-only change — 2026-11-14

The interface (`src/sts_agent/ui/`, `hooks.py`) was added and the Spire Mentor
integration was reduced to the native Autobattle bridge on top of `d73c7f3`. The
interface was **not launched**, the game was not started, moved, or sent any
action, and no provider call was made. Everything below is offline.

- **Regression, local ignored tests:** 166 passed and 12 failed. A clean export
  of `d73c7f3` with the same tests fails the same 12 (strategic planner, expert
  statistics, one observation and one decision-engine case), so they predate
  this change. `test_connection_refused_is_not_delivered` was left out: a
  refused local connection times out in the sandbox it ran in.
- **Synthetic, scripted model and scripted game client fed with recorded
  states:** an unpaused step sends one action and reports through the hooks; a
  paused session sends nothing; Step lets exactly one action through and leaves
  the pause set; Stop while paused ends as `interrupted` with nothing sent.
- **Synthetic, fake SDK client:** a streamed DeepSeek call assembles the answer,
  delivers reasoning and answer deltas, and records usage once; a rejected
  stream falls back to a plain call and stays plain; without a listener the
  request is unchanged.
- **Synthetic, local server:** requests without the token get 403; the state
  feed, the page, a command, and the long poll answer as designed.
- **Recorded live observations replayed offline:** the combat log rebuilt the
  three fights of session `20261114T024056Z-511e34` from its raw states.
- **Page:** rendered with replayed data in a browser with no script errors. The
  combined page was seen once; the three separate panel pages were checked
  through the DOM at their intended sizes, not by eye.
- **Read-only on the running machine:** the layout was computed from the real
  work area (1920×1128: game 1344×790 at 0,96) and the running game's window
  handle was found. The window was not moved.

Not verified: placing the game window, opening and placing the Edge panel
windows, launching the game from the interface, New run setup from the
interface, streaming against the real DeepSeek API, and pausing or resuming a
real delegated fight. Mentor's own in-game overlay may still appear during
delegated fights, because the bridge switches Mentor to its Coach style.

## Documentation review — 2026-11-12

The current implementation was reviewed through `35d636c` ("Fix live merchant
and rest handling and configure DeepSeek effort"), following the two merges of
enemy move forecasts and the route/elite/ancient evidence work. This section
describes source inspection and offline checks; it adds no live-play or real-model
validation claim.

- Reviewed the mod hooks, full deck/pile visibility, reference move forecasts,
  contextual statistics/history, route projections/replanning, elite gaps,
  ancient offer/map outlooks, provider setup, and trace/replay behavior.
- The latest implementation offers leave in a ready regular shop even when the
  room proceed button is disabled, withholds potion purchases when known slots
  are full, waits through rest states without enabled actions, and forwards
  optional DeepSeek reasoning effort (`low`, `high`, `max`). The commit title
  identifies live handling fixes; it is not a checked-in end-to-end run report.
- Offline CLI help/capabilities and syntax parsing of all 38 Python source/tool
  files passed during familiarization. Documentation checks passed for all
  eight Markdown guides, 32 local links/anchors, four structured code examples,
  example configuration/defaults, schema/prompt names, and six CLI help routes.
- The current checkout has no local test suite. Historical test counts below
  refer to earlier local, ignored tests, not a suite distributed with the clone.
- Every capability in code still reports `live_validated=false`. No game/mod
  download, build, launch, gameplay POST, external evidence fetch, or billable
  provider call was performed for this review.

The records below are preserved as historical evidence from the earlier local
Windows environment. Paths, installed builds, dependency availability, and API
keys described there do not establish the state of the current cloud workspace.
In particular, successful older preview builds do not validate later deck,
enemy-move, and ancient-map hooks in a currently installed DLL. The repository
authorizes bounded live verification in `AGENTS.md`; historical statements about
pending validation describe what was done then, not an offline-only restriction.

## Historical local checks — 2026-11-03

### Native combat preview extension

Added the repository-owned `mod/McpMod.AgentPreview.cs` extension and the
idempotent `tools/patch_mod_preview.py` integration script. Built against the
installed v0.107.1 game: **zero warnings, zero errors**. The ignored output is
`.venv/mod-build/out-preview/STS2_MCP.dll`, SHA-256
`522E94E37254B70A8730642BE9F15B7C1BB16961429C16FA13C6C93D24AF3034`.

Source inspection of the installed game confirms that `UpdateDynamicVarPreview`
uses native damage/block hooks and target-specific status/relic rules. The
extension updates cloned variables using the real card identity. This is source
inspection and a successful build, not live validation of the extension.

Focused synthetic observation/decision checks: **21 passed**. Two added local
regression tests cover the exact serialized LLM message, target-specific damage,
block, visible statuses, missing previews, unknown later intents, and changed
state invalidation. The two tests also passed after extending coverage for AoE,
multi-hit, and unparsed descriptions. Tests and fixtures remain local and ignored.
One existing setup test was excluded because the configured local `.env` makes
its missing-provider premise false. Pytest's temp directory was placed under
the ignored tests directory to avoid sandbox temp-directory permissions.

Installation/live verification is pending: the running game holds the old DLL
open, and the user requested waiting for the other chat's active run to finish
before restarting. Its installed DLL remains unchanged. An ignored backup is
`.venv/mod-build/STS2_MCP.before-preview.dll`.

See `COMBAT_VISIBILITY.md` for the preview data contract and limitations.

The user now authorizes game/mod access. No live gameplay action or billable LLM
call has been performed during these checks.

### Historical v0.107.1 recheck — startup and API reads verified

The installed game changed to **v0.107.1**, commit `59260271`, main assembly hash
`-1555940892`. Steam now reports build **23811903**, `public` branch.

Upstream `main` still points to `55e064850a68f3b4cde7e5fd525bf9b2dec4e885`.
Rebuilt that source against the currently installed game with .NET SDK 9.0.320:
**build succeeded, zero warnings, zero errors**. Output is local and ignored:
`.venv/mod-build/out-v01071/STS2_MCP.dll`.

Installed the rebuilt DLL and its manifest into the game's `mods` directory and
launched via Steam. The game log confirms successful mod initialization and the
server listening on `http://localhost:15526/`. Read-only checks passed:

- `GET /`: `{"message":"Hello from STS2 MCP v0.4.0","status":"ok"}`.
- `GET /api/v1/singleplayer`: readable `menu` state, parsed by the agent.
- `GET /api/v1/compendium`: readable JSON.

This establishes compilation, startup, and API-read compatibility with v0.107.1;
combat and other gameplay actions remain unvalidated. No gameplay POSTs or LLM
calls were made. The agent does not automate the menu/run-start screen.

Use this rebuilt source version, rather than assuming the older downloadable
0.4.0 DLL is equivalent: the version label was not bumped for the compatibility
fix. No alternate mod or further game-version change is needed for these verified
startup/read checks.

### Earlier v0.111.0 check

#### Installed game and mod in that check

- Steam installation: `C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2`.
- `release_info.json`: **v0.111.0**, commit `41cef1ea`, main assembly hash `222455745`.
- Steam manifest: build **24724944**, `public-beta` branch.
- No game `mods` directory or STS2MCP artifact was found in the checked game and
  Workshop locations.

Downloaded the exact STS2MCP revision used by the agent,
`55e064850a68f3b4cde7e5fd525bf9b2dec4e885`, into the ignored `.venv/mod-build/`
directory. Built with workspace-local .NET SDK 9.0.320 against the installed game
assemblies. **Build failed with four CS1061 errors** in `McpMod.StateBuilder.cs`:

- line 783: `StartRunLobby.MaxPlayers` does not exist.
- lines 973, 982, 1000: `LoadRunLobby.ConnectedPlayerIds` does not exist.

This revision is not source-compatible with the installed v0.111.0 game.
No mod was installed and no game was launched. Compilation alone, even if fixed,
would not establish runtime compatibility.

Next step: obtain or port an STS2MCP build for v0.111.0, then compare its API schema
with this adapter and perform live read-only checks before a bounded action.
Alternatively, use a game build explicitly supported by the chosen mod revision.
No downgrade or replacement mod is currently verified by this project.

### Historical DeepSeek setup and checks

`deepseek` is the default provider. `.env` and `.env.example` contain blank
`DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and `DEEPSEEK_MODEL` values.
The API root defaults to `https://api.deepseek.com` when blank; the API key and
model must be supplied. No OpenAI account/key is used. The OpenAI-compatible
SDK is only a client library, as documented by
[DeepSeek](https://api-docs.deepseek.com/).

The adapter shares the decision/strategy budget, disables SDK retries, requests
JSON, records usage, rejects truncation/refusal/tool output, and excludes
`reasoning_content`. Error messages omit SDK exception bodies. The normal
decision validation and fresh-state checks remain in place.

Verification: 17 focused decision-engine and strategic-planner tests passed,
including DeepSeek response failures/budgets, provider selection, secret rejection
in TOML, .env precedence and the real SDK constructor (without a network call).
No API key was available; a live DeepSeek call is pending user configuration.
