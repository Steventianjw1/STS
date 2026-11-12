# Combat and deck information available to the LLM

The adapter exposes current player/enemy HP, block, visible statuses (`id`,
`name`, `amount`, `type`, `description`), card descriptions, and supported
candidates. Unknown values remain null. The mod extension adds information;
missing extension fields do not become guessed mechanics.

## Deck and pile contents

Outside combat, `facts.deck` is the complete permanent deck supplied by the
patched mod, grouped by identity, upgrade state, cost, type, rarity, and text,
with `count`. Optional star cost is preserved. A missing deck or malformed card
identity makes it unavailable; combat piles are never used to reconstruct it.
The strategic assessor can use this deck on non-combat refreshes.

During combat, the frame omits the duplicate permanent deck and includes the
hand plus `piles.draw`, `piles.discard`, and `piles.exhaust`. Piles contain full
contents grouped by name/cost/text with counts (and star cost where supplied),
not just a truncated sample or a list of names. Draw order is hidden. The
normalization schema is `sts2-obs-2`; saved normalized observations may include
the permanent deck when the mod supplies it, even when combat frames omit it.

## Observed intents and static forecasts

STS2MCP's `monster.NextMove.Intents` describes **one upcoming enemy move**.
Multiple entries, such as attack and buff, are simultaneous parts of that move,
not successive turns. Intent labels/descriptions are retained; incoming damage
is parsed from the displayed label without applying modifiers again.

In combat decision frames, each enemy has a three-entry `intent_forecast`:

| Offset | Meaning |
| --- | --- |
| 1 | Observed next move, or unknown if not exposed. |
| 2 | Predicted following enemy turn when a reference transition can be resolved; otherwise unknown. |
| 3 | One further predicted turn when the reference remains resolvable; otherwise unknown. |

`enemy_patterns.py` annotates the normalized enemies when building the screen
view. The raw normalized observation alone retains unknown offsets 2–3.
Forecasts do not advance the game or consume RNG.

Fetch the local reference with:

```sh
python tools/fetch_monster_data.py
```

The downloader pins `tfriedel/spire-mentor` commit `d3115ef`, file
`sts2-wiki/data/v0.107.1/monsters.json`, to ignored
`data/monster_reference/monsters.json`. Configure `monster_data_path` if using a
different local location. Missing/unreadable data disables reference forecasts.
A configured build differing from v0.107.1 produces a warning in pattern metadata;
it does not disable predictions automatically.

The extension supplies `monster_id`, `next_move_id`, and up to five recent
performed `move_history` IDs. Reference matching falls back to entity ID or
unique displayed name. Without an exposed move ID, current position may be
inferred from a unique intent-type/hit signature; ambiguous matches stay unknown.
`move_pattern` includes match/position provenance, cycle/transition information,
weighted random choices, and recent moves when supplied.

Future entries use `reference_pattern_prediction`, with deterministic-per-data
or random alternatives/probabilities. Unresolved probability mass is labeled;
conditional/missing transitions leave later turns unknown. The reference omits
some HP thresholds, phase changes, deaths, stuns, and player-effect conditions.
Prediction damage is **base reference data** adjusted for ascension when known,
not for current Strength, Weak, Vulnerable, or other modifiers. The model is
instructed to account for those and re-check each new observed move. These are
conditional forecasts, not guaranteed three-turn outcomes.

## Target-specific native previews

`mod/McpMod.AgentPreview.cs` adds `player.hand[].combat_previews`. For each enemy
target (including each AoE target), it clones dynamic variables using the real
card identity and calls
`UpdateDynamicVarPreview(CardPreviewMode.Normal, target, vars)`. It serializes
each variable's name, base value, and integer preview value. Self cards have a
null target. Live card variables and selected UI targets are not overwritten.

These are the installed game's damage/block hooks, including current player,
target, and relic modifiers. Candidate `details.effect_preview.targets` binds
values to numeric `target_combat_id`. For simple fully parsed descriptions,
summary fields include `damage_per_hit`, `hits_from_text`,
`total_damage_if_unchanged`, `hp_loss_if_unchanged` after block, and `block_gain`.
Complex cards still expose available native variables while unsupported summaries
stay null. Potions expose descriptions rather than native card previews.

Native values already include current modifiers; do not apply them twice.
They are UI previews, not full simulations: triggers, retaliation, changes between
hits, card replay, and later actions can change results. Missing/failed previews
are unknown, not zero or a guaranteed base-text result. Statuses and previews
participate in the fingerprint, so changed values invalidate a pending choice.

## Rebuilding the mod with the extension

Use the source-inspected upstream revision
`55e064850a68f3b4cde7e5fd525bf9b2dec4e885`, .NET 9, and compatible installed
game assemblies. From this repository root, for example in PowerShell:

```powershell
python tools/patch_mod_preview.py .venv/mod-build/STS2MCP
dotnet build .venv/mod-build/STS2MCP/STS2_MCP.csproj `
  '-p:STS2GameDir=C:/Program Files (x86)/Steam/steamapps/common/Slay the Spire 2' `
  -o .venv/mod-build/out-preview
```

Clone/check out the pinned upstream source first as shown in the [README](../README.md).
Use the Python/.NET executable paths installed locally; the old portable setup
used `.venv\python.exe` and `.venv\dotnet\dotnet.exe`.

The patcher copies the repository extension and adds hooks for hand previews,
player deck, enemy move state, and `event.agent_map` on ancient screens. It is
idempotent for its expected source layout and refuses missing/nonunique anchors.
Ancient map data uses the existing read-only map builder and does not provide
travel actions during the event.

With the game closed, back up its existing `mods/STS2_MCP.dll`, copy the rebuilt
DLL and upstream manifest (`mod_manifest.json` as `STS2_MCP.json`) into `mods`,
then reopen with mods enabled. Rebuild and verify after game/version changes.
A prior preview-only DLL is not evidence that all current extension fields are
installed; confirm them in a saved observation/frame.

## Verification scope

Historical v0.107.1 assembly inspection/build records establish the native
preview API and compilation of the then-current extension. Synthetic local
checks exercised transport of target-specific damage, block, AoE/multi-hit,
complex/missing previews, statuses, and invalidation. They do not verify game
formulas or model reasoning. Later deck/move/map hooks require verification in
the installed DLL; do not extend an older build result to newer code implicitly.

Current capability flags remain `live_validated=false`. See
[LOCAL_VALIDATION.md](LOCAL_VALIDATION.md) for historical startup/read results,
known build failures, and the limits of the current documentation review.
