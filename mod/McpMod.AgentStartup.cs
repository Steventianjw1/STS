// Deterministic singleplayer setup, dispatched on the game's main thread.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;

namespace STS2_MCP;

public static partial class McpMod
{
    private static Dictionary<string, object?>? ExecuteAgentStartupOption(string option, Node root)
    {
        if (option is "agent_autobattle_status" or "agent_autobattle_on" or "agent_autobattle_off"
            or "agent_autobattle_hold_offers" or "agent_autobattle_ai_pick")
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
                try { SetAgentOfferHold(assembly, advisorType, false); } catch { }
            }
            else if (option == "agent_autobattle_hold_offers")
            {
                // Mentor's own pause_on: Autobattle then holds every in-fight choose-a-card screen
                // instead of answering it, and carries on after the choice is made.
                SetAgentOfferHold(assembly, advisorType, true);
            }
            else if (option == "agent_autobattle_ai_pick")
            {
                // Mentor's own one-decision takeover (its F6): it plays its standing recommendation once.
                if (AgentHeldOfferId(advisorType, advisor) == null)
                    return Error("Spire Mentor is not holding an in-fight offer.");
                advisorType.GetMethod("ExecuteBestOnce")!.Invoke(advisor, null);
            }
            return new Dictionary<string, object?>
            {
                ["status"] = "ok", ["autobattle_version"] = 1,
                ["combat_in_progress"] = fighting, ["mode"] = mode.GetValue(null)!.ToString(),
                ["autobattle"] = autobattle.GetValue(advisor), ["autoplay"] = armed.GetValue(null),
                ["brain"] = advisorType.GetProperty("CurrentBrain")!.GetValue(null),
                ["fight_offer_hold"] = AgentOfferHoldActive(assembly),
                ["fight_offer_held"] = AgentHeldOfferId(advisorType, advisor)
            };
        }
        catch (Exception exc)
        {
            return Error("Spire Mentor Autobattle API is incompatible: " +
                         (exc is TargetInvocationException call ? call.InnerException?.Message : exc.Message));
        }
    }

    // pause_on id of the screen Mentor shows for an in-fight "choose a card" offer (its ScreenName, normalized).
    private const string AgentOfferHoldId = "nchooseacardselectionscreen";
    private static bool _agentAddedOfferHold;

    private static object? MentorPauseConfig(Assembly assembly, out PropertyInfo? pauseOn, out PropertyInfo? effective)
    {
        var type = assembly.GetType("SpireMentor.SimConfig");
        pauseOn = type?.GetProperty("PauseOn");
        effective = type?.GetProperty("EffectivePauseOn");
        return type?.GetField("Instance", BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
    }

    private static FieldInfo? MentorPausedKey(Type advisorType) =>
        advisorType.GetField("_pausedKey", BindingFlags.NonPublic | BindingFlags.Instance);

    // Null when this Mentor build has no pause_on; otherwise whether the hold applies to this launch.
    private static bool? AgentOfferHoldActive(Assembly assembly)
    {
        try
        {
            var instance = MentorPauseConfig(assembly, out _, out var effective);
            if (instance == null || effective == null) return null;
            return effective.GetValue(instance) is IEnumerable<string> ids && ids.Contains(AgentOfferHoldId);
        }
        catch { return null; }
    }

    private static void SetAgentOfferHold(Assembly assembly, Type advisorType, bool on)
    {
        var instance = MentorPauseConfig(assembly, out var pauseOn, out _);
        // Without the paused-decision record and the one-shot takeover a held offer could never be released.
        if (instance == null || pauseOn == null || MentorPausedKey(advisorType) == null ||
            advisorType.GetMethod("ExecuteBestOnce") == null)
            throw new InvalidOperationException("this Spire Mentor build cannot hold in-fight offers");
        var ids = (pauseOn.GetValue(instance) as IEnumerable<string> ?? Array.Empty<string>()).ToList();
        if (on && !ids.Contains(AgentOfferHoldId))
        {
            ids.Add(AgentOfferHoldId);
            _agentAddedOfferHold = true;
        }
        else if (!on && _agentAddedOfferHold)
        {
            ids.Remove(AgentOfferHoldId);
            _agentAddedOfferHold = false;
        }
        else return;
        pauseOn.SetValue(instance, ids);
    }

    // The open in-fight choose-a-card screen, when Mentor recorded that it is holding exactly that screen.
    // Mentor's record is "<advice_for>|<event>|<floor>|<screen node id>"; null means "not held" or "unknown".
    private static string? AgentHeldOfferId(Type advisorType, object advisor)
    {
        try
        {
            if (!CombatManager.Instance.IsInProgress) return null;
            if (NOverlayStack.Instance?.Peek() is not NChooseACardSelectionScreen screen) return null;
            string id = screen.GetInstanceId().ToString();
            return MentorPausedKey(advisorType)?.GetValue(advisor) is string key &&
                   key.StartsWith("card_reward|", StringComparison.Ordinal) &&
                   key.EndsWith("|" + id, StringComparison.Ordinal) ? id : null;
        }
        catch { return null; }
    }
}
