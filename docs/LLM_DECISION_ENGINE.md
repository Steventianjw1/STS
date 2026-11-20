# LLM decision engine

This is the current architecture reference, replacing the original implementation
handoff. The code in `src/sts_agent/decision/` is authoritative for field shapes,
capabilities, and execution behavior. The current decision prompt is
`decision-prompt-20`, frame schema `sts2-decision-frame-1`, and LLM policy version
`1.1.0`. See [compatibility](COMPATIBILITY.md) for validation limits.

## Responsibilities

```text
STS2MCP -> normalized observation -> supported screen + candidates
                                          |
                  strategic assessment + advisory evidence + bounded history
                                          |
                                     DecisionFrame
                                          |
                     configured model -> validated action ID
                                          |
                       fresh state + binding validation
                                          |
                     send once -> observe resolution -> trace
```

One configured provider instance is shared by the strategic assessor and action
policy. DeepSeek is the default; Anthropic is supported. Both implement
`TextModel.complete(system=..., user=...)`, use optional SDKs, and share call/token
accounting, and any configured budget, across decisions, strategic updates,
corrective retries, provider retries, and what-if tool calls. No budget applies
by default. SDK retries are disabled; the policy itself retries a
transient provider failure. Core/offline execution is standard-library-only.
A session started from the [interface](INTERFACE.md) streams its DeepSeek calls
so the reasoning can be displayed; the answer is assembled and validated as a
plain response is, and the reasoning is never part of the decision or the trace.

| Module | Responsibility |
| --- | --- |
| `decision/screens.py` | Raw screen state to allowlisted facts, candidates, private payload bindings, readiness, and limitations. |
| `decision/models.py` | Frame hash, serialization, choice parsing, and map-plan validation. |
| `decision/prompts.py` | Versioned instruction modules assembled per screen, advisory labels, and prompt-size reduction. |
| `decision/policy.py` | Model call, validation, corrective and provider retries (unlimited unless configured), and the optional what-if tool round. |
| `decision/whatif.py` | The two read-only what-if calculations the model may request. |
| `decision/coordinator.py` | Session bounds, memory, strategic refresh, fresh validation, execution, and resolution. |
| `decision/offline.py` | Explicitly synthetic clients and scripted model replies. |
| `providers/` | DeepSeek/Anthropic transport, sanitized diagnostics, usage, and budget accounting. |
| `autobattle.py` | Commands to Spire Mentor's native Autobattle through the mod bridge. |
| `hooks.py` | No-op observer and pause gate the coordinator reports to; the interface overrides it. |

## Facts and frames

`DecisionFrame` binds run identity, screen/capability context, observation
fingerprint, fact provenance, current facts, candidates, limitations, assessment,
previous turn intent, up to eight recent history entries, and optional
`decision_support`. Its `frame_id` hashes the entire context, including private
execution bindings. The public model message omits those bindings.

`null` means unknown. Game text and external catalog/profile text are data, not
instructions. No seed, RNG state, hidden draw order, or unrevealed rewards are
passed as observed facts. External cohort identities and statistics stay in
labeled advisory evidence rather than game facts.

The patched mod supplies the complete permanent deck. Non-combat frames expose
it as grouped `facts.deck` entries; combat frames omit that duplicate inventory
and include full grouped draw/discard/exhaust piles. Combat piles are never used
to reconstruct the permanent deck. Game-native previews already include current
status modifiers. Enemy forecast offset 1 is observed; offsets 2–3, when
available, are static reference predictions with base values and explicit limits.
See [combat visibility](COMBAT_VISIBILITY.md).

Candidates contain a frame-local `action_id`, kind, description, and details.
Python creates all targets, indices, and HTTP payloads. LLM combat candidates
use observed `can_play` and verified targeting; X-cost or unparsed effect text
alone does not exclude a playable card. The baseline parser's narrower support
does not constrain LLM choices.

Three blocks are added to `current_facts` when available:

- `route_context` (non-map rooms): the act graph as last seen on the map, read
  from the room the agent travelled to: next rooms, the act boss, and ranges over
  every continuation (rest sites, shops, elites, fights before the next rest).
  `stored_route_plan` adds the remaining rooms of the model's own route plan when
  the room is on it. Absent means the coming rooms are unknown.
- `keyword_glossary`: the game's own tooltip text for terms on the cards,
  relics, potions, and offers of this state, collected from the patched mod. A
  term without an entry stays unknown.
