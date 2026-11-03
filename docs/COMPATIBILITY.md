# Compatibility, verification status and limitations

## Dependency

| item | value |
|---|---|
| Game interface | [STS2MCP](https://github.com/Gennadiyev/STS2MCP), a mod that serves a localhost REST API on port 15526 |
| Source revision inspected | `55e064850a68f3b4cde7e5fd525bf9b2dec4e885` (`git describe`: `0.4.0-9-g55e0648`), dated 2026-07-29: "Fix game API compatibility with STS2 v0.107" |
| Latest tagged release | `0.4.0` (2026-05-05). **It predates the v0.107 fix.** |
| Mod self-reported version | `0.4.0`, for both the release and the newer source (`McpMod.Version` was not bumped) |
| Endpoints used | `GET /`, `GET /api/v1/singleplayer`, `POST /api/v1/singleplayer` (`play_card`, `end_turn`), and optionally `GET /api/v1/compendium` (for `run_id` only) |

## What is verified vs assumed

### Verified by reading the mod source at `55e0648` (not live)

- `play_card` checks the phase, energy and target, then **enqueues** a `PlayCardAction` and returns
  `{"status":"ok"}` at once. "ok" means queued, not resolved. The agent confirms resolution by polling.
- `play_card` does **not** refuse while another card is resolving. Only `end_turn` checks
  `InCardPlay`. That is why the agent never sends a second action before the first has visibly
  resolved.
- A rejected action returns HTTP 200 with `{"status":"error","error":...}` and has no effect.
  HTTP 500 means an exception on the game thread, which the agent treats as ambiguous.
- `target` accepts either an `entity_id` (like `LOUSE_0`) or a numeric `combat_id`. Entity IDs are
  recomputed over *living* enemies, so `LOUSE_1` becomes `LOUSE_0` when `LOUSE_0` dies. The agent
  sends `combat_id`.
- The busy/ready signal is `battle.is_play_phase` together with `battle.turn == "player"`. No
  "card mid-play" or "actions disabled" flag is exposed. The agent therefore also waits for two
  identical consecutive reads.
- In-combat card selection prompts appear as `state_type: "hand_select"`. Unrecognized overlays
  appear as `state_type: "overlay"`.
- Combat end is signaled by the combat room with no `battle` and a "Combat ended" message, then by
  `rewards`, `card_reward` or `map`. Defeat shows as `game_over`.
- Hand cards carry no numeric damage or block fields. Values exist only in the rendered
  `description`. Cards have no per-instance id.
- The draw pile is returned sorted by rarity and id, not in draw order.
- The game build is not reported by any endpoint.

### Assumed (needs live confirmation)

- That your installed game build and mod build match each other. The 0.4.0 release DLL probably
  fails on STS2 v0.107+, because the fix removed `CombatManager.IsPlayPhase`. This is not confirmed.
- The exact wording of Ironclad starter-card descriptions, which the effect parser recognizes:
  `Deal N damage.`, `Gain N Block.`, `Apply N Vulnerable.` Any other wording is reported as
  unsupported, never guessed.
- That a hand card's `description` already includes the player's own damage modifiers. If you have
  any debuff, damage is treated as unverified for lethal checks.
- That intent `label` is the displayed number (`"11"` or `"5x2"`).
- That two identical consecutive reads 0.25 s apart mean the game has settled after a card play.
- Card IDs in the test fixtures are placeholders. **All fixtures are synthetic.**

### Live validation status

**Pending.** The game and mod cannot be run on the development device. All checks so far are
offline:

- 62 local pytest tests (48 original, 5 strategic planner, 9 decision engine), passing on Python
  3.11 and 3.13. Every one uses synthetic states and scripted model replies.
- A CLI smoke test of `doctor`, `observe`, `play-combat` and `replay` against a scripted fake HTTP
  server.

Mocked checks are not live verification.

## LLM decision engine: screen capability table

Run `sts-agent capabilities` for the live copy of this table. All entries come from a read-only
review of the STS2MCP source at commit `55e0648` (`McpMod.Actions.cs` and
`McpMod.StateBuilder.cs`). **`live_validated` is false for every screen.**

There are three capability states:

- **source-inspected**: the action, its parameters and its index semantics were checked in the mod
  source, so the live adapter may send these payloads.
- **synthetic-only**: exercised only with synthetic states, so it is never sent live. No screen
  currently has this status.
- **unsupported**: the agent stops with `unsupported_decision_screen` and sends nothing.

| Screen (`state_type`) | Status | Actions offered to the LLM | Evidence / caveats |
|---|---|---|---|
| combat (`monster`/`elite`/`boss`) | source-inspected | `play_card` (with target `combat_id`), `use_potion`, `discard_potion`, `end_turn` | `play_card` only enqueues; resolution is observed. A card with no `target` is accepted for target types `None`/`Self`/`AllEnemies`. Other target types are listed as a limitation and not offered. X-cost and unparsed-text cards **are** offered when `can_play` is true. |
| `hand_select` | source-inspected | `combat_select_card`, `combat_confirm_selection` | Same holder list in state and action. Selected cards are visible. |
| `rewards` | source-inspected | `claim_reward`, `proceed`, `discard_potion` | Same enabled-button filter in state and action. |
| `card_reward` | source-inspected | `select_card_reward`, `skip_card_reward` | Both sorted by position. |
| `map` | source-inspected | `choose_map_node` | Both list travelable nodes ordered by column. Visible map node types and boss names are included. |
| `event` | source-inspected | `choose_event_option` (unlocked only), `advance_dialogue` | Same option-button list. Event outcomes are not predicted. |
| `rest_site` | source-inspected | `choose_rest_option`, `proceed` | **Assumption:** the state lists `restSiteRoom.Options` while the action indexes UI buttons, so the same order is assumed. |
| `shop` | source-inspected | `shop_purchase`, `proceed` | **Assumption:** the state concatenates card, relic, potion and removal entries while the action indexes `inventory.AllEntries`. The coordinator stops with `adapter_index_mismatch` if a different item's stock changes after a purchase. The mod opens the inventory when state is read. |
| `card_select` (grid / choose) | source-inspected | `select_card`, `confirm_selection`, `cancel_selection` | **Limitation:** grid screens don't expose which cards are selected. A toggle with no visible change is recorded as `accepted_unobservable` and listed in history. |
| `relic_select` | source-inspected | `select_relic`, `skip_relic_selection` | Same holder list. |
| `treasure` | source-inspected | `claim_treasure_relic`, `proceed` | The mod opens the chest when state is read. |
| `bundle_select` | unsupported | none | The mod has the actions; the adapter is not implemented. |
| `crystal_sphere` | unsupported | none | The mod has the actions; the adapter is not implemented. |
| `fake_merchant` | unsupported | none | The adapter is not implemented. |
| `overlay`, `menu` (popups), unknown | unsupported | none | Menus, popups and starting runs are out of scope. |

Potion use **outside combat** is not offered, because the API's `can_use_in_combat` flag doesn't
distinguish combat-only from any-time potions. Discarding is offered.

## Provider (Claude) integration

- **Adapter:** `providers/anthropic.py`, using the official `anthropic` SDK as the optional extra
  `[llm]`, imported lazily. The calls follow the
  [Python SDK](https://platform.claude.com/docs/en/api/sdks/python) and
  [Messages API](https://platform.claude.com/docs/en/api/messages/create) documentation, reviewed
  on 2026-11-03.
- **Retries and timeouts:** `max_retries=0`, so the configured timeout is the whole call budget,
  and every call counts against the per-run budget.
- **Credentials:** only from `ANTHROPIC_API_KEY`. Errors are sanitized. Config rejects key-like
  fields.
- **Rejected responses:** a `stop_reason` of `max_tokens` (truncated), `refusal`, or
  `stop_details.type == "refusal"`; any other stop reason; and non-text content blocks.
- **Verification:** checked only with an injected fake SDK client. **No paid or real call has been
  made, so real-model behavior is unvalidated.**

## Baseline comparison mode: unsupported mechanics

These apply only to `--policy baseline`, the Day-1 heuristic. The LLM engine does not share them.

- In-combat card selection stops the baseline.
- Cards whose text the parser doesn't fully recognize are never played by the baseline.
- The baseline reads English card text only.
- The baseline never uses potions.
- The baseline doesn't model Vulnerable or Weak, and plans no multi-card sequences.
- The baseline handles no non-combat screens.
- Multiplayer is not supported in either mode: only the singleplayer endpoint is used.

## Known baseline heuristic weaknesses

- It blocks whenever visible incoming damage exceeds current block, even for 1 point.
- When block is needed, the first block card in hand order wins ties.
