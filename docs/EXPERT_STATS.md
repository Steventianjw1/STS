# Expert statistics and decision support

`ExpertDecisionStats` in `src/sts_agent/expert_stats.py` supplies read-only
advisory evidence from [Spire Codex](https://spire-codex.com). It never performs
gameplay actions. The coordinator calls `for_view` before relevant decisions and
includes the result in `decision_support`, separate from observed game facts.
The configured model still selects one offered action.

| Screen | Main evidence |
| --- | --- |
| Card reward, ordinary event, regular shop | `get_expert_decision_stats` (schema 2): profile rates, indexed floor/deck context, optional community advice; card rewards/shops can also carry elite readiness. |
| Map | `get_expert_route_profile` (schema 3): routes, HP risk, path projections, elite readiness, and reconsideration triggers. |
| Ancient event | `get_ancient_offer_evidence` (schema 1): offer-conditioned history, relic effects versus deck/resources, elite threats, and map outlook. |

`observe --save-frame` and `observe --preview-decision` fetch applicable evidence
but have no prior coordinator route plan or strategic assessment. Saving makes no
model call; previewing a decision is billable. `decide-frame` and replay reuse
saved evidence without contacting Spire Codex. Scripted/synthetic CLI gameplay
sessions do not automatically fetch evidence. Combat card selections, upgrades,
removals, and transformations do not use card-reward pick rates.

## Cohort and filters

The client requests `GET /api/leaderboards/elo?limit=100&min_runs=30` (minimum
configurable), then selects up to `cohort_size` public accounts with at least
`min_character_runs` rated runs on the observed character. Accounts are sorted
by character Elo, then character run count and username. This selects within
the global top 100 solo A10 standard leaderboard, not an exhaustive character
ranking or a submitter win-rate bracket. English display names and canonical
character IDs are accepted.

Profiles are fetched through `GET /api/players/{username}/stats` with character,
ascension, `players=1`, and version when configured. Defaults match observed
ascension and declared `game_build`. `[expert_stats].ascension` explicitly
overrides difficulty; `match_version=false` or an unknown build spans versions
and is labeled. An empty exact scope is not automatically broadened.

Profile counts cannot be restricted by game mode and can include daily/custom
runs despite the board's standard-play qualification. The local indexed corpus
separately filters for standard solo runs, character, ascension, and configured
version. Scope, sparse samples, missing profiles, and normalization issues must
be considered before applying evidence.

## Profile rates

| Metric | Pooled numerator | Pooled denominator |
| --- | --- | --- |
| Card reward pick rate | `taken` | `offered` |
| Merchant purchase rate | `bought` | `seen` |
| Event choice share | `chosen` for the option | All recorded choices at the event |

Percentages use summed counts, not averaged player percentages. Rows retain raw
counts, contributor counts, compact contributor detail, and concentration information.
Missing card/shop rows remain unknown. A player with recorded choices at an
event contributes the event denominator and zero to an option they never chose.
Repeated stages are recorded choices, not necessarily distinct visits.

Events match observed `option_id` when present; otherwise they require a unique
exact normalized title from the event catalog, including later option pages.
Formatting tags, whitespace, and case are ignored. Indices/fuzzy similarity are
not historical identifiers. Unmatched or ambiguous buttons have unavailable evidence.

Profile rates are overall rates without floor/deck conditioning. Event shares
do not establish which buttons were offered; shop rates do not account for price
or affordability. High rates and correlated win rates are not causal utility scores.

## Local history and contextual rates

`spire_history.py` maintains `run_history.sqlite3` under `cache_dir`. It indexes
bounded submitted runs, decision rows, reconstructed pre-floor deck contexts,
route floors/outcomes, and ancient offers. Normalizer versions prevent reuse of
incompatible entries. Rejected data and per-scope skipped runs are recorded so
filter mismatches or unsupported shapes do not block later runs indefinitely.
Route records can be retained when card-history reconstruction is unavailable.

Incremental sync rotates players, fetches at most one run per player per pass,
and respects `history_new_runs_per_decision`, `history_runs_per_player`, and the
fetch deadline. To populate the corpus ahead of play:

```sh
sts-agent sync-history --character IRONCLAD --ascension 0 --max-new-runs 100 --max-seconds 120
```

The CLI's ascension is explicit; match it to the desired decision-evidence scope.
Bulk sync makes network requests but no game or model calls. Its run/time values
bound the sync loop; a sync pass can finish after the requested new-run target.

Per-option `history_context` tries nearby floor (within the same act when known),
then act, then cohort. With a complete observed deck, it first tries these scopes
with deck similarity, then without. It stops at `min_context_samples` or reports
the available sparse final scope. Widening attempts and selected scope are explicit.
Deck similarity is multiset Jaccard over base card IDs and copies; upgrades,
relics, resources, and within-room changes are not matched. Historical deck
snapshots are from before the floor and carry completeness/reconstruction issues.
This is context evidence, not an exact simulation of the current decision.

## Community advice

When an explicit permanent deck and relic identities are available,
`supplementary_deck_advice` can call `POST /api/draft-advice` and
`GET /api/runs/pick-coach`. The POST requests advice and does not change game state.
Responses are restricted to offered card IDs; pick-coach receives at most five
offered cards and exposes bounded archetype suggestions.

These broader community scores cannot be restricted to the expert cohort or
exact patch and are not calibrated probabilities. Archetypes are optional
suggestions. Unknown deck/relic identities produce unavailable advice rather
than a reconstructed deck or a guessed score.

## Route profiles, HP risk, and paths

The map adapter supplies visible nodes, edges, travel options, current/visited
positions, and boss identities. The evidence adapter resolves the act variant
from the catalogs and visible boss identity when possible. Unresolved acts are
reported instead of silently choosing a variant.

`route_profile.py` compares all non-abandoned indexed runs entering the act with
the subset that won the whole run. It reports first nodes, node-count quantiles,
first elite/shop positions, hallway fights before elites, shop gold, rest choices
by arrival HP band, and HP checkpoints. Complete-act route-count statistics use
runs that reached the boss. The sparse flag is based on winning sample size.
Run files lack the offered map, so these patterns mix preference, available
paths, and deck strength; they are not causal recommendations.

`route_risk.py` pools mean HP loss, p90 when sufficiently sampled, and death rate
for early/later hallways, elites, bosses, and question-mark nodes. Damage is a
percentage of max HP. Early hallway splitting uses observed weak-encounter shares;
when unavailable, the hallway sample is pooled. Rest healing, gold changes, and
shop spending are measured from the indexed data rather than assumed constants.
Sparse classes may use `/api/runs/encounter-stats` means/death rates. Fallback
scope and sample counts are labeled; fallback does not supply a p90 estimate.

`route_paths.py` enumerates visible paths using a cap on distinct node-type
sequences per node (default 2000), merging equivalent type sequences after the
same first node. It projects HP/gold, boss arrival, fight/elite/rest/shop positions,
and distance from expert median node counts. Most representatives come from the
non-dominated set; a continued prior plan and preparation alternatives can be
retained even when dominated, and carry `dominated`. At most `route_candidates`
paths (default six) are shown with `selected_for` labels, not a combined ranking.

Expected HP uses pooled mean losses. The bad case applies p90 to every fight
(or mean where p90 is missing), which is a stress test rather than a likely
outcome. Simulated rest choices follow measured expert HEAL shares by arrival
HP band. Missing means and death rates can behave as zero contributions inside
the projection; missing healing provides no modeled recovery. Check source,
sparse/unavailable classes, `bad_case_uses_mean_for`, truncation, and unmodeled
node types before interpreting apparently safe paths. Projections assume current
max HP and pooled independent fight outcomes, excluding deck/relic/potion combat
effects and most event consequences. They are not a combat simulator.

The map-only model `route_plan` is retained by the coordinator with its starting
state and available trajectory. `route_plan_status` reports no plan or triggers:
new act, leaving the planned path, HP/gold off projection, a fight worse than p90,
relic changes, a new potion, or a nearby planned elite with deck gaps. The model
replans; Python never chooses a map action. See the
[response contract](LLM_DECISION_ENGINE.md#response-contract).

## Elite readiness

`elite_readiness.py` compares catalog threats for this act and later acts with
observed permanent-deck coverage. It exposes enemies/summons, large attack turns,
Strength gain, and community encounter-build summaries. The actual elite behind
a map node remains unknown.

The gap rules are deliberately limited:

- `aoe`: multiple enemies/summons with no AllEnemies attack in the deck.
- `block`: a largest attack turn at least `elite_block_attack_pct` of max HP,
  or Strength gain, with no non-starter block card.
- `damage_or_scaling`: neither a non-starter attack nor a Power card.

Card rewards/shops show offered cards' `fills_gaps`. Routes can include
fewest-elites and most-preparation alternatives, and planned elites within
`elite_trigger_steps` can trigger reconsideration. Coverage ignores relics,
potions, enchantments, and synergies; community build outcomes are correlations.
Without observed deck facts, threats may still be available while gaps stay unknown.

## Ancient choices and the act map

For `event.is_ancient=true`, `ancient_advisor.py` and the evidence adapter resolve
offered relics from catalog identity/name, fetch pool conditions, and compare
text-derived effects with deck counts, HP/max HP, gold, and expert checkpoints.
Indexed ancient offers supply take-when-offered, head-to-head co-offer counts,
similar HP/deck situations, sparse flags, and observed outcomes. Community lift
and archetype fit are separate evidence. Labels describe options without ranking them.

The patched mod can expose `event.agent_map`. The screen adapter constructs a
read-only planning graph from current/root position, with no actionable travel
indices. `map_outlook` summarizes route ranges, early/unavoidable elites, rests,
shops, survivable shares, and elite gaps. Each option's `route_outlook` reprojects
from recognized HP/max-HP/gold changes; options without detected resource changes
reuse the baseline outlook. This does not simulate a relic's card, combat,
healing, or other effects beyond the recognized resource comparisons. Read the
full relic text; regex tags and projections can be incomplete.

## Configuration, caching, and failures

All fields and example values are in
[`sts_agent.example.toml`](../sts_agent.example.toml). Feature switches include
`enabled`, `history_enabled`, `deck_advice_enabled`, `route_planning_enabled`,
`elite_readiness_enabled`, and `ancient_advisor_enabled`. Map evidence currently
requires both statistics and history enabled; route planning is nested under it.
Ancient advice has its own switch and can still report partial evidence.

Key defaults: cohort 5, minimum character runs 30, corpus cap 100 runs/player,
3 new runs/decision, floor window ±3, minimum context samples 20, deck similarity
0.5, six route candidates, 2000 route sequences/node, HP replan tolerance 15
percentage points, gold tolerance 100, block threshold 20% max HP, elite horizon
three steps. `encounter_stats_bracket` defaults to `solo`; broader fallback
brackets are separate from expert-cohort evidence.

The optional `SPIRE_CODEX_API_KEY` comes from the environment/ignored `.env`.
Cached responses use the full endpoint and request parameters/body, are kept in
memory and atomically on disk, and expire after `cache_ttl_s` (default one day).
Custom cache locations should remain ignored. Cache write failures do not
discard usable responses. Local history is a bounded indexed corpus, not merely
an expiring HTTP response cache.

Fetches use `request_timeout_s` (default three seconds) and total `max_fetch_s`
(default twelve seconds), reduced by remaining session time. A 429 honors
`Retry-After` by deferring requests, without sleeping/retrying during the decision.
Network/private/missing data are labeled unavailable/partial. Unexpected
external-data exceptions are caught at `for_view` and reported as unavailable
support, allowing a decision from current game facts. Fresh game validation
still occurs before execution.

The public API has no data-through timestamp; recent fetching does not establish
recent source statistics. Evidence participates in the frame hash and trace.
Prompt-size handling can drop detail/community advice or all support while
preserving facts/candidates and recording the reduction. See
[trace format](TRACE_FORMAT.md) and [decision engine](LLM_DECISION_ENGINE.md).

Sources: [API documentation](https://spire-codex.com/docs),
[developer documentation](https://spire-codex.com/developers), and
[player statistics implementation](https://github.com/ptrlrd/spire-codex/blob/main/backend/app/services/player_stats.py).
