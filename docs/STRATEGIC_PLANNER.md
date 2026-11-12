# Strategic planner

`src/sts_agent/strategic/` maintains an evidence-grounded, revisable assessment
of the run. It is integrated with `decision/coordinator.py`; the same configured
DeepSeek or Anthropic provider makes assessment and action calls. This reference
replaces the original standalone-planner implementation handoff.

The assessor describes needs and threats and maintains a standing plan for the
deck. The [decision engine](LLM_DECISION_ENGINE.md)
selects an offered action and the coordinator validates and executes it. The
assessment is labeled model judgment, not a fact or an instruction that overrides
the current position.

## Interface and lifecycle

```python
class TextModel(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...

class StrategicPlanner:
    def __init__(self, model: TextModel, *, transcript_path=None): ...
    def update(self, snapshot: StrategicSnapshot, *, force: bool = False) -> PlannerResult: ...
    def reset(self) -> None: ...
```

`update` also accepts a mapping of snapshot constructor fields. The component is
synchronous and single-caller; the provider owns timeouts. Optional transcripts
should stay under ignored `traces/`. `strategic/models.py` owns validation,
`prompts.py` owns instructions, and `planner.py` owns the update gate and memory.
Disable integration with `[llm].strategic_planner = false`.

The coordinator refreshes initially, after confirmed combat completion, and
when strategic facts change on settled non-combat screens. Deck changes now
produce `deck_changed`; relics, potions, resources, act, and opportunities have
separate reasons. Ordinary card plays never trigger mid-combat refreshes after
the initial assessment. Starting the agent inside combat can produce an initial
assessment with unknown deck and opportunities.

New run IDs reset memory before generating an assessment. Identical snapshots
are no-ops unless forced. New revisions with unchanged facts reuse the previous
assessment, except a new `combat_finished` revision or `force=True` requests a
refresh. Reusing a revision with different content or supplying a revision older
than the latest checked revision is invalid. A failed call can be attempted again
with the identical snapshot. All memory remains session-local in the CLI.

## Snapshot contract

Current schemas are `sts2-strategic-snapshot-0` and
`sts2-strategic-assessment-1`; see `strategic/prompts.py` for the current prompt
version. Snapshot fields include caller-owned `run_id`, nonnegative `revision`,
`data_source` (`observed`, `manual`, `synthetic`), `live_validated`, `facts`, and
bounded `change_reasons`. Synthetic input cannot be live validated.

`snapshot_id` hashes run identity, revision, schema, provenance, facts, and change
reasons. `facts_hash` excludes revision/change reasons for change detection.
The serialized snapshot is limited to 64 KiB. Unknown keys, invalid nested types,
nonfinite values, invalid counts, duplicate entry IDs, and oversized input are
rejected. `null` means unknown; a supplied empty inventory means known absence.

Every top-level fact key is required, with an object or null value:

| Fact | Shape and current coordinator behavior |
| --- | --- |
| `run` | `character`, `difficulty`, `act`, `floor`, `encounter_type`. |
| `resources` | `hp`, `max_hp`, `gold`, `max_energy`; the coordinator currently leaves persistent `max_energy` null. |
| `deck` | `{cards: [...]}` from explicit mod-supplied permanent deck facts when available on the screen; otherwise null. Never reconstructed from combat piles. |
| `relics` | `{items: [...]}` with unique entry IDs, names, descriptions, and counters. |
| `potions` | `{slots, items: [...]}` with unique slots, IDs, names, and descriptions. Capacity stays null when inconsistent or unavailable. |
| `opportunities` | `{items: [...]}` for current non-combat candidates; null during combat. |
| `recent_outcomes` | `{items: [...]}` containing up to ten observed combat summaries retained by the coordinator. |

One optional key may be present and is never null: `glossary`, `{items: [{term,
definition}]}` with up to 60 entries of the game's own tooltip text for terms on
the cards, relics, potions, and offers above (the coordinator sends at most 40).

Deck cards carry `id`, `name`, `count`, `upgraded`, `cost`, `type`, and
`description`. The coordinator creates unique list-entry IDs using card ID,
an upgrade suffix, and an occurrence suffix when needed; these are not game
instance IDs. The strategic schema allows up to 200 deck entries; the current
coordinator takes at most 200 groups. Decision frames retain their own deck facts
and have a separate prompt-size limit.

Opportunities carry `id`, `kind`, `description`, and `details` (a bounded string).
Kinds include rewards, shop items/removals, rest sites, routes, events,
encounters, and other visible choices. The coordinator takes at most 40 items.
It passes immediate offered choices, not the full external evidence package,
into the assessment. In a room reached from an observed map it appends one
`route` opportunity with id `map_after_this_room`: the next rooms, the act boss,
and ranges over every continuation. That entry is not a choice on the screen. Route and ancient evidence are
attached separately to action decision frames.

