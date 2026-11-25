// Members of the game that differ between the builds the agent supports, read by reflection so
// one mod source builds against STS2 v0.107.1 and v0.111.x. tools/patch_mod_preview.py points
// the upstream lines that no longer compile on v0.111 (StartRunLobby.MaxPlayers and
// LoadRunLobby.ConnectedPlayerIds, both in the multiplayer lobby state) at these helpers.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Godot;

namespace STS2_MCP;

public static partial class McpMod
{
    private const BindingFlags AgentCompatFlags =
        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;

    // The first readable property or field of the given names; null when the build has none of them.
    private static object? AgentCompatMember(object? target, params string[] names)
    {
        if (target == null) return null;
        var type = target.GetType();
        foreach (var name in names)
        {
            try
            {
                var property = type.GetProperty(name, AgentCompatFlags);
                if (property != null && property.GetIndexParameters().Length == 0) return property.GetValue(target);
                var field = type.GetField(name, AgentCompatFlags);
                if (field != null) return field.GetValue(target);
            }
            catch { }
        }
        return null;
    }

    // StartRunLobby.MaxPlayers exists in v0.107.1 and not in v0.111.0: unknown (null) where it is gone.
    private static object? AgentCompatMaxPlayers(object? lobby) =>
        AgentCompatMember(lobby, "MaxPlayers", "MaxPlayerCount");

    // LoadRunLobby.ConnectedPlayerIds exists in v0.107.1 and not in v0.111.0, whose load lobby lists the
    // players that are back in it as Players. Ids are compared as text so no id type is assumed.
    private static HashSet<string>? AgentCompatConnectedPlayerIds(object? lobby)
    {
        try
        {
            if (AgentCompatMember(lobby, "ConnectedPlayerIds") is IEnumerable ids)
                return new HashSet<string>(ids.Cast<object>().Select(id => id.ToString() ?? ""));
            if (AgentCompatMember(lobby, "Players", "ConnectedPlayers") is IEnumerable players)
                return new HashSet<string>(players.Cast<object>()
                    .Select(player => AgentCompatMember(player, "NetId", "netId", "id", "Id")?.ToString())
                    .Where(id => id != null).Select(id => id!));
        }
        catch { }
        return null;
    }

    // The first node under root whose class has this name, found without naming the class at compile time.
    private static Node? AgentCompatFindNode(Node? root, string typeName)
    {
        if (root == null) return null;
        if (root.GetType().Name == typeName) return root;
        foreach (var child in root.GetChildren())
        {
            var found = AgentCompatFindNode(child, typeName);
            if (found != null) return found;
        }
        return null;
    }

    // Calls the first instance method of the given name that takes these arguments; false when the build has none.
    private static bool AgentCompatInvoke(object? target, string name, params object[] args)
    {
        if (target == null) return false;
        try
        {
            var method = target.GetType().GetMethods(AgentCompatFlags).FirstOrDefault(m =>
                m.Name == name && m.GetParameters().Length == args.Length &&
                m.GetParameters().Zip(args, (p, a) => p.ParameterType.IsInstanceOfType(a)).All(ok => ok));
            if (method == null) return false;
            method.Invoke(target, args);
            return true;
        }
        catch (TargetInvocationException) { throw; }
        catch { return false; }
    }
}
