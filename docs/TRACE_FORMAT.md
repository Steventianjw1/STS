# Trace formats and replay

LLM sessions use `sts2-trace-2`. Explicit baseline sessions retain
`sts2-trace-1`. Trace format and observation schema are separate versions;
current normalization uses `sts2-obs-2`. Neither agent output nor external expert
evidence makes the recorded gameplay a human/expert demonstration.

## Session layout and durability

`step`, `play-combat`, and (LLM only) `play-run` create:

```text
traces/<session_id>/
  manifest.json
  decisions.jsonl
  debug_raw.jsonl   # optional; raw API responses, not training data
```

Session IDs are `YYYYMMDDTHHMMSSZ-<6 hex>`, using UTC independently of local time.
JSONL records have `type`, `session_id`, UTC `ts`, and monotonic `mono` for
durations. Records are flushed immediately; `fsync` is attempted but unsupported
filesystem errors are tolerated. Manifests are rewritten atomically. Until close,
`ended_at` and `outcome` remain null, which can indicate an active or uncleanly
terminated session. Handled exceptions can close a trace with a `crashed` outcome.

The decision is recorded before sending an action. Outcomes are separate records
with matching `seq`; a loader can exclude future outcomes from decision inputs.
Local traces, cached statistics, tests, and fixtures remain Git-ignored.

## Manifest

Common fields include:

| Field | Meaning |
| --- | --- |
| `trace_format`, `session_id`, `command` | Format and session identity/entry point. |
| `run_id` | Mod compendium ID when available. LLM sessions otherwise use `session:<session_id>`; baseline may use null. Seed is not recorded as a normalized fact. |
| `started_at`, `ended_at` | UTC timestamps. |
| `agent_version`, `observation_schema`, `policy` | Interpretation/replay versions. |
| `game_build` | User-declared build, null if unknown; not reported by the API. |
| `mod_version`, `mod_source_commit_adapter_written_against` | Greeting version and source-inspected adapter revision; a 0.4.0 greeting alone does not identify the build. |
| `provenance` | Policy/source labels described below. |
| `config` | Effective config; `game_dir`, `trace_dir`, and expert `cache_dir` are removed. Other configured values, including `monster_data_path`, can remain. |
| `files`, `counts`, `outcome` | Output filenames, decision/outcome/error counts, and final summary. |

LLM manifests additionally carry `model`, `decision_prompt_version`,
`run_id_source` (`mod_compendium` or `session_scoped`), `facts_source`,
`capabilities_mod_revision`, and final `model_usage`. Provider identity includes
provider/model, output cap, timeout, disabled SDK retries, and scripted flag.
DeepSeek identity includes `reasoning_effort` when configured. Usage includes
shared call/input/output token accounting and configured budgets; estimated cost
appears only with configured prices.

Credentials are supplied outside TOML and omitted from provider identity. Normalized
facts exclude raw hidden/profile data. `debug_raw.jsonl` deliberately stores the
raw API response separately and should be reviewed before sharing. Configurable
paths and public cohort usernames in advisory support can also be present.

## Provenance

| Session | Labels |
| --- | --- |
| Baseline | Manifest `data_source=agent_generated`, `behavior=heuristic_baseline_policy`, `is_expert_demonstration=false`; decision provenance `agent_generated_heuristic`. |
| Real model | Manifest `data_source=agent_generated`, `behavior=llm_policy`, `is_expert_demonstration=false`, `facts_source`, `live_validated=false`; records use `llm_generated` or `llm_generated_synthetic_input`. |
| Scripted model | Manifest `data_source=synthetic`, `behavior=scripted_model_replies`, `is_llm_reasoning=false`; records use `synthetic_scripted_model`. |

Observed facts, synthetic inputs, source-inspected adapters, and live validation
are distinct. Current CLI capability flags remain false even when observing a
running game; a trace label does not automatically certify a screen.

## LLM records (`sts2-trace-2`)

| Type | Contents |
| --- | --- |
| `llm_decision` | `seq`, provenance, policy/prompt/model identity, full `frame`, `choice` or null, `attempts`, `error_kind`, `diagnostic`, `history_dropped`, `support_trim`, `provider_retries`, `tool_calls`, and `prompt_modules`. Written before action execution. |
| `outcome` | Same `seq`, verdict, game response, `timing.since_send_s`, `observation_after` (kind/fingerprint/facts), and detail. |
| `strategic_update` | Revision/snapshot ID/change reasons, status, whether a model was called, diagnostic, assessment, applicability, call metadata, and attempts. |
| `event` | Session start and `combat_finished` with observed HP change, turns, and agent potion-use notes. |
| `error` | Such as ambiguous response, invalid strategic snapshot, or unexpected exception. |
| `session_end` | Final session summary. |

The saved `frame` uses `sts2-decision-frame-1` and contains its ID, run identity,
objective, screen/capability metadata, observation fingerprint, fact provenance,
facts, candidates, private `bindings`, limitations, assessment, previous turn
intent, history, and optional `decision_support`. Bindings contain executable
payloads and resolution modes; the public LLM message omits them and labels facts
as `current_facts`. Frames carry at most eight recent encounter history entries.
The current decision prompt version is `decision-prompt-18`. `prompt_modules`
names the instruction modules assembled for that screen.

A successful recorded choice has `action_id`, `reason`, `turn_intent`,
`route_plan` (null or the optional map-only plan), and `shop_plan` (null or the
ordered shop actions). Plans have `path_id`, `plan`,
and `change_course_if`; supplied path IDs must be compatible with the chosen
map action. The coordinator keeps additional plan state/trajectory in memory;
that internal state is not the choice object. Applicable route-plan status and
triggers reappear inside later map support.

