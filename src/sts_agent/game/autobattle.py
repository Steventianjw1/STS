"""Native Autobattle bridge: the one part of Spire Mentor this agent uses.

The Spire Mentor mod (tfriedel/spire-mentor, MIT) can play a whole fight by
itself. The agent's mod extension (``mod/McpMod.AgentStartup.cs``) exposes that
through STS2MCP's ``menu_select`` action on the game thread; this class only
sends those commands. Nothing else of Mentor is read: no advice, no logs, no
card or path suggestions.
"""
from __future__ import annotations

from .client import GameClientError

COMMANDS = ("status", "on", "off", "hold_offers", "ai_pick")


class MentorAutobattle:
    """Control native Autobattle through STS2MCP's game-thread bridge."""

    def __init__(self, client):
        self.client = client

    def control(self, command: str) -> dict:
        """``hold_offers`` makes Mentor hold in-fight choose-a-card screens (its ``pause_on``);
        ``ai_pick`` has Mentor play its own recommendation on the screen it is holding, once."""
        if command not in COMMANDS:
            raise ValueError("Unknown Autobattle command")
        response = self.client.post_action({"action": "menu_select", "option": f"agent_autobattle_{command}"})
        body = response.body
        if not response.accepted or not isinstance(body, dict) or body.get("autobattle_version") != 1:
            raise GameClientError(f"Spire Mentor Autobattle unavailable: {response.error or body}. "
                                  "Rebuild/install the agent mod extension and enable Spire Mentor.")
        return body