- `energy_observation`: the last observed maximum energy, labeled as dated.

The `strategic_assessment` block carries the planner's `standing_plan` (deck
needs, what to avoid, removal and upgrade targets, a skip rule, boss preparation)
on every screen. See [strategic planner](STRATEGIC_PLANNER.md).

## Instructions per screen

The system prompt is assembled by `prompts.system_instruction(frame)`. The
harness knows the screen kind, so it picks the modules; the model never selects
or loads instructions. Every prompt has the core rules and the response contract.
The rest depends on the screen:

| Screen | Added modules |
| --- | --- |
| `combat`, `hand_select` | Combat play and combat-state reading. |
| Picker opened during a fight | Fight-picker rules and combat-state reading. |
| `card_reward` | Non-combat rules, card-reward rules, expert-profile, duplicate, upgrade-after-pick, and elite-readiness notes. |
| `shop` | Non-combat rules, shop rules, expert-profile, upgrade-after-pick, and elite-readiness notes; `shop_plan` contract. |
| `map` | Non-combat rules, map rules, route evidence notes; `route_plan` contract. |
| `rest_site`, non-combat `card_select` | Non-combat rules, rest or grid rules, upgrade-choice notes. |
| `event` | Non-combat rules, expert-profile and upgrade-choice notes; ancient events use the ancient-offer notes instead. |
| `rewards` | Non-combat rules; reward-batch contract. |
| Anything else | Non-combat rules and expert-profile notes. |

Module names used for a decision are recorded as `prompt_modules`.
`SYSTEM_INSTRUCTION` is every module concatenated, kept for inspection only; it is
never sent.

Supported screens cover combat, hand selections, rewards, map, events, rest,
regular shops, card grids, relic selection, and treasure. Unsupported screens
stop before selecting an action. Source-inspected coverage, synthetic checks,
and live validation are separate claims. Run `sts-agent capabilities`.

## Response contract

Each response is one bare JSON object:

```json
{
  "action_id": "one-offered-action-id",
  "reason": "Brief explanation grounded in the current position.",
  "turn_intent": "Short conditional plan for later choices."
}
```

The model is not asked to echo the frame ID; an echoed `frame_id` is ignored.
Reject wrappers, duplicate/missing/extra keys, nonfinite values, incorrect types,
and unoffered action IDs. `reason` is nonempty and at most
500 characters; `turn_intent` is at most 300 characters and may be empty.
Response text is capped at 8 KiB. Payloads are never accepted from the model.

On map screens only, an optional `route_plan` key is allowed:

```json
{
  "route_plan": {
    "path_id": "one supplied path ID, or empty string",
    "plan": "Intended route for this act and why.",
    "change_course_if": "Observable reasons to reconsider."
  }
}
```

This is a fragment to add to the base response, not a standalone reply.
Both text fields are at most 300 characters; `plan` must be nonempty. A nonempty
`path_id` must occur in `decision_support.paths.candidates` and start with the
chosen `action_id`. An empty ID permits a textual plan without a supplied path.
Omitting `route_plan` retains the previous plan. Non-map responses cannot include it.

On shop screens an optional `shop_plan` lists the whole visit: offered
`shop_purchase`/`proceed` action IDs in execution order, starting with
`action_id`, without repeats, within current gold and free potion slots. Leaving
must be last, and so must card removal. The coordinator executes the steps one by
one with state checks and abandons the rest when prices, stock, or gold change.

On map screens, choosing a campfire as the immediate destination requires a
`rest_option` key: the stable uppercase option ID (`HEAL`, `SMITH`, or another
observed option). Travel and that option are one decision. On arrival the
coordinator selects the option with fresh bindings and no model call; if the
arrival, the player's resources, or the option's availability differ from what
was expected, the plan is dropped and the campfire is decided as a normal
screen. A response for any other destination must not include the key.

On rewards screens a `reward_batch` candidate collects all gold and then claims
one other reward or proceeds, as one choice. Each step is still sent, observed,
and validated on its own, and an unexpected change abandons the rest.

