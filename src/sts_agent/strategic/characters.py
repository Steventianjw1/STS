"""Each character's version of the strategy: which guidance the planner and the decisions get.

Ironclad's version is the shared text itself: it was written from the logged Ironclad runs and
carries no extra guidance. The other four add their own deck guidance and elite stance (one
module each, from the owner's guidance for that character) and read the deck balance with their
own plan and tracks (``evidence.deck_balance``). A character none of this knows plays by
Ironclad's version.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..game.characters import character_key
from . import defect, necrobinder, regent, silent


@dataclass(frozen=True)
class CharacterStrategy:
    key: str
    prompt_version: str | None      # None: the shared planner prompt's own version
    deck_guidance: str | None
    elite_guidance: str | None
    act_stance: bool                # whether the shared stance on elites by act applies as well (after elite_guidance)
    own_balance: bool               # whether the deck balance is read with the character's plan and tracks


IRONCLAD = CharacterStrategy("IRONCLAD", None, None, None, act_stance=True, own_balance=False)
STRATEGIES = {
    "IRONCLAD": IRONCLAD,
    # The Silent and the Necrobinder take elites selectively in every act: their own stance replaces the shared one.
    "SILENT": CharacterStrategy("SILENT", silent.PROMPT_VERSION, silent.DECK_GUIDANCE, silent.ELITE_GUIDANCE,
                                act_stance=False, own_balance=True),
    "NECROBINDER": CharacterStrategy("NECROBINDER", necrobinder.PROMPT_VERSION, necrobinder.DECK_GUIDANCE,
                                     necrobinder.ELITE_GUIDANCE, act_stance=False, own_balance=True),
    "REGENT": CharacterStrategy("REGENT", regent.PROMPT_VERSION, regent.DECK_GUIDANCE, regent.ELITE_GUIDANCE,
                                act_stance=True, own_balance=True),
    "DEFECT": CharacterStrategy("DEFECT", defect.PROMPT_VERSION, defect.DECK_GUIDANCE, defect.ELITE_GUIDANCE,
                                act_stance=True, own_balance=True),
}


def strategy_for(character) -> CharacterStrategy:
    """The strategy version of ``character`` (a game ID or display name); Ironclad's for anything else."""
    return STRATEGIES.get(character_key(character) or "IRONCLAD", IRONCLAD)
