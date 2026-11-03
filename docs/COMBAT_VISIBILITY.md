# Combat information available to the LLM

Every combat decision includes player and enemy HP, block, and visible statuses
(`id`, `name`, `amount`, `type`, `description`). These statuses were already
included before the preview extension. The decision prompt now explicitly tells
the LLM to compare targets and their status-adjusted previews.

## Enemy intent horizon

STS2MCP reads `monster.NextMove.Intents`. This is **one upcoming enemy move**.
Multiple entries (for example attack and buff) are simultaneous components of
that move, not separate future rounds. Intent labels and hover descriptions are
retained. Parsed incoming damage uses the game's displayed label, without
reapplying Weak, Vulnerable, or other modifiers.

Each enemy now has an explicit three-entry `intent_forecast`:

| `enemy_turn_offset` | Information |
| --- | --- |
| 1 | Observed next move, when exposed by the mod |
| 2 | `intents: null`, `source: not_exposed` |
| 3 | `intents: null`, `source: not_exposed` |

There is no exact three-round forecast. Future moves can depend on random
choices and intervening actions. The agent does not advance the game or consume
RNG to predict them. The LLM is instructed to plan those turns conditionally.

## Target-specific native previews

The additive extension in `mod/McpMod.AgentPreview.cs` produces
`player.hand[].combat_previews`. For each enemy target (including each enemy
affected by an AoE card), it:

1. Clones the card's dynamic variables, keeping the real card as their owner.
2. Calls the game's `UpdateDynamicVarPreview(CardPreviewMode.Normal, target, vars)`.
3. Exposes each variable's name, base value, and integer preview value.

This uses the installed game's damage/block hooks, including player modifiers,
target modifiers and relics. It avoids hardcoding formulas from another version
and avoids changing the live card's variables or the selected UI target. For
example, a Strike can expose `Damage: 9` against a Vulnerable enemy and
`Damage: 6` against an ordinary enemy. These are illustrative numbers, not a
claim that every attack or version uses that formula.

Each candidate card action includes `details.effect_preview.targets`, bound by
numeric `target_combat_id`. Self cards use a null target ID. The original
description and complete visible status lists remain available. For simple,
fully parsed descriptions, the adapter also exposes `damage_per_hit`,
`hits_from_text`, `total_damage_if_unchanged`, `hp_loss_if_unchanged` after current
block, and `block_gain`. For complex cards, the native variables still reach the
LLM, while unsupported summaries remain null.

These are UI previews, **not full simulations**. Triggers, retaliation, block
changes, card replay, and status changes between hits can change the outcome.
Totals assume unchanged modifiers across hits. Potions currently expose their
descriptions, not native card previews. The LLM is instructed not to apply status
modifiers a second time and to reconsider its choice after every resolved action.
Statuses and previews participate in the decision fingerprint, so changes
invalidate a previously chosen action.

If the extension is missing or a native preview throws, its values stay unknown.
The adapter reports the missing capability instead of treating base card text
as an exact target-specific prediction. Old mod builds remain usable.

## Rebuilding the mod with the extension

Apply this repository's additive patch to the source-inspected STS2MCP checkout
(`55e064850a68f3b4cde7e5fd525bf9b2dec4e885`), then build normally:

```powershell
.\.venv\python.exe tools\patch_mod_preview.py PATH_TO_STS2MCP_SOURCE
.\.venv\dotnet\dotnet.exe build PATH_TO_STS2MCP_SOURCE\STS2_MCP.csproj `
  '-p:STS2GameDir=C:/Program Files (x86)/Steam/steamapps/common/Slay the Spire 2' `
  -o .venv/mod-build/out-preview
```

With the game closed, back up its existing `mods/STS2_MCP.dll`, copy the rebuilt
DLL into that location, and reopen the game. The patch script is idempotent and
refuses a source layout without its expected hand-card serialization anchor.
Rebuild against the installed game after version changes.

## Verification

Source inspection: the installed v0.107.1 game assembly confirms the native
preview API, damage/block hooks, dynamic-variable cloning, and integer display
conversion. STS2MCP's source confirms the next-move intent horizon.

Synthetic checks in the existing ignored local decision tests verify that
target-specific damage, block, status stacks and descriptions reach the actual
serialized LLM message, including AoE/multi-hit and missing/complex previews.
They also verify decision invalidation and unknown later intents. Synthetic
native values test transport, not game formulas or model reasoning.

The preview-enabled mod compiles against the installed v0.107.1 game with zero
warnings/errors. Live validation is recorded separately in `LOCAL_VALIDATION.md`.
