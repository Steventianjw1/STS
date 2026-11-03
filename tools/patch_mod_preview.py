"""Add native combat previews to the source-inspected STS2MCP checkout.

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
    if changed:
        builder.write_text(text, encoding="utf-8")
    shutil.copyfile(Path(__file__).resolve().parents[1] / "mod" / "McpMod.AgentPreview.cs",
                    source / "McpMod.AgentPreview.cs")
    print("Native combat previews and permanent deck patched; rebuild and install STS2_MCP.dll.")


if __name__ == "__main__":
    patch(Path(sys.argv[1]))
