# STS Agent

An LLM-driven agent for **Slay the Spire 2**, using the
[STS2MCP](https://github.com/Gennadiyev/STS2MCP) mod's localhost HTTP API.
Development runs have used single-player Ironclad, most recently at Ascension
10. `start-run` accepts any of the five characters and Ascension 0–10; `doctor`
warns that a character other than Ironclad is untried. The agent continues an
already-started run across supported screens;
`start-run` and the interface's **New run** can also set up a new run through
Neow. It does not manage profiles.

`start.cmd` (or `./start`) opens the [interface](docs/INTERFACE.md): one window
holding the game, a control bar, a combat log, the map plan, the current step
with an availability bar, and the model's thinking.

The configured model chooses each gameplay action. Python supplies allowlisted
facts and supported candidates, validates the choice against a fresh state,
sends the action once, observes resolution, and records the result. The default
policy is `llm` and the default provider is `deepseek`. Anthropic is also supported.
`--policy baseline` explicitly selects a combat-only heuristic; it is never a
fallback for an LLM failure.

## Current capabilities

- Combat cards, targets, potions, end turn, and in-combat card selections.
- Rewards, maps, events, rest sites, shops, card grids, relic choices, and treasure.
- Full permanent-deck facts outside combat and full draw/discard/exhaust pile
  contents during combat, grouped with counts and without hidden draw order.
- Optional game-native, target-specific damage/block previews and static enemy
  move-pattern forecasts for the two enemy turns after the observed next move.
- A shared-provider strategic assessor with seven dimensions, a maintained
  standing plan for the deck, and deck-aware refreshes.
- Per-screen instructions, the observed map ahead of each room, the game's own
  keyword tooltips, and two optional what-if calculations.
- Spire Codex expert rates, floor/deck-conditioned history, route HP-risk and path
  projections, elite-readiness analysis, and ancient-offer evidence using the act map.
- Optional prompt-size, model-call, and token limits (none by default), JSONL
  traces, and offline validation of recorded decisions.
- Same-run memory saved between sessions, so a restarted session picks up the
  route plan and fight outcomes of the run it left.
- Optional native Autobattle: the Spire Mentor mod plays whole fights while the
  model makes every non-combat choice. Nothing else of Mentor is read.
- A local interface to watch, pause, step, stop, and start runs.

`bundle_select`, `crystal_sphere`, `fake_merchant`, menus, and unrecognized
popups remain unsupported. A regular merchant is supported; the fake merchant
is a separate event. Run `sts-agent capabilities` for the source-derived table.

Support is not a claim of verified play strength or compatibility with every
build. Capability flags remain `live_validated=false`. Historical records include
v0.107.1 mod compilation, startup, and API reads; the latest code includes fixes
for live merchant/rest behavior. See [compatibility](docs/COMPATIBILITY.md) and
[validation records](docs/LOCAL_VALIDATION.md) for the evidence and its limits.

## Install

Python 3.11+ is required. The core and offline components use the standard
library; each real provider needs its optional SDK extra.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[deepseek]"
Copy-Item sts_agent.example.toml sts_agent.toml
Copy-Item .env.example .env
```

On Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[deepseek]'
cp sts_agent.example.toml sts_agent.toml
cp .env.example .env
```

Activate the environment or invoke its `sts-agent` executable directly. A
portable Windows environment may instead have `.venv\python.exe`; use the path
that exists locally. For Anthropic install `.[llm]`; `.[dev]` adds pytest.
Tests and fixtures are local and Git-ignored, so a clean clone need not contain them.

## Configure the provider

Fill in the ignored `.env` locally:

```dotenv
DEEPSEEK_API_KEY=<your key>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=<model available on your account>
```

A blank base URL uses the official endpoint. A configured URL must be an HTTPS
API root without embedded credentials, query, or fragment. There is no assumed
model ID. The OpenAI-compatible SDK is only the DeepSeek transport; it does not
use an OpenAI account or key.

By default no output cap is sent to DeepSeek, so the provider's own limit
applies. Anthropic requires a cap and uses 16384 when none is set. DeepSeek
reasoning effort defaults to `medium`, which is sent as `high`:

```toml
[llm]
provider = "deepseek"
# max_output_tokens = 16384             # optional cap
deepseek_reasoning_effort = "medium"    # low, medium (sent as high), high, or max
```

Supported effort depends on the selected model/API. This option is sent only to
DeepSeek and is recorded in provider identity when set. For Anthropic, use
`provider = "anthropic"`, an explicit `[llm].model`, and `ANTHROPIC_API_KEY` in
the environment or `.env`.

Precedence is code defaults < TOML < command-line flags. For DeepSeek,
`--model` overrides `[llm].model`, which overrides `DEEPSEEK_MODEL`. Existing
environment variables take precedence over `.env`. With `--config`, `.env` is
loaded from that config file's directory; otherwise it is loaded from the working
directory. The loader supports plain/quoted values and full-line comments,
without shell expansion. Credentials belong outside TOML; key-like fields are rejected.

`sts-agent doctor` checks local provider setup and game connectivity without a
model call. Missing model, key, or SDK stops gameplay commands without a baseline
fallback. No call or token budget applies unless one is set under `[llm]`
(`max_calls_per_run`, `max_input_tokens_per_run`, `max_output_tokens_per_run`);
token prices there only add a cost estimate. Decision calls, strategic calls,
corrective and provider retries, and what-if tool calls are counted together
against any such budget.

## Install the mod and optional data

The source-inspected STS2MCP revision is
`55e064850a68f3b4cde7e5fd525bf9b2dec4e885`. The tagged 0.4.0 release predates
that revision's STS2 v0.107 compatibility fix, although both report mod version
0.4.0. Do not infer compatibility from that label alone.

For the recorded v0.107.1 setup, build the pinned source with .NET 9 against the
installed game. Apply this repository's additive extension before building to
include permanent deck, native card previews, enemy move IDs/history, and the
ancient-screen map:

```powershell
git clone https://github.com/Gennadiyev/STS2MCP.git .venv/mod-build/STS2MCP
git -C .venv/mod-build/STS2MCP checkout 55e064850a68f3b4cde7e5fd525bf9b2dec4e885
python tools/patch_mod_preview.py .venv/mod-build/STS2MCP
dotnet build .venv/mod-build/STS2MCP/STS2_MCP.csproj `
  '-p:STS2GameDir=C:/Program Files (x86)/Steam/steamapps/common/Slay the Spire 2' `
  -o .venv/mod-build/out-preview
```

With the game closed, back up the old DLL, copy the rebuilt `STS2_MCP.dll` and
upstream `mod_manifest.json` (renamed `STS2_MCP.json`) into the game's `mods`
directory, then enable mods and reopen the game. See
[the extension guide](docs/COMBAT_VISIBILITY.md). Rebuild and verify after game
updates; the recorded v0.111.0 attempt failed to compile this upstream revision.
Declare the actual `game_build` in TOML, and optionally `game_dir` for manifest checks.

Fetch the optional pinned v0.107.1 enemy reference from the repository root:

```sh
python tools/fetch_monster_data.py
```

It writes ignored `data/monster_reference/monsters.json`, selected by
`monster_data_path`. Without readable reference data, later enemy turns stay
unknown. These are static predictions, not game simulation.

Spire Codex support is enabled by default. Its optional `SPIRE_CODEX_API_KEY`
belongs in `.env`. The cache and SQLite history are under ignored
`traces/spire_codex_cache`; use `sync-history` to fill a bounded corpus before
play if desired. See [expert evidence](docs/EXPERT_STATS.md) for scopes and limits.

## Autobattle (optional)

```toml
[autobattle]
enabled = true          # Spire Mentor's Autobattle plays whole fights
offer_handoff = true    # the model decides in-fight offers an enemy opens
```

This needs the [Spire Mentor](https://github.com/tfriedel/spire-mentor) mod
installed beside `STS2_MCP` and the rebuilt agent mod extension
(`mod/McpMod.AgentStartup.cs`, installed by `tools/patch_mod_preview.py`), which
exposes Mentor's Autobattle to the agent on the game thread. With it enabled the
model never plays combat and there is no LLM combat fallback: a fight Mentor
stops playing ends the session with `autobattle_failed`. With it disabled the
model plays the fights. The agent sends Autobattle commands only; it does not
read Mentor's advice, logs, or card and path suggestions. The earlier per-action
combat search through Mentor's advisor (`[combat_search]`) has been removed, and
a configuration that still has that table is rejected as an unknown key.

## Interface

`start.cmd`, `./start`, or `sts-agent ui` opens one window with the game inside
it and the panels around it. When no game is running, the pinned game copy is
launched inside that window; a game that is already open is adopted into it.
Closing the window ends the interface, and closes a game it started. The window
is the frame host that ships with the Spire Mentor mod; without it, or with
`[ui].single_window = false`, the game window is moved to the top-left and
separate panel windows are tiled around it. Runs are started from the control
bar. Options live under `[ui]`; see
[the interface guide](docs/INTERFACE.md). `start run ...` (`sts-agent start-run`)
opens the same window and begins a new run in it; add `--console` to play
without the interface.

## Commands

Global options (`--config`, `--base-url`, `--policy`, `--model`) precede the command.
The mod normally listens at `http://127.0.0.1:15526`.

| Command | Behavior |
| --- | --- |
| `sts-agent doctor` | Check config, provider setup, connectivity, mod build, and current screen; no model call. |
| `sts-agent observe [--json] [--save F] [--save-raw F]` | Read state; optionally save normalized/raw data. Plain text can show the baseline comparison choice. |
| `sts-agent observe --save-frame traces/frame.json` | Save a supported decision frame, with advisory evidence when available; no model call. |
| `sts-agent observe --preview-decision` | Ask the configured model for a choice; billable, no gameplay POST. |
| `sts-agent decide-frame traces/frame.json [--scripted-reply TEXT]` | Decide from a saved frame; configured model is billable, scripted replies are synthetic; no gameplay execution. |
| `sts-agent step` | Make and execute one LLM gameplay decision. A strategic refresh may add model calls. |
| `sts-agent play-combat` | Continue until the current combat ends or a stop condition occurs. |
| `sts-agent play-run` | Continue an already-started run across supported screens. |
| `sts-agent start-run [--character C] [--ascension N] [--console]` | Replace the current run: open the interface, launch the pinned game copy inside it if needed, set up a new run through Neow, then play it. Prompts for missing values. `--console` plays without the interface. |
| `sts-agent ui [--no-launch] [--no-browser] [--port N] [--game-fraction F]` | Open the interface. Sends nothing to the game until a run is started from its control bar. |
| `sts-agent sync-history --character IRONCLAD --ascension 0 --max-new-runs 100 --max-seconds 120` | Fetch bounded public run history; no game or model calls. Match the scope to your configured evidence filters. |
| `sts-agent replay traces/SESSION_ID [-v]` | Validate recorded LLM decisions or rerun baseline choices offline. |
| `sts-agent capabilities` | Print supported actions, assumptions, and unsupported screens. |

`play-combat`, `play-run`, and `start-run` accept `--max-actions` and
`--max-no-progress-s`. For `play-run` and `start-run` the action limit sets
`max_run_actions`. No action limit applies unless one is given, and there is no
elapsed-time limit. Gameplay commands also accept `--trace-dir` and `--no-raw`.

Only `step`, `play-combat`, `play-run`, `start-run`, and a run started from the
interface send gameplay POSTs. With Autobattle enabled, sessions and the
interface's status check also send Autobattle control commands, which play no
card by themselves. State reads can
open shop inventory or treasure chests inside the mod. Advisory evidence may
make external requests (including a read-only draft-advice POST); saving a frame
therefore need not be network-free. `decide-frame` and replay use stored evidence.

Start verification with `doctor`, then `observe --save-frame traces/frame.json`,
then a bounded `step` or `play-combat`. Review the frame and resulting trace.
This documentation update does not itself establish live compatibility.

## Decision flow and memory

```text
observe and settle -> refresh strategy when needed -> attach advisory evidence
 -> build frame -> model selects one action ID -> validate JSON
 -> re-read and check fingerprint + binding -> POST once
 -> observe screen-specific resolution -> record outcome -> repeat
```

The model chooses confirmations, rewards, end turn, and leaving as well as card
plays. One case needs no model call: when a ready screen has exactly one way
forward (a single map destination that is not a campfire, a lone proceed or
confirm, end turn with nothing playable), the coordinator sends it and records
an `automatic_decision`. Travelling to a campfire also names the campfire option
(`rest_option`), which is carried out on arrival without a second call.

Invalid decision output is corrected and transient provider failures are
retried, both without limit unless `[llm].corrective_retries` or
`[llm].provider_retries` is set; other provider failures stop. The strategic
assessor keeps its single corrective retry. The model may spend one extra call per decision on
a what-if tool (`[llm].what_if_tools`). An ambiguous gameplay request is inspected and never resent.
Rejected candidates are withheld for that exact state.

With `[autobattle].enabled`, a fight is handed to Spire Mentor's Autobattle as a
whole and the loop above resumes on the first screen after it; with
`offer_handoff`, an in-fight offer that an enemy opens comes back to the model
for that one choice. A session started from the interface can be paused or
stopped at safe points of this loop, never between sending an action and
observing its resolution.

The seven-dimension strategic assessment and its standing plan (deck needs, what
to avoid, removal and upgrade targets, a skip rule, boss preparation) persist for
the session and are shown on every decision. Encounter
memory keeps up to eight decisions/outcomes and a conditional turn intent.
Recent combat summaries supplied to the assessor are capped at ten.
An optional map-only `route_plan` persists across encounters and is revisited
when resources, route position, fights, or elite preparation warrant it. Python
provides projections and triggers; the model still chooses the route.

Run identity uses the mod's compendium ID when available, otherwise a
session-scoped identifier. With a compendium ID, the route plan, fight outcomes,
map memory, and the open encounter's history are saved under
`traces/run_state/` and restored by a later session of the same run. The
strategic assessment is not saved; it is rebuilt. Session-scoped runs save
nothing, and there is no cross-run learning.

No local prompt-size cap applies by default; the provider's context limit still
does. With `[llm].max_input_bytes` set, decision prompts shrink advisory detail,
community deck advice, oldest history, and finally all advisory support, in that
order. Facts and legal candidates are never removed to fit; if those still
exceed the cap, the session stops.

## Stops and exit codes

Unsupported screens, missing actions, configured budgets and action limits,
stale states, unresolved actions, and stalls are explicit outcomes. A
regular shop offers leave once its inventory is ready even if its room-level
`can_proceed` is false; full potion slots suppress potion purchases. A rest site
with no enabled options and no proceed action is treated as not ready.

| Stop reason | Meaning |
| --- | --- |
| `step_done`, `combat_ended` | Expected bounded completion. |
| `player_defeated` | Game-over state with no remaining HP (or unknown HP). |
| `run_over_unconfirmed` | Game-over state with positive HP; the API does not certify victory. |
| `unsupported_decision_screen` | No supported adapter; nothing is chosen for that screen. |
| `model_failed`, `model_budget_exhausted`, `invalid_model_output` | No valid model choice; no gameplay action sent for it. The last two need a configured budget or `corrective_retries` limit. |
| `decision_input_too_large` | Essential facts/candidates exceed a configured `max_input_bytes`. |
| `no_actions_on_actionable_screen` | A ready screen has no supported candidates. |
| `adapter_index_mismatch` | A purchase changed a different shop entry than expected. |
| `unresolved_action`, `ambiguous_outcome_unresolved` | Outcome not confirmed; request is never resent. |
| `repeated_rejections`, `ready_timeout`, `no_progress`, `max_actions`, `game_unavailable`, `interrupted` | Execution or configured bounds stopped the session. `interrupted` is also the interface's **Stop**. |
| `not_in_combat` | `play-combat` was started outside a fight. |
| `autobattle_failed` | The Autobattle bridge was unavailable, Mentor stopped playing a delegated fight, or a held offer could not be answered. No LLM combat fallback. |

Exit `0` means expected completion for the command (`step_done`/`combat_ended`
for step, `combat_ended` for play-combat, `run_over_unconfirmed` for play-run
and start-run). Other session stops return `1`; setup/connectivity errors
generally return `2`; a cancelled `start-run` prompt returns `130`. `ui`, and
`start-run` without `--console`, return `0` when the window is closed, whatever
the session's stop reason; that is shown in the window and in the trace.

## Project map and references

| Location | Responsibility |
| --- | --- |
| `src/sts_agent/client.py`, `observation.py` | Game transport and normalized facts. |
| `src/sts_agent/decision/` | Screens, frames, prompts, choice validation, and run coordination. |
| `src/sts_agent/providers/` | DeepSeek and Anthropic transport with shared usage budgets. |
| `src/sts_agent/strategic/` | Evidence-grounded run assessment. |
| `src/sts_agent/expert_stats.py`, `spire_history.py` | External evidence, cache, and local run index. |
| `src/sts_agent/route_*.py`, `elite_readiness.py`, `ancient_advisor.py` | Route projections and rule-based advisory analysis. |
| `src/sts_agent/enemy_patterns.py`, `combat_preview.py` | Static forecasts and native-preview summaries. |
| `src/sts_agent/trace.py`, `replay.py` | Recording and offline validation. |
| `src/sts_agent/actions.py`, `cards.py`, `policy.py`, `runner.py` | Explicit combat-only baseline comparison. |
| `src/sts_agent/autobattle.py` | Commands to Spire Mentor's native Autobattle through the mod bridge. |
| `src/sts_agent/startup.py`, `game_pin.py` | Deterministic new-run setup; the pinned game build and its frozen copy. |
| `src/sts_agent/hooks.py`, `src/sts_agent/ui/` | Session observer interface; the single window and its panels, their server, and the combat log. |
| `mod/`, `tools/` | Additive mod extension (previews, deck, run setup, Autobattle bridge), patcher, and reference downloader. |

- [Interface](docs/INTERFACE.md)
- [Decision engine](docs/LLM_DECISION_ENGINE.md)
- [Strategic planner contracts](docs/STRATEGIC_PLANNER.md)
- [Expert statistics and route advice](docs/EXPERT_STATS.md)
- [Combat visibility and mod setup](docs/COMBAT_VISIBILITY.md)
- [Compatibility and screen coverage](docs/COMPATIBILITY.md)
- [Trace format and replay](docs/TRACE_FORMAT.md)
- [Historical and current validation records](docs/LOCAL_VALIDATION.md)
