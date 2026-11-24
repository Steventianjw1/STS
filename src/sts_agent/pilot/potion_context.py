"""Share the agent's observed drop estimate with the advisor during a fight."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..game.game_pin import frozen_copy

logger = logging.getLogger(__name__)


def context_path(pinned_game_dir) -> Path:
    return frozen_copy(pinned_game_dir).resolve() / "mods" / "SpireMentor" / "sts-agent-potion-context.json"


@contextmanager
def publish(path, *, act, floor, drop_chance_pct, starting_potion_count):
    """Publish an atomic snapshot for the lifetime of the delegated fight."""
    if path is None or act is None or floor is None:
        yield
        return

    payload = json.dumps({"act": act, "floor": floor, "drop_chance_pct": drop_chance_pct,
                          "starting_potion_count": starting_potion_count})
    temporary = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, path)
        published = True
    except OSError as exc:
        logger.warning("Pilot potion context unavailable: %s", exc)
    try:
        yield
    finally:
        try:
            if published and path.read_text(encoding="utf-8") == payload:
                path.unlink()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not clear Pilot potion context: %s", exc)


def read(path, *, act, floor) -> dict:
    if path is None or act is None or floor is None:
        return {}
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(context, dict) or (context.get("act"), context.get("floor")) != (act, floor):
        return {}
    return context
