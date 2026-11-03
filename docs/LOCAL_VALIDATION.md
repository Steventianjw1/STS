# Local compatibility and provider checks — 2026-11-03

The user now authorizes game/mod access. No live gameplay or billable LLM call
has been performed during these checks.

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
