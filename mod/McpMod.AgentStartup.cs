// Deterministic singleplayer setup, dispatched on the game's main thread.
using System;
using System.Collections.Generic;
using Godot;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Runs;

namespace STS2_MCP;

public static partial class McpMod
{
    private static Dictionary<string, object?>? ExecuteAgentStartupOption(string option, Node root)
    {
        if (option != "agent_startup_status" && option != "agent_abandon_run" &&
            !option.StartsWith("agent_ascension_", StringComparison.Ordinal)) return null;

        var manager = RunManager.Instance;
        var screen = FindFirst<NCharacterSelectScreen>(root);
        var lobby = screen != null && IsNodeVisible(screen) ? screen.Lobby : null;
        bool multiplayer = (manager.IsInProgress && manager.NetService.Type.IsMultiplayer()) ||
                           (lobby != null && lobby.NetService.Type.IsMultiplayer());
        if (option == "agent_startup_status")
        {
            var run = manager.IsInProgress ? manager.DebugOnlyGetState() : null;
            var player = run != null ? LocalContext.GetMe(run) : null;
            return new Dictionary<string, object?>
            {
                ["status"] = "ok", ["startup_version"] = 1,
                ["multiplayer"] = multiplayer, ["run_in_progress"] = manager.IsInProgress,
                ["run_character"] = player?.Character.Id.Entry,
                ["run_ascension"] = run?.AscensionLevel,
                ["selected_character"] = lobby?.LocalPlayer.character?.Id.Entry,
                ["game_mode"] = lobby?.GameMode.ToString(),
                ["ascension"] = lobby?.Ascension, ["max_ascension"] = lobby?.MaxAscension
            };
        }
        if (multiplayer) return Error("Agent startup requires singleplayer");
        if (option == "agent_abandon_run")
        {
            if (!manager.IsInProgress) return Error("No active run to abandon");
            if (!manager.IsAbandoned) manager.Abandon();
            return new Dictionary<string, object?> { ["status"] = "ok", ["message"] = "Abandoning active run" };
        }
        if (manager.IsInProgress || lobby == null) return Error("Ascension requires character select");
        if (!int.TryParse(option.Substring("agent_ascension_".Length), out int level) ||
            level < 0 || level > 10 || level > lobby.MaxAscension)
            return Error($"Ascension must be unlocked and between 0 and {Math.Min(10, lobby.MaxAscension)}");
        lobby.SyncAscensionChange(level);
        return new Dictionary<string, object?> { ["status"] = "ok", ["ascension"] = lobby.Ascension };
    }
}
