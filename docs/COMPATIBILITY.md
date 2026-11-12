# Compatibility, coverage, and limitations

## Game and mod versions

| Item | Current reference |
| --- | --- |
| Game interface | [STS2MCP](https://github.com/Gennadiyev/STS2MCP), single-player localhost HTTP API, default port 15526. |
| Source-inspected revision | `55e064850a68f3b4cde7e5fd525bf9b2dec4e885` (`0.4.0-9-g55e0648`), containing the STS2 v0.107 compatibility fix. |
| Tagged release | 0.4.0 predates that fix; both builds report 0.4.0, so the greeting alone cannot distinguish them. |
| Repository extension | `mod/McpMod.AgentPreview.cs`, installed into the upstream checkout by `tools/patch_mod_preview.py`; additive native previews, permanent deck, enemy move IDs/history, and ancient act map. |
| Historical v0.107.1 evidence | Successful builds and mod startup/API reads were recorded; this is not full gameplay validation. |
| Historical v0.111.0 evidence | Pinned upstream source failed compilation on changed lobby APIs. No compatible port is established by this repository. |
| Enemy reference | Static v0.107.1 data from `tfriedel/spire-mentor` commit `d3115ef`; build mismatches are warned about, not automatically blocked. |

Primary game endpoints are `GET /`, `GET /api/v1/singleplayer`,
`POST /api/v1/singleplayer`, and optional `GET /api/v1/compendium` for run ID.
The game build is manually declared in config; the API does not report it.
English game text is recommended for exact catalog matching and baseline parsing.

Rebuild against the installed game after updates. Use the
[extension instructions](COMBAT_VISIBILITY.md#rebuilding-the-mod-with-the-extension)
and [recorded checks](LOCAL_VALIDATION.md), rather than assuming a version label
or successful Python import establishes game compatibility.

## Evidence categories

- **Source-inspected:** an adapter is implemented from the pinned mod's fields,
  actions, and index semantics. Some ordering assumptions remain explicit.
- **Synthetic:** scripted states/replies or injected provider clients exercise
  contracts and execution plumbing, without establishing live behavior or play strength.
- **Recorded live results:** historical build/startup/API reads are described
  separately in `LOCAL_VALIDATION.md`; they do not certify all actions.

`sts-agent capabilities` prints the current code table. Every screen still has
`live_validated=false`; no screen currently uses `synthetic_only` status. The
latest commit's merchant/rest fixes are implemented behavior, not a blanket
claim that a complete autonomous run has been verified.

## Screen capability table

| Screen / state | Status | Offered actions | Evidence and limits |
| --- | --- | --- | --- |
| Combat (`monster`, `elite`, `boss`) | source-inspected | `play_card`, `end_turn`, `use_potion`, `discard_potion` | Cards require observed playability and verified targeting. Targets use stable numeric `combat_id`. Accepted play is queued, then observed. |
| `hand_select` | source-inspected | `combat_select_card`, `combat_confirm_selection` | State/action use the same holder list; selected cards are visible. |
| `rewards` | source-inspected | `claim_reward`, `proceed`, `discard_potion` | State/action use the same enabled-button filter. |
| `card_reward` | source-inspected | `select_card_reward`, `skip_card_reward` | Card holders are sorted by position in both paths. |
| `map` | source-inspected | `choose_map_node` | Travelable nodes are ordered by column. Visible graph and bosses inform advice. |
| `event`, including ancients | source-inspected | `choose_event_option`, `advance_dialogue` | Locked buttons are excluded. Ancient evidence is advisory; patched map data has no travel action from this screen. |
| `rest_site` | source-inspected | `choose_rest_option`, `proceed` | Room-option order matching UI-button order remains an assumption. No enabled option and no proceed means not ready. |
| Regular `shop` | source-inspected | `shop_purchase`, `proceed`, potion discard where exposed | State's concatenated entries are assumed to align with `inventory.AllEntries`; a different changed stock entry stops with `adapter_index_mismatch`. |
| `card_select` | source-inspected | `select_card`, `confirm_selection`, `cancel_selection` | Card-grid selection state is not exposed; an accepted invisible toggle may be `accepted_unobservable`. |
| `relic_select` | source-inspected | `select_relic`, `skip_relic_selection` | State/action use the same holder list. |
| `treasure` | source-inspected | `claim_treasure_relic`, `proceed` | Reading state opens the chest inside the mod. |
| `bundle_select` | unsupported | None | Upstream actions exist; adapter absent. |
| `crystal_sphere` | unsupported | None | Minigame adapter absent. |
| `fake_merchant` | unsupported | None | Separate from the supported regular shop. |
| `overlay`, menus, unknown | unsupported | None | Unrecognized popups, run start, profile/difficulty management remain out of scope. |

Common room adapters may also offer potion discard. Potion use outside combat
is not offered: the mod's combat-use flag does not establish any-time use.
Unknown targeting or playability is reported, not guessed. LLM mode supports
X-cost and unparsed-description cards when the mod confirms playability and the
action's targeting semantics are supported.

Regular shop reads open inventory and can disable the room proceed button.
The adapter therefore offers `proceed` whenever inventory is ready, because the
mod closes inventory before proceeding. When known potion capacity is full,
potion purchases are withheld until the model frees a slot. These rules do not
add support for `fake_merchant`.

## Execution guarantees and assumptions

The mod enqueues `play_card` and immediately returns `status: ok`; it does not
prove resolution or exclude another card already resolving. Readiness uses the
player play phase plus consecutive identical reads (two by default). This is a
settling assumption, not a game-provided action-complete flag. The coordinator
also requires fresh fingerprint/binding validation before a single POST.

Game rejection can be HTTP 200 with `status: error`. HTTP 500 and delivery
uncertainty are treated as ambiguous: observe, never resend automatically.
Living-enemy `entity_id` values can be renumbered on deaths, so execution targets
numeric `combat_id` instead. A card has no persistent per-instance ID; shifting
hand indices are validated against the fresh screen.

Combat completion is observed from transitions and summaries. Positive-HP game
over is `run_over_unconfirmed`, not certified victory. Shop ordering and rest
UI ordering still need broader live confirmation. Unsupported overlays can
interrupt otherwise supported screens.

## Optional information and advisory limits

The patched mod supplies complete deck facts outside combat, native per-target
card variables, monster/current-move IDs and five recent performed moves, and a
read-only act map during ancient choices. Missing extension fields stay unknown;
older mod builds can still use basic supported actions. Combat piles expose
contents, never hidden draw order. Previews are conditional UI values, not a
simulation. Static forecasts may be wrong after phase/condition changes and do
not apply current status modifiers to base reference damage. See
[combat visibility](COMBAT_VISIBILITY.md).

Expert statistics are public observational evidence, not game facts or causal
recommendations. Routes depend on sparse pooled losses and simplified recovery/
gold assumptions; missing risk can make projections optimistic. Elite gaps are
catalog rules that omit relic/potion/synergy coverage. Ancient relic effects are
text-derived, and option route projections model only recognized resource
changes. Missing or failed external support is advisory and does not stop a
choice from current facts. See [EXPERT_STATS.md](EXPERT_STATS.md).

## Providers and budgets

DeepSeek (`.[deepseek]`, OpenAI-compatible SDK transport) is the default.
Anthropic (`.[llm]`, official SDK) is optional. Both require an explicit model
and the corresponding environment key, disable SDK retries, share decision/
assessment budgets, report usage, and reject truncation, refusal, or unexpected
non-text/tool output. DeepSeek requests JSON and excludes `reasoning_content`.
Optional `deepseek_reasoning_effort` accepts `low`, `high`, or `max`; actual API
support depends on the selected model. Omission uses the provider default.

Provider failures stop without a gameplay fallback. Invalid decision or
assessment output allows at most one corrective retry per operation. Synthetic
provider checks are historical evidence, not proof of current account/model
availability. The documentation review makes no billable provider calls.

## Explicit baseline comparison

`--policy baseline` uses the original combat-only heuristic and supports neither
non-combat decisions nor in-combat selections or potion use. It requires fully
parsed English effect text, does not search sequences or model Weak/Vulnerable,
and uses simple lethal/block/damage rules. It has no tactical fallback role in
LLM mode. Standard-library core execution does not imply a real provider runs
without its optional SDK.
