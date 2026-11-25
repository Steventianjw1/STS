"""Each character's version of the strategy: which guidance the planner and the decisions get.

A strategy belongs to a game build (``game.builds``): the text and the numbers in it describe
that build's cards and what its best players' decks hold. ``strategy_for`` answers for the build
in use.

On v0.107.1 Ironclad's version is the shared text itself: it was written from the logged Ironclad
runs and carries no extra guidance. The other four add their own deck guidance and elite stance
(one module each, from the owner's guidance for that character) and read the deck balance with
their own plan and tracks (``evidence.deck_balance``). On v0.111 every character has its own
module. A character none of this knows plays by Ironclad's version.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..game import builds
from ..game.characters import character_key
from .v0107 import defect as defect_0107, necrobinder as necrobinder_0107, regent as regent_0107, silent as silent_0107
from .v0111 import (defect as defect_0111, ironclad as ironclad_0111, necrobinder as necrobinder_0111,
                    regent as regent_0111, silent as silent_0111)


@dataclass(frozen=True)
class CharacterStrategy:
    key: str
    prompt_version: str | None      # None: the shared planner prompt's own version
    deck_guidance: str | None
    elite_guidance: str | None
    act_stance: bool                # whether the shared stance on elites by act applies as well (after elite_guidance)
    own_balance: bool               # whether the deck balance is read with the character's plan and tracks


IRONCLAD = CharacterStrategy("IRONCLAD", None, None, None, act_stance=True, own_balance=False)


def _own(key, module, *, act_stance) -> CharacterStrategy:
    return CharacterStrategy(key, module.PROMPT_VERSION, module.DECK_GUIDANCE, module.ELITE_GUIDANCE,
                             act_stance=act_stance, own_balance=True)


STRATEGIES_BY_BUILD = {
    "v0.107.1": {
        "IRONCLAD": IRONCLAD,
        # The Silent and the Necrobinder take elites selectively in every act: their own stance replaces the shared one.
        "SILENT": _own("SILENT", silent_0107, act_stance=False),
        "NECROBINDER": _own("NECROBINDER", necrobinder_0107, act_stance=False),
        "REGENT": _own("REGENT", regent_0107, act_stance=True),
        "DEFECT": _own("DEFECT", defect_0107, act_stance=True),
    },
    "v0.111.0": {
        "IRONCLAD": _own("IRONCLAD", ironclad_0111, act_stance=True),
        "SILENT": _own("SILENT", silent_0111, act_stance=False),
        "NECROBINDER": _own("NECROBINDER", necrobinder_0111, act_stance=False),
        "REGENT": _own("REGENT", regent_0111, act_stance=True),
        "DEFECT": _own("DEFECT", defect_0111, act_stance=True),
    },
}


def strategies(build=None) -> dict:
    """Every character's strategy for ``build`` (the build in use when None)."""
    return STRATEGIES_BY_BUILD[builds.get(build).key]


def strategy_for(character, build=None) -> CharacterStrategy:
    """The strategy version of ``character`` (a game ID or display name); Ironclad's for anything else."""
    table = strategies(build)
    return table.get(character_key(character) or "IRONCLAD", table["IRONCLAD"])
