// Deterministic singleplayer setup, dispatched on the game's main thread.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Runs;

namespace STS2_MCP;

public static partial class McpMod
{
    private static Dictionary<string, object?>? ExecuteAgentStartupOption(string option, Node root)
    {
        if (option is "agent_autobattle_status" or "agent_autobattle_on" or "agent_autobattle_off")
            return ExecuteAgentAutobattle(option);
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

    private static Dictionary<string, object?> ExecuteAgentAutobattle(string option)
    {
        // Optional dependency: use the installed Mentor's public API on the game thread.
        var assembly = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a => a.GetName().Name == "SpireMentor");
        if (assembly == null) return Error("Spire Mentor is not loaded; install and enable it first.");
        try
        {
            var advisorType = assembly.GetType("SpireMentor.SimAdvisor", true)!;
            var runtime = assembly.GetType("SpireMentor.SimRuntime", true)!;
            var config = assembly.GetType("SpireMentor.SpireMentorModConfig", true)!;
            var advisor = advisorType.GetField("Instance")!.GetValue(null)!;
            var mode = runtime.GetProperty("Mode")!;
            var armed = runtime.GetProperty("AdvisorAutoplayArmed")!;
            var autobattle = advisorType.GetProperty("AutobattleOn")!;
            bool fighting = CombatManager.Instance.IsInProgress;
            if (option == "agent_autobattle_on")
            {
                var manager = RunManager.Instance;
                if (!manager.IsInProgress || manager.NetService.Type.IsMultiplayer() || !fighting)
                    return Error("Autobattle requires an active singleplayer combat.");
                advisorType.GetMethod("SetBrain")!.Invoke(advisor, new object[] { "search", "STS Agent" });
                runtime.GetProperty("Style")!.SetValue(null,
                    Enum.Parse(runtime.GetProperty("Style")!.PropertyType, "Coach"));
                mode.SetValue(null, Enum.Parse(mode.PropertyType, "Advisor"));
                if (!(bool)autobattle.GetValue(advisor)!)
                    advisorType.GetMethod("ToggleAutobattle")!.Invoke(advisor, null);
            }
            else if (option == "agent_autobattle_off")
            {
                if ((bool)armed.GetValue(null)!)
                    advisorType.GetMethod("ToggleAutoPlay")!.Invoke(advisor, null);
                mode.SetValue(null, Enum.Parse(mode.PropertyType, "Off"));
                config.GetProperty("CoachOnAtLaunch")!.SetValue(null, false);
            }
            return new Dictionary<string, object?>
            {
                ["status"] = "ok", ["autobattle_version"] = 1,
                ["combat_in_progress"] = fighting, ["mode"] = mode.GetValue(null)!.ToString(),
                ["autobattle"] = autobattle.GetValue(advisor), ["autoplay"] = armed.GetValue(null),
                ["brain"] = advisorType.GetProperty("CurrentBrain")!.GetValue(null)
            };
        }
        catch (Exception exc)
        {
            return Error("Spire Mentor Autobattle API is incompatible: " +
                         (exc is TargetInvocationException call ? call.InnerException?.Message : exc.Message));
        }
    }
}
