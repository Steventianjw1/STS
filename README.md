# STS Agent

DeepSeek support is available through `src/sts_agent/providers/deepseek.py`, using
[DeepSeek Chat Completions](https://api-docs.deepseek.com/).
Install with `python -m pip install -e ".[deepseek]"` and fill in the ignored `.env`:
`DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` (API root, e.g. `https://api.deepseek.com`),
and `DEEPSEEK_MODEL` (e.g. `deepseek-flash`; check availability on your account).
All three values are initially blank. An empty base URL uses the official endpoint.
The default provider is `deepseek`; `[llm] model` or `--model` overrides the .env model.
Existing environment variables take precedence over .env; this file supports plain
or quoted values and full-line comments, without shell expansion.
The compatible OpenAI SDK is used only as a transport library: no OpenAI key or
account is used. SDK retries are disabled; decision validation and the shared
call/token budget also apply to DeepSeek. Anthropic remains available via
`provider = "anthropic"` and the `llm` install extra.

For the local portable Python setup, use `.\.venv\python.exe` and
`.\.venv\Scripts\sts-agent.exe` directly. Start with `doctor`, then
`observe --save-frame traces/live-frame.json` before a bounded `step`.
Live testing is now authorized; historical validation statements below describe
the original reconstruction, not a guarantee of compatibility with a new build.

An LLM-driven agent for Slay the Spire 2 (single-player; developed for Ironclad at Ascension 0). It
is built on the [STS2MCP](https://github.com/Gennadiyev/STS2MCP) mod's localhost HTTP API.

**By default, the configured LLM chooses every gameplay action** on the screens the adapter supports:

- combat card plays, targets, potions and end turn
- in-combat card selections
- rewards and card rewards
- the map
- events
- rest sites
- shops
- deck card-selection screens
- relic choices and treasure rooms

Python observes the game, offers only legal and verified actions, validates the model's choice
against a fresh observation, executes it once, confirms the result, and records everything. The old
rule-based heuristic is kept only as an explicit comparison mode (`--policy baseline`), for combat
only. It is never a fallback.

> **What is implemented, what is checked, and what is pending**
>
> | | Status |
> |---|---|
> | **Decision engine** | Implemented. Covers frames, screen adapters, LLM selection with one corrective retry, the run coordinator (`play-run`), strategic-planner integration, and traces with recorded-decision validation. |
> | **Claude provider adapter** | Implemented (`anthropic` SDK, optional extra). It has been checked only against an injected fake SDK client. **No real model call has been made.** |
> | **Screen adapters** | Built from the STS2MCP source at commit `55e0648` (*source-inspected*). Three screens are unsupported: bundle selection, Crystal Sphere and the fake merchant. The agent stops on them before sending anything. Run `sts-agent capabilities` for the table. |
> | **Checks** | 62 local tests (48 original, 5 planner, 9 decision engine), all using **synthetic** states and **scripted** model replies. They verify the harness, not play strength or real-game compatibility. |
> | **Live game** | **Not validated.** The game and mod cannot be run on the development device. Every screen is `live_validated = false`. |

```
src/sts_agent/
  client.py            game HTTP client (stdlib urllib): timeouts, ambiguous vs undelivered
  observation.py       allowlisted combat/run observation (null = unknown)
  decision/
    screens.py         screen adapters + capability table: raw state -> facts + candidate actions
    models.py          DecisionFrame (frame_id over the whole context) and the choice contract
    prompts.py         versioned decision instruction + size-bounded user message
    policy.py          LLM selection: one validated action id per call, <= 1 corrective retry
    coordinator.py     run loop: ready -> strategy refresh -> decide -> fresh validation -> send once -> resolve
    offline.py         SYNTHETIC test doubles: scripted game client and scripted text model
  providers/anthropic.py   Claude adapter for TextModel.complete(system=, user=)
  strategic/           strategic assessor (seven dimensions), refreshed by the coordinator
  trace.py, replay.py  JSONL traces (LLM: sts2-trace-2) and offline replay/validation
  actions.py, cards.py, policy.py, runner.py   baseline comparison mode (combat only)
  cli.py               commands
```

The core runs on the standard library alone. The real provider needs the optional `anthropic`
package.

---

## 1. Install (Windows, PowerShell)

You need Python 3.11 or newer.

```powershell
cd "D:\Users\Documents\ChatGPT\STS Agent"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[llm,dev]"           # sts-agent command + Claude SDK + pytest
copy sts_agent.example.toml sts_agent.toml
```

## 2. Configure Claude

1. In `sts_agent.toml`, under `[llm]`, set `model` to a current model ID from the
   [Claude model documentation](https://platform.claude.com/docs/en/about-claude/models/overview).
   There is no built-in default model.
2. Put your API key **only** in the environment. Never put it in TOML; the loader rejects key-like
   fields.
   ```powershell
   $env:ANTHROPIC_API_KEY = "<your key>"
   ```
3. Optionally, set limits for each run: `max_calls_per_run` and the token caps. If you also set
   prices per million tokens, the run summary includes an estimated cost.
4. Run `sts-agent doctor`. It checks the model ID, the SDK and the key, and makes **no** model calls.

If the model, SDK or key is missing, `step`, `play-combat` and `play-run` exit with an error before
contacting the game. They never fall back to the baseline.

## 3. Install the STS2MCP mod (on a machine that can run the game)

Check your game build on the main menu, and put it in `sts_agent.toml` as `game_build`. The mod's
latest release, **0.4.0, predates its STS2 v0.107 fix** (commit `55e0648`, which this adapter was
written against).

- **Game older than v0.107:** copy `STS2_MCP.dll` and `STS2_MCP.json` from
  <https://github.com/Gennadiyev/STS2MCP/releases/tag/0.4.0> into `<game install>\mods\`.
- **Game v0.107 or newer:** build from source with the .NET 9 SDK:
  ```powershell
  git clone https://github.com/Gennadiyev/STS2MCP.git; cd STS2MCP
  git checkout 55e064850a68f3b4cde7e5fd525bf9b2dec4e885
  .\build.ps1 -GameDir "<game install>"
  copy out\STS2_MCP\STS2_MCP.dll "<game install>\mods\STS2_MCP.dll"
  copy mod_manifest.json         "<game install>\mods\STS2_MCP.json"
  ```

Then enable mods in the game's settings and check the mod is running:
`Invoke-RestMethod http://127.0.0.1:15526/`.

English game text is recommended. The LLM can read other languages, but the baseline's parser and
some diagnostics assume English.

## 4. Commands

Nothing is sent to the game unless you run `step`, `play-combat` or `play-run`.

| Command | What it does |
|---|---|
| `sts-agent doctor` | Checks the config, connectivity, mod build, current screen, model ID, SDK and API key. Read-only; makes no model calls. |
| `sts-agent observe [--save F] [--save-raw F] [--json]` | Prints the current state, read-only. Also shows the baseline's choice, labeled as comparison mode. |
| `sts-agent observe --save-frame frame.json` | Saves the LLM decision frame for the current screen. No model call. |
| `sts-agent observe --preview-decision` | Asks the configured model what it *would* choose. **Billable.** Never executes. |
| `sts-agent decide-frame frame.json [--scripted-reply TEXT]` | Runs a decision offline on a saved frame. It uses the configured model (billable), or a literal scripted reply (synthetic). Never executes. |
| `sts-agent step` | Makes **one** LLM decision on the current supported screen, executes it once, and records it. |
| `sts-agent play-combat` | The LLM plays until the current fight ends. |
| `sts-agent play-run` | The LLM continues an **already started** run across supported screens. It stops on defeat, the end of the run, an unsupported screen, a limit, or an unresolved action. It never starts runs, changes difficulty or manages profiles. |
| `sts-agent replay PATH [-v]` | For LLM traces, validates the recorded decisions without calling a model. For baseline traces, re-runs the deterministic policy. |
| `sts-agent capabilities` | Prints the screen capability table. |

Global options: `--config`, `--base-url`, `--policy llm|baseline`, `--model ID`.
`play-combat` and `play-run` also take `--max-actions`, `--max-elapsed-s` and `--max-no-progress-s`.

Exit codes: `0` means the expected end (`step_done`, `combat_ended`, or `run_over_unconfirmed` for
`play-run`). `1` means it stopped for another reason. `2` means a setup error or the game could not
be reached.

### Stop reasons

| stop_reason | meaning |
|---|---|
| `combat_ended` | The fight is over (`play-combat`). |
| `player_defeated` | The game-over screen appeared with HP 0. |
| `run_over_unconfirmed` | The game-over screen appeared with HP above 0. Victory is not confirmed by the API. |
| `unsupported_decision_screen` | The screen has no verified adapter, such as a bundle choice, Crystal Sphere, fake merchant, menu or popup. `detail` names the missing capability. Nothing was sent. |
| `model_failed`, `model_budget_exhausted` | The provider call failed, or the run budget is used up. Nothing was sent. |
| `invalid_model_output` | The model's reply was still invalid after one corrective retry. Nothing was sent. |
| `decision_input_too_large` | The facts and candidates don't fit the prompt limit, even with no history. Nothing is trimmed silently. |
| `no_actions_on_actionable_screen` | A ready screen offered no supported actions. This is a diagnostic; there is no automatic leave or end turn. |
| `adapter_index_mismatch` | After a purchase, a different shop item changed. The shop index alignment assumption failed. |
| `unresolved_action`, `ambiguous_outcome_unresolved` | An action was not confirmed. The agent never resends. |
| `repeated_rejections`, `ready_timeout`, `no_progress`, `max_actions`, `max_elapsed`, `game_unavailable`, `interrupted` | As in the baseline runner. |

## 5. How an LLM decision is made and executed

```
observe -> wait until a supported screen is ready AND two consecutive reads are identical
        -> combat finished? record observed outcome (HP change, turns, potions used) to recent_outcomes
        -> refresh strategic assessment if strategic facts changed (never mid-combat for card plays)
        -> DecisionFrame: allowlisted facts + candidates (ids only; payloads stay private)
           + adapter limitations + labeled assessment + previous turn intent + <= 8 recent outcomes
        -> model returns {"frame_id","action_id","reason","turn_intent"}; <= 1 corrective retry
        -> write `llm_decision` record (input + choice) BEFORE sending
        -> re-read the game: the screen fingerprint and that action's binding must be unchanged,
           and the session deadline not passed
        -> POST once; rejected -> that action is withheld for this exact state; timeout -> inspect, never resend
        -> wait for screen-specific resolution (end_turn: next round; shop: stock check;
           card grid toggles may be unobservable) -> write `outcome` record -> repeat
```

The model chooses every gameplay action, including end turn, taking gold, single-option choices and
confirmations. Code only waits and observes. Two caveats: the mod itself opens shop inventories and
treasure chests when their state is read, and the agent doesn't manage menus or popups.

**Memory:**
- The strategic assessment persists for the whole run.
- The last turn intent and up to 8 recent decisions and outcomes are kept per encounter, and cleared
  when the encounter changes.
- There is no cross-run learning.
- The run ID comes from the mod's compendium endpoint when available, otherwise
  `session:<session_id>`. Resuming a run across sessions doesn't preserve its identity.

## 6. Baseline comparison mode

`--policy baseline` runs the Day-1 deterministic heuristic, which plays combat only (`step` and
`play-combat`). It plays lethal first, then kills of attacking enemies, then block, then damage,
then end turn, using card numbers parsed from English text. It never runs unless you select it.

## 7. Live validation checklist (pending; needs a machine with the game)

1. `sts-agent doctor` on the main menu and inside a fight.
2. `sts-agent observe --save-raw tests\fixtures\live_combat_1.json --save-frame traces\frame1.json`.
   Check the facts against the screen.
3. `sts-agent decide-frame traces\frame1.json`. This is one billable call, with no execution.
4. `sts-agent step` (one action), then `sts-agent play-combat` (one fight), then
   `sts-agent replay traces\<session_id>`.
5. `sts-agent play-run` with conservative `--max-actions`. Watch which screens stop as unsupported,
   and check the shop and rest-site index assumptions in the traces.

If anything breaks, send the trace folder. `debug_raw.jsonl` has the exact API responses.

## 8. Strategic planner

`sts_agent.strategic` keeps a seven-dimension, evidence-grounded assessment. In `play-run`, the
coordinator refreshes it with the same provider:

- at the start of the run
- after a confirmed combat finishes
- when strategic facts change on a settled non-combat screen: relics, potions, HP or gold, act, or
  visible opportunities

It never refreshes for ordinary card plays. Facts the API doesn't expose stay `null`. In
particular, the permanent deck is never reconstructed from combat piles. If an update fails, the
frame says `assessment_unavailable` (or `historical`) and the decision still goes ahead from current
facts. See [docs/STRATEGIC_PLANNER.md](docs/STRATEGIC_PLANNER.md) for its standalone API.

## 9. More documentation

- [docs/LLM_DECISION_ENGINE.md](docs/LLM_DECISION_ENGINE.md): the design handoff this
  implementation follows.
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): the capability table, source evidence,
  assumptions, and what is unsupported.
- [docs/TRACE_FORMAT.md](docs/TRACE_FORMAT.md): baseline (`sts2-trace-1`) and LLM
  (`sts2-trace-2`) traces, and replay.
- [docs/STRATEGIC_PLANNER.md](docs/STRATEGIC_PLANNER.md): the strategic assessor's contracts.
- [tests/fixtures/README.md](tests/fixtures/README.md): all fixtures are **synthetic**.