Attempt records hold bounded response text (up to 8 KiB for decisions), a
validation error, and provider call usage/stop reason/request ID/latency when
available. An attempt that was a what-if tool request also has `tool_call` with
the parsed `request` and the computed `result`; a failed provider call that was
retried is an attempt with an error and no response text. Strategic attempts have their separate 16 KiB text bound. No private
reasoning transcript is requested; DeepSeek `reasoning_content` is discarded.
Reasons and conditional plans are the contracted public explanation.

### Advisory evidence and prompt reduction

`decision_support` may contain expert per-option rates, floor/deck-conditioned
history, community advice, route profiles/HP risk/path candidates, elite readiness,
or ancient-offer/map outlooks. It records scope, samples, availability, and
provenance where available; it is not an observed game fact. See
[EXPERT_STATS.md](EXPERT_STATS.md).

Support participates in the full frame hash. Absent support is omitted from
serialization to preserve older frame hashes. Size reduction changes the
serialized user message, not the full saved frame or its ID:

| `support_trim` | Meaning |
| --- | --- |
| null | No support reduction needed. History may still be reduced when no support exists. |
| `detail_dropped` | Per-option contributor/history-widening detail removed. |
| `deck_advice_dropped` | Supplementary community advice also removed. |
| `support_omitted` | Entire support replaced by an `omitted_for_size` marker. |

`history_dropped` counts oldest encounter history entries removed. Reduction
order is support detail, community advice, oldest history, then all support.
Facts and candidates are never removed. If essential context still exceeds
`[llm].max_input_bytes`, the decision fails without execution. The saved frame
can therefore be larger than the actual prompt; the trace records reduction
metadata rather than a separate exact copy of every decision prompt.

## Outcomes and ambiguity

| Verdict | Meaning |
| --- | --- |
| `resolved` | Accepted action produced a settled observable state change. |
| `resolved_screen_changed` | Accepted action changed screen/ended combat. |
| `accepted_unobservable` | Supported card-grid toggle accepted but selection is not exposed. |
| `stale_not_sent` | Fresh state or binding differs; chosen action not sent. |
| `rejected` | Game rejected the action; withheld for that exact state. |
| `no_visible_change`, `timeout_after_change` | Accepted but unresolved; session stops. |
| `ambiguous_inferred_executed` | Delivery uncertain but subsequent state change was observed; request is never resent. |
| `ambiguous_unresolved` | Delivery uncertain without sufficient observed resolution; stop, never resend. |
| `not_delivered` | Request known not delivered. |
| `not_sent_validation_read_failed`, `not_sent_deadline` | Validation read failed or deadline reached before POST. |
| `interrupted_during_send` | Interrupted while request was in flight; outcome unknown. |

An inferred execution is an observation-based conclusion, not proof of every
intended effect. Shop alignment failures stop with `adapter_index_mismatch`.
Combat summaries measure net HP change including end-of-combat effects, not a
complete damage accounting or causal diagnosis.

## Baseline records (`sts2-trace-1`)

Baseline `step` and `play-combat` use the same files with `decision` rather than
`llm_decision`. Each record contains `input.observation`, offered candidates,
excluded cards/reasons, and `decision` with `action_id`, bound payload, policy
identity, rule, reason, and scores. The policy is explicitly heuristic; it is
not model reasoning or expert play. Outcomes include response/timing and a
normalized `observation_after`. `blocked_candidates` events record candidates
withheld after rejection for use by replay.

The format is retained for older traces. Do not confuse it with the older
`sts2-obs-1` observation shape. Current `sts2-obs-2` includes:

- Run, phase, player resources/statuses, hand and native card previews.
- `piles.draw`, `piles.discard`, and `piles.exhaust` as grouped card objects with
  counts and text; legacy `draw_composition` and name-only lists are not the
  current contract. Draw order remains hidden.
- Enemy intents, numeric target IDs, statuses, optional monster/current-move IDs,
  recent move history, and initially unknown later forecast offsets.
- Relics, potions/capacity, screen descriptor, and optional explicit permanent deck.
- A fingerprint over normalized content.

Reference forecasts are attached by screen-view construction for LLM frames,
not by normalization alone. Public map/event details likewise belong to screen
adapters. See [COMBAT_VISIBILITY.md](COMBAT_VISIBILITY.md).

## Offline replay and limits

```sh
sts-agent replay traces/SESSION_ID
sts-agent replay traces/SESSION_ID -v
sts-agent replay saved_observation.json
```

Replay never contacts the game, model, or evidence service and does not simulate
or restore game state. A single saved observation uses baseline comparison,
not a new LLM decision. For a saved LLM frame use `decide-frame`; its configured
model is billable unless `--scripted-reply` is supplied.

For LLM records, replay rebuilds `DecisionFrame`, checks the recorded hash,
checks that the selected action was offered, and parses the final recorded model
response under the frame contract (including map-plan validation). It compares
the parsed **action ID** with `choice.action_id`; it does not separately compare
the saved reason, intent, or route-plan text with the parsed response. A recorded
failure with no choice must have an `error_kind`. Strategic-update records and
execution outcomes are not regenerated or independently verified by this replay.

For baseline records, replay recomputes the observation fingerprint, rebuilds
candidate IDs with recorded blocked actions removed, and reruns the deterministic
policy to compare action ID/rule. Policy-version differences are reported. Older
traces may differ after normalization/candidate changes; a retained format name
is not a guarantee of identical behavior across code revisions.

These are recorded-decision integrity checks, not evidence that the LLM would
choose identically on resampling or that an action succeeded in the real game.
