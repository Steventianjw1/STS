"""Add agent previews, deck/map observations and deterministic run setup to STS2MCP.

Usage: python tools/patch_mod_preview.py PATH_TO_STS2MCP_SOURCE
Then rebuild/install the mod normally. The patch is additive and idempotent.
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
    enemy_anchor = '            ["status"] = BuildPowersState(creature)\n        };'
    enemy_addition = '        AddAgentMoveState(creature, state);'
    if enemy_addition not in text:
        if text.count(enemy_anchor) != 1:
            raise SystemExit("Unsupported STS2MCP source: expected one enemy status anchor.")
        text = text.replace(enemy_anchor, enemy_anchor + "\n" + enemy_addition)
        changed = True
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
    print("Patched previews, deck, enemy moves, ancient map and run setup. Rebuild and install STS2_MCP.dll.")


if __name__ == "__main__":
    patch(Path(sys.argv[1]))