Invalid output is answered with a corrective retry against the same frame, and a
transient provider failure (timeout, dropped connection, 408/409/425/429, 5xx)
is retried with the same prompt after a short backoff. Both repeat until a valid
choice arrives unless `[llm].corrective_retries` or `[llm].provider_retries`
sets a limit; a requested stop also ends the loop. Nothing is sent to the game
in between, and the screen is revalidated before any action. Other provider
failures stop. No fallback chooses an action. The
[strategic planner](STRATEGIC_PLANNER.md) is different: it allows one corrective
retry for invalid assessment output and does not retry provider failures.

## What-if tools

With `[llm].what_if_tools = true` (default) the user message lists
`available_tools` for the screen. Instead of a choice the model may reply once
with `{"tool": "<name>", "args": {...}}`; it then receives the same frame again
with `tool_result` and must choose. The tool call is one extra model call, is not
a corrective retry, and never acts in the game. Bad arguments return an error
result.

| Tool | Offered when | Returns |
| --- | --- | --- |
| `deck_after` | Non-combat screens with the permanent deck | Deck size, cards by type, cost counts, basics, curses, and draw-cycle length before and after hypothetical additions (offered action IDs) and removals (card names). Counts only. |
| `route_projection` | Non-map rooms on the stored route plan, after a map screen of this act produced a risk model | The plan's remaining rooms re-projected from one to three hypothetical HP values: expected and worst-tenth boss-arrival HP, death risk before the next rest and before the boss. Same pooled model and limits as map paths. |

Tool lists and results travel only in the message; the recorded frame and
`frame_id` are unchanged.

## Evidence, route planning, and memory

[Spire Codex evidence](EXPERT_STATS.md) is requested before relevant decisions:

- Card rewards, ordinary events, and shops: expert profile rates, optional
  indexed floor/deck contexts, and broader community deck advice.
- Map: expert route profile, pooled HP risk, bounded path enumeration,
  elite-readiness gaps, and route-plan reconsideration triggers.
- Ancient events: offer-conditioned expert evidence and relic effects compared
  with the deck, resources, and readable act map.

Evidence is advisory. Fetches have a shared per-decision deadline. Expected
data/network failures return availability
labels; unexpected external-data errors become unavailable support rather than
stopping gameplay. Python does not select a route, reward, or purchase.

A session retains the strategic assessment, up to ten combat summaries for the
assessor, a map plan, the act graph of the last map screen, and combat outcomes
used for replanning. Combat outcomes record the net HP change and, separately, HP
removed and regained during the fight; route projections scale hallway fights and
elites/bosses separately from HP removed. Decision frames
carry the assessment and up to eight encounter-local decision/outcome history
entries. Encounter-local history and turn intent reset on encounter changes.
A stored route plan records
its act, position, HP/gold, relics/potions, path trajectory when supplied, and
number of combat outcomes seen. Triggers report changed act/path/resources,
new potions/relics, unusually costly fights, or an imminent elite with deck gaps.
The model decides whether and how to revise its plan.

Run identity comes from the mod compendium when available, otherwise
`session:<session_id>`. With a compendium ID, observed facts, and a real model,
the coordinator saves same-run state to `traces/run_state/<run id>.json` after
each resolved action. A later session restores it when the file is for the same
run and character and the game is at or past the saved act and floor:

- Always: the route plan, fight outcomes and route calibration, map memory, the
  energy observation, and the shop entry.
- Only when the session resumes in the same room: the open encounter's history,
  turn intent, and combat tally.
- A fight that ended while no session was watching is recorded as `unobserved`.

The strategic assessment is not saved and is rebuilt. Session-scoped runs and
scripted models save nothing. There is no cross-run learning.

## Input-size handling

No local user-message cap applies by default; only the provider's context limit
does. When `[llm].max_input_bytes` is set and the message exceeds it,
`build_user_message` reduces context in this order:

1. Remove per-option contributor and history-widening detail.
2. Remove `supplementary_deck_advice`, when present.
3. Remove oldest encounter history entries.
4. Replace all decision support with an `omitted_for_size` marker.
5. Fail with `decision_input_too_large` if essential context still cannot fit.

Facts and candidates are never trimmed. These reductions affect only serialized
prompt text; the full recorded frame and `frame_id` remain unchanged. Traces
record `history_dropped` and `support_trim` (`detail_dropped`,
`deck_advice_dropped`, `support_omitted`, or null).

## Execution and stopping

The coordinator waits for a supported ready screen and consecutive identical
observations (two by default). It refreshes strategy when needed, obtains one
validated model choice, and records it before sending. It then re-reads the game
and requires the same screen fingerprint and action binding. A stale choice is
recorded without being sent, and so is one that meets a configured action limit
(`not_sent_action_limit`). There is no session time limit.

