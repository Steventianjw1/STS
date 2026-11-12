# LLM decision engine

This is the current architecture reference, replacing the original implementation
handoff. The code in `src/sts_agent/decision/` is authoritative for field shapes,
capabilities, and execution behavior. The current decision prompt is
`decision-prompt-10`, frame schema `sts2-decision-frame-1`, and LLM policy version
`1.0.0`. See [compatibility](COMPATIBILITY.md) for validation limits.

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
                  fresh state + binding + deadline validation
                                          |
                     send once -> observe resolution -> trace
```

One configured provider instance is shared by the strategic assessor and action
policy. DeepSeek is the default; Anthropic is supported. Both implement
`TextModel.complete(system=..., user=...)`, use optional SDKs, and share call/token
budgets across decisions, strategic updates, and corrective retries. SDK retries
are disabled. Core/offline execution is standard-library-only.

| Module | Responsibility |
| --- | --- |
| `decision/screens.py` | Raw screen state to allowlisted facts, candidates, private payload bindings, readiness, and limitations. |
| `decision/models.py` | Frame hash, serialization, choice parsing, and map-plan validation. |
| `decision/prompts.py` | Versioned instructions, advisory labels, and prompt-size reduction. |
| `decision/policy.py` | Model call, validation, and at most one corrective retry. |
| `decision/coordinator.py` | Session bounds, memory, strategic refresh, fresh validation, execution, and resolution. |
| `decision/offline.py` | Explicitly synthetic clients and scripted model replies. |
| `providers/` | DeepSeek/Anthropic transport, sanitized diagnostics, usage, and budget accounting. |

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

Supported screens cover combat, hand selections, rewards, map, events, rest,
regular shops, card grids, relic selection, and treasure. Unsupported screens
stop before selecting an action. Source-inspected coverage, synthetic checks,
and live validation are separate claims. Run `sts-agent capabilities`.

## Response contract

Each response is one bare JSON object:

```json
{
  "frame_id": "copy-the-current-frame-id",
  "action_id": "one-offered-action-id",
  "reason": "Brief explanation grounded in the current position.",
  "turn_intent": "Short conditional plan for later choices."
}
```

Reject wrappers, duplicate/missing/extra keys, nonfinite values, incorrect types,
mismatched frame IDs, and unoffered action IDs. `reason` is nonempty and at most
500 characters; `turn_intent` is at most 300 characters and may be empty.
Response text is capped at 8 KiB. Payloads are never accepted from the model.

On map screens only, an optional fifth key is allowed:

```json
{
  "route_plan": {
    "path_id": "one supplied path ID, or empty string",
    "plan": "Intended route for this act and why.",
    "change_course_if": "Observable reasons to reconsider."
  }
}
```

This is a fragment to add to the four-key response, not a standalone reply.
Both text fields are at most 300 characters; `plan` must be nonempty. A nonempty
`path_id` must occur in `decision_support.paths.candidates` and start with the
chosen `action_id`. An empty ID permits a textual plan without a supplied path.
Omitting `route_plan` retains the previous plan. Non-map responses cannot include it.

Invalid output permits one corrective retry against the same frame. Provider
failures are not retried. No fallback chooses an action. The
[strategic planner](STRATEGIC_PLANNER.md) separately allows one corrective retry
for invalid assessment output using the same budget.

## Evidence, route planning, and memory

[Spire Codex evidence](EXPERT_STATS.md) is requested before relevant decisions:

- Card rewards, ordinary events, and shops: expert profile rates, optional
  indexed floor/deck contexts, and broader community deck advice.
- Map: expert route profile, pooled HP risk, bounded path enumeration,
  elite-readiness gaps, and route-plan reconsideration triggers.
- Ancient events: offer-conditioned expert evidence and relic effects compared
  with the deck, resources, and readable act map.

Evidence is advisory. Fetches have a shared per-decision deadline and remaining
session-time allowance. Expected data/network failures return availability
labels; unexpected external-data errors become unavailable support rather than
stopping gameplay. Python does not select a route, reward, or purchase.

A session retains the strategic assessment, up to ten combat summaries for the
assessor, a map plan, and combat outcomes used for replanning. Decision frames
carry the assessment and up to eight encounter-local decision/outcome history
entries. Encounter-local history and turn intent reset on encounter changes.
A stored route plan records
its act, position, HP/gold, relics/potions, path trajectory when supplied, and
number of combat outcomes seen. Triggers report changed act/path/resources,
new potions/relics, unusually costly fights, or an imminent elite with deck gaps.
The model decides whether and how to revise its plan.

Run identity comes from the mod compendium when available, otherwise
`session:<session_id>`. Memory is in-process/session-local; an exposed run ID does
not restore an earlier session's plans. There is no cross-run learning.

## Input-size handling

The default user-message cap is 98,304 UTF-8 bytes (`[llm].max_input_bytes`). If
the message exceeds it, `build_user_message` reduces context in this order:

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
and requires the same screen fingerprint and action binding, with time remaining
before the session deadline. A stale choice is recorded without being sent.

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

The model chooses every gameplay action, including confirmations, end turn,
rewards, and leaving. Polling is automatic; gameplay decisions are not. The mod
can open inventory/chests as a side effect of state reads. Limits include model
latency; progress accounting avoids treating normal model/evidence latency as a
game stall. Budget exhaustion, unsupported screens, unresolved actions, and
session bounds stop explicitly. Positive-HP game over is `run_over_unconfirmed`,
not certified victory.

## Recording and verification

LLM sessions use `sts2-trace-2`; explicit baseline sessions use `sts2-trace-1`.
Frames, choices, optional map plans, retries, provider usage, strategy updates,
trimming, and outcomes are recorded separately. No private reasoning transcript
is requested or stored. Replay validates saved LLM frames and offered action IDs
without resampling a model or contacting the game/evidence service. See
[trace format](TRACE_FORMAT.md) for exact replay limits.

Scripted clients/replies verify harness behavior and must be labeled synthetic.
They establish neither play strength nor live compatibility. Existing tests and
fixtures remain local and ignored by Git. Historical build/API checks and the
latest documentation-review checks are separated in
[validation records](LOCAL_VALIDATION.md). Live access and bounded verification
are authorized by `AGENTS.md`; the obsolete handoff's offline-only restriction
no longer describes repository policy.
