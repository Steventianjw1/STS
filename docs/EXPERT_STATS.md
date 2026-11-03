# Expert decision statistics

`get_expert_decision_stats` is a read-only Python adapter in
`src/sts_agent/expert_stats.py`. In normal LLM sessions the coordinator calls it
before decisions on card rewards, event options and shop purchases. Its compact
result is included as `decision_support` in the model's decision frame and trace.
The model still selects a legal action; the adapter never performs game actions.

`observe --save-frame traces/frame.json` and `observe --preview-decision` include
the same evidence. Saving a frame makes no model call. `decide-frame` and replay
use the saved evidence without contacting Codex. Scripted/synthetic CLI sessions
do not automatically contact Codex. Combat selections, removals, upgrades and
transformations do not use reward pick rates.

## Cohort and filters

1. Request `GET /api/leaderboards/elo?limit=100&min_runs=30` (the minimum is configurable).
2. Keep returned public accounts with at least `min_character_runs` rated runs
   on the observed character. Sort by that character's Elo, breaking ties by
   character run count and username. Select up to `cohort_size` accounts.
3. Fetch `GET /api/players/{username}/stats` for each selected account with
   `character`, `ascension`, `players=1`, and `version` when configured.

The board is a global top 100 over solo A10 standard play. The cohort is selected
within that board, not an exhaustive ranking of everyone playing the character.
It does not use `wr75` or other submitter win-rate brackets. English display
names such as `The Ironclad` and canonical character IDs are accepted.

By default profiles match the observed ascension and configured `game_build`.
An explicit `[expert_stats].ascension` overrides the profile ascension, for
example to compare an A0 position with experts' A10 decisions. The returned scope
records the actual filter. The tool never broadens an empty filter automatically.
Without a known `game_build`, or with `match_version=false`, profiles span all
versions and this limitation is reported. Profiles cannot filter game mode, so
their counts can include daily/custom runs despite the board's standard-only
qualification. Snapshot lag and no runs for the exact filters are reported.

## Rates and missing data

For every matching candidate the adapter pools counts across available profiles:

| Metric | Numerator | Denominator |
|---|---|---|
| Card reward pick rate | Sum of `taken` | Sum of `offered` |
| Merchant purchase rate | Sum of `bought` | Sum of `seen` |
| Event choice share | Sum of `chosen` for this option | Sum of all recorded choices at this event |

Each row includes the raw numerator/denominator, percentage, contributing player
count, per-player counts and the largest player's share of the denominator.
Percentages are never averaged. Card and shop rows missing from a profile remain
unknown; they do not contribute zero observations. For events, a player with
other recorded choices at the event contributes zero choices of an unchosen
option and their full event denominator. Repeated stages count as recorded
choices, not distinct visits. Event shares do not account for which options were
available, and purchase rates do not account for affordability or prices.

Event buttons are matched using an observed `option_id` when available, otherwise
by an exact, unique normalized title from `GET /api/events/{id}?lang=eng`, including
options on subsequent pages. Formatting tags/whitespace/case are ignored. Button
indices and fuzzy text similarities are never used to identify historical options.
Unmatched or ambiguous options have unavailable evidence.

The response labels partial cohorts, missing rows and unavailable profiles.
It contains no floor/deck conditioning, recommendations to buy a particular item,
or inferred causal win-rate gains. The model must still consider current needs,
resources, skipping and leaving.

## Configuration and cache

Defaults are shown in `sts_agent.example.toml` under `[expert_stats]`:

```toml
[expert_stats]
enabled = true
cohort_size = 5
min_character_runs = 30
# ascension = 10             # omit to match the observed difficulty
match_version = true
lang = "eng"                # match the game's language for event titles
cache_dir = "traces/spire_codex_cache"
cache_ttl_s = 86400.0
request_timeout_s = 3.0
max_fetch_s = 12.0
```

The optional `SPIRE_CODEX_API_KEY` comes from the environment/ignored `.env`.
Credentials are not saved in frames, cache files or manifests. Public responses
are cached in memory and atomically on disk, keyed by the full endpoint and
filters. The default cache directory is Git-ignored. Keep custom cache paths
ignored too. Cache write failures do not discard a usable response.

Expired data is not served. HTTP/network failures and private/missing profiles
are reported rather than treated as zero. Errors are briefly held in memory to
avoid repeated failed fetches. A 429 honors `Retry-After` by deferring further
network requests; it never sleeps/retries during the decision. Each fetch has a
timeout and a total decision fetch budget, reduced by the remaining run time.
Fresh game-state validation still runs before sending any chosen action.

API responses have a local fetch timestamp/cache source. The public profile API
does not expose its own data-through timestamp, so a recent fetch does not imply
a recently rebuilt statistics snapshot. Existing frames without evidence retain
their original hashes; new evidence participates in the frame hash and replay.

Sources: [API documentation](https://spire-codex.com/docs),
[developer documentation](https://spire-codex.com/developers),
[player statistics implementation](https://github.com/ptrlrd/spire-codex/blob/main/backend/app/services/player_stats.py).