A ready screen with exactly one way forward skips the strategy refresh, the
evidence, and the model. `automatic_action` (`screens.py`) recognizes a single
map destination that is not a campfire, a lone proceed, confirm, or option, an
end turn when no card or potion can be used, a shop where nothing is affordable,
and the confirmation of an already-previewed card selection. Potion discards are
ignored when counting alternatives. A `reward_batch`, a potion use, a card play,
or a grid toggle whose result cannot be observed is never automatic. The click
is recorded as an `automatic_decision` and goes through the same fresh
validation and resolution.

The action is posted once. Accepted means queued, so the coordinator polls for
screen-specific resolution. Rejected actions are withheld for that exact state.
An ambiguous request is inspected and never resent. Shop resolution checks stock
alignment and stops on `adapter_index_mismatch`; unobservable card-grid toggles
are recorded as `accepted_unobservable`.

Regular shops offer leave whenever inventory is ready, even when opening that
inventory makes the room's `can_proceed` false. Full potion slots suppress potion
purchases; supported discard choices allow the model to free a slot. Rest sites
with neither enabled options nor proceed are not ready, so transitional states
are polled rather than immediately reported as actionable with no candidates.

With `[autobattle].enabled`, Spire Mentor's native Autobattle plays whole fights
through the mod bridge (`autobattle.py`, `mod/McpMod.AgentStartup.cs`). This is
the only part of Mentor the agent uses: it sends `status`, `on`, `off`,
`hold_offers`, and `ai_pick` and reads nothing else. There is no LLM combat
fallback; a fight Mentor stops playing ends the session with `autobattle_failed`.
With `offer_handoff` (default on) the bridge asks Mentor to hold in-fight
choose-a-card offers through its own `pause_on` list and reports the held screen.
A held offer is answered exactly once: one that opened during the enemy's turn (a
boss's forced debuff choice) is decided by the model with the fight in view; any
other is handed back to Mentor's own pick. An older mod or Mentor build that
cannot hold leaves fights entirely with Mentor. The earlier per-action combat
search, which read Mentor's advisor and mapped its recommendation onto a
candidate, has been removed along with `[combat_search]`.

The coordinator reports to `SessionHooks` (`hooks.py`), which does nothing unless
the [interface](INTERFACE.md) is attached. Hooks observe and gate; they never
choose an action. A pause or stop is honored only at safe points: before a step,
while waiting for a ready screen, after a strategy refresh, before the fresh
validation that precedes a send, and in the delegated-fight loop, where Autobattle
is switched off for the pause and on again after it. No such point lies between
sending an action and observing its resolution. A pause after the strategy call
discards that step so the screen is observed again; a pause before a send goes
through the usual fresh validation on resume. Time spent paused restarts the
no-progress clock. A requested stop ends the session with `interrupted` and ends
the policy's retry loop before its next retry.

Outside delegated fights and the forced clicks above, the model chooses every
gameplay action, including confirmations, end turn, rewards, and leaving.
Polling is automatic; choices between alternatives are not. The mod
can open inventory/chests as a side effect of state reads. Progress accounting
avoids treating normal model/evidence latency as a game stall. Exhaustion of a
configured budget, unsupported screens, unresolved actions, and session bounds
stop explicitly. Positive-HP game over is `run_over_unconfirmed`,
not certified victory.

## Recording and verification

LLM sessions use `sts2-trace-2`; explicit baseline sessions use `sts2-trace-1`.
Frames, choices, optional map and shop plans, retries, tool calls, prompt modules,
provider usage, strategy updates, trimming, and outcomes are recorded separately. No private reasoning transcript
is stored: reasoning streamed for the interface is displayed and discarded. Replay validates saved LLM frames and offered action IDs
without resampling a model or contacting the game/evidence service. See
[trace format](TRACE_FORMAT.md) for exact replay limits.

Scripted clients/replies verify harness behavior and must be labeled synthetic.
They establish neither play strength nor live compatibility. Existing tests and
fixtures remain local and ignored by Git. Historical build/API checks and the
latest documentation-review checks are separated in
[validation records](LOCAL_VALIDATION.md). Live access and bounded verification
are authorized by `AGENTS.md`; the obsolete handoff's offline-only restriction
no longer describes repository policy.