Recent outcomes carry `id`, `encounter_type`, `result`, `hp_lost`, `turns`, and
`notes`. HP loss is the nonnegative net drop measured at the first post-combat
screen, including end-of-combat effects; the notes also give HP removed and
regained during the fight. Potion counts cover use by this agent;
these summaries do not prove why combat went well or badly.

Complete nested shapes and limits are in the `strategic/models.py` module
reference. No hand, current-turn energy/block, hidden draw order, seed, RNG state,
unrevealed rewards, or raw API response is included in a strategic snapshot.

## Assessment contract

The model returns one bare JSON object with exactly these keys:

```text
summary: nonempty, at most 600 characters, 60 words, and 2 sentences
dimensions: exactly the seven keys listed below
  each: judgment (<=240 characters), horizon (<=80),
        confidence (low|medium|high), evidence (0..3 JSON Pointer paths)
priorities: 0..3 items
  each: need (<=160), horizon (<=80), tradeoff (<=160),
        reconsider_when (<=160), evidence (0..3 JSON Pointer paths)
standing_plan: exactly these six keys
  deck_needs, avoid: 0..4 strings each, each <=160 characters
  removal_targets, upgrade_targets: 0..4 strings each, each <=80 characters
  skip_rule, boss_prep: nonempty strings, each <=240 characters
uncertainties: 0..5 strings, each <=160 characters
change_note: <=240 characters
```

| Dimension | Question |
| --- | --- |
| `immediate_threats` | What could end the run before the next improvement? |
| `damage` | Can the deck handle short fights, multiple enemies, and longer damage requirements? |
| `defense` | Can it prevent or recover enough damage over the relevant horizon? |
| `scaling` | Can damage or defense improve enough during long fights at an affordable setup cost? |
| `draw_consistency` | Can it reliably find and play what it needs? |
| `potion_needs` | What needs temporary coverage, and when should reserves be used or replenished? |
| `upcoming_opportunities` | Which visible choices can address current needs? |

`standing_plan` is the maintained part of the assessment. Every later reward,
shop, rest, event, and removal decision is told to check its choice against it,
and the planner is told to start from the previous plan and change an entry only
for a stated reason.

| Field | Meaning |
| --- | --- |
| `deck_needs` | Capabilities the deck still lacks for the fights ahead, most urgent first. |
| `avoid` | What more of would now make the deck worse. |
| `removal_targets`, `upgrade_targets` | Names of current deck cards, best first. A card that cannot be removed is never a removal target. |
| `skip_rule` | The test an offered card must pass to be added instead of skipping or keeping gold. |
| `boss_prep` | What the act boss demands of deck, HP, and potions; says unknown when no boss has been observed. |

The plan is phrased as needs and anti-needs, never as a named archetype. Its
strings are not validated against the deck or the map.

Evidence pointers resolve relative to the supplied facts, for example
`/resources/hp` or `/deck/cards/0/description`, with JSON Pointer escaping.
High confidence requires at least one non-null evidence target. Validating a
pointer establishes that a fact exists, not that the judgment is correct.
The prompt requests horizons, tradeoffs, and conditions for reconsideration,
without forcing a named archetype or inventing win probabilities.

Output is capped at 16 KiB. Malformed JSON, duplicate/extra keys, markdown/prose
wrappers, wrong types, invalid pointers, and bound violations are rejected.
The code wraps accepted content with run/revision, snapshot/facts hashes,
schema/prompt versions, and provenance; the model does not supply those bindings.

## Failure behavior and results

Invalid assessment output permits **at most one corrective retry**, with the
validation error and bounded rejected response. Provider exceptions are not
retried. This differs from the original handoff's single-attempt design.
Decision calls, assessment calls, and retries share the provider's budget.

`PlannerResult.status` is `updated`, `unchanged`, `failed`, or `invalid_input`.
It includes `assessment` (possibly historical), `snapshot_id`,
`latest_checked_revision`, `model_called`, `applies_to_current_facts`, and a
bounded diagnostic. `current_assessment` exposes an applicable assessment only
after a successful `updated` or `unchanged` result.

After failure, the coordinator labels retained advice `historical`, or reports
`assessment_unavailable` if none exists; it can still ask the action model to
decide from current facts. Disabled planning is labeled `planner_disabled`.
During combat a retained current assessment is labeled
`current_in_combat_not_refreshed`. No heuristic assessment replaces a failed one.

## Verification and records

Synthetic model replies verify input/output binding, evidence validation,
refresh/reset rules, and failures. They are not evidence of strategic quality.
Tests and fixtures stay local and Git-ignored. Provider/live status and prior
build checks are documented in [LOCAL_VALIDATION.md](LOCAL_VALIDATION.md).

`strategic_update` trace records include the assessment, applicability, revision,
change reasons, call metadata, and attempt records. Optional standalone planner
transcripts include supplied prompts and bounded returned JSON, without a private
reasoning transcript. [Replay](TRACE_FORMAT.md) validates action records without
resampling the model; it does not regenerate strategic assessments.
