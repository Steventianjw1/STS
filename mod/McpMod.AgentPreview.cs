// Additive STS2MCP extension for the agent; built against STS2 v0.107.1.
using System;
using System.Collections.Generic;
using System.Linq;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;

namespace STS2_MCP;

public static partial class McpMod
{
    private static List<Dictionary<string, object?>>? BuildAgentPermanentDeck(Player player)
    {
        try
        {
            // Read the permanent pile directly, including copies and upgrades.
            // Do not reconstruct it from combat cards or mutate the pile.
            return player.Deck.Cards.Select(card => new Dictionary<string, object?>
            {
                ["id"] = card.Id.Entry,
                ["is_upgraded"] = card.IsUpgraded
            }).ToList();
        }
        catch { return null; }
    }

    private static Dictionary<string, object?> BuildAgentPreview(CardModel card, Creature? target)
    {
        var result = new Dictionary<string, object?>
        {
            ["target_combat_id"] = target?.CombatId,
            ["source"] = "game_native_preview_v1",
            ["variables"] = null
        };
        try
        {
            // Use the real card for hook identity, but clone its variables. Never
            // overwrite the live card's preview, base values, target, or UI state.
            var vars = card.DynamicVars.Clone(card);
            card.UpdateDynamicVarPreview(CardPreviewMode.Normal, target, vars);
            result["variables"] = vars.Values.Select(v => new Dictionary<string, object?>
            {
                ["name"] = v.Name,
                ["base_value"] = (int)v.BaseValue,
                // Same integer conversion used by DynamicVar.ToHighlightedString.
                ["preview_value"] = (int)v.PreviewValue
            }).ToList();
        }
        catch
        {
            // A missing preview is unknown, never a base-value substitute.
        }
        return result;
    }

    private static List<Dictionary<string, object?>> BuildAgentPreviews(CardModel card)
    {
        if (card.TargetType == TargetType.AnyEnemy || card.TargetType == TargetType.AllEnemies)
            return card.CombatState?.Enemies.Where(e => e.IsAlive)
                .Select(e => BuildAgentPreview(card, e)).ToList()
                ?? new List<Dictionary<string, object?>>();
        return new List<Dictionary<string, object?>> { BuildAgentPreview(card, null) };
    }
}
