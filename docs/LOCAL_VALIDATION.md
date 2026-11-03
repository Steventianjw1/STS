# Local compatibility and provider checks — 2026-11-03

## Native combat preview extension

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

## Latest recheck: v0.107.1 — startup and API reads verified

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

## Earlier check: v0.111.0 (historical)

## Installed game and mod

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

## DeepSeek

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
