"""Add agent previews, deck/map observations and deterministic run setup to STS2MCP,
and make its source build against both game builds the agent supports.

Usage: python tools/patch_mod_preview.py PATH_TO_STS2MCP_SOURCE
Then rebuild/install the mod normally. The patch is idempotent. It adds the agent's
extension files and lines, and it replaces the four upstream lines that stopped
compiling on STS2 v0.111 (StartRunLobby.MaxPlayers, LoadRunLobby.ConnectedPlayerIds)
with reflection helpers from mod/McpMod.AgentCompat.cs, which read the same values
on v0.107.1 and report them as unknown where v0.111 has no such member. Upstream
(Gennadiyev/STS2MCP main, 55e0648) has no v0.111 fix of its own.
"""
from pathlib import Path
import shutil
import sys


def patch(source: Path) -> None:
    builder = source / "McpMod.StateBuilder.cs"
    text = builder.read_text(encoding="utf-8-sig")
    anchor = '        state["description"] = SafeGetCardDescription(card); // hand cards use default pile'
    addition = '        state["combat_previews"] = BuildAgentPreviews(card);'
    changed = False
    if addition not in text:
        if text.count(anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one hand-card description anchor.")
        text = text.replace(anchor, anchor + "\n" + addition)
        changed = True
    deck_anchor = '        state["gold"] = player.Gold;'
    deck_addition = '        state["deck"] = BuildAgentPermanentDeck(player);'
    if deck_addition not in text:
        if text.count(deck_anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one player gold anchor.")
        text = text.replace(deck_anchor, deck_anchor + "\n" + deck_addition)
        changed = True
    keywords_addition = '        state["deck_keywords"] = BuildAgentDeckKeywords(player);'
    if keywords_addition not in text:   # sits right after the deck line added above
        if text.count(deck_addition) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one agent deck line.")
        text = text.replace(deck_addition, deck_addition + "\n" + keywords_addition)
        changed = True
    enemy_anchor = '            ["status"] = BuildPowersState(creature)\n        };'
    enemy_addition = '        AddAgentMoveState(creature, state);'
    if enemy_addition not in text:
        if text.count(enemy_anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one enemy status anchor.")
        text = text.replace(enemy_anchor, enemy_anchor + "\n" + enemy_addition)
        changed = True
    # v0.111: lobby members that no longer exist are read through McpMod.AgentCompat.cs.
    # (old line, new line, how often the old line appears upstream)
    compat = [
        ('["max_players"] = lobby.MaxPlayers,', '["max_players"] = AgentCompatMaxPlayers(lobby),', 1),
        ('info["connected_player_count"] = lobby.ConnectedPlayerIds?.Count ?? 0;',
         'info["connected_player_count"] = AgentCompatConnectedPlayerIds(lobby)?.Count ?? 0;', 1),
        ('var connectedPlayerIds = lobby.ConnectedPlayerIds;',
         'var connectedPlayerIds = AgentCompatConnectedPlayerIds(lobby);', 1),
        ('connectedPlayerIds.Contains(player.NetId) &&', 'connectedPlayerIds.Contains(player.NetId.ToString()) &&', 1),
        ('bool isConnected = lobby.ConnectedPlayerIds?.Contains(sp.NetId) ?? false;',
         'bool isConnected = AgentCompatConnectedPlayerIds(lobby)?.Contains(sp.NetId.ToString()) ?? false;', 1),
    ]
    for old, new, times in compat:
        if new in text:
            continue
        if text.count(old) != times:
            raise SystemExit(f"Unsupported STS2MCP source: expected {times} line(s) reading `{old}`.")
        text = text.replace(old, new)
        changed = True
    if "lobby.MaxPlayers" in text or "lobby.ConnectedPlayerIds" in text:
        raise SystemExit("Unsupported STS2MCP source: a lobby member that v0.111 removed is still read directly.")
    map_anchor = '        state["is_ancient"] = isAncient;'
    map_addition = '        if (isAncient) state["agent_map"] = BuildAgentAncientMap(runState);'
    if map_addition not in text:
        if text.count(map_anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one ancient event anchor.")
        text = text.replace(map_anchor, map_anchor + "\n" + map_addition)
        changed = True
    if changed:
        builder.write_text(text, encoding="utf-8")
    actions = source / "McpMod.Actions.cs"
    action_text = actions.read_text(encoding="utf-8-sig")
    startup_anchor = '        // Game over screen. Prefer the active overlay stack entry so hidden or'
    startup_addition = ('        var startupResult = ExecuteAgentStartupOption(option, tree.Root);\n'
                        '        if (startupResult != null) return startupResult;\n\n')
    if startup_addition not in action_text:
        if action_text.count(startup_anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one menu startup anchor.")
        actions.write_text(action_text.replace(startup_anchor, startup_addition + startup_anchor), encoding="utf-8")
    shutil.copyfile(Path(__file__).resolve().parents[1] / "mod" / "McpMod.AgentPreview.cs",
                    source / "McpMod.AgentPreview.cs")
    shutil.copyfile(Path(__file__).resolve().parents[1] / "mod" / "McpMod.AgentStartup.cs",
                    source / "McpMod.AgentStartup.cs")
    shutil.copyfile(Path(__file__).resolve().parents[1] / "mod" / "McpMod.AgentCompat.cs",
                    source / "McpMod.AgentCompat.cs")
    print("Patched previews, deck and its keyword tooltips, enemy moves, ancient map, run setup and the "
          "v0.111 lobby members. Rebuild and install STS2_MCP.dll.")


if __name__ == "__main__":
    patch(Path(sys.argv[1]))
