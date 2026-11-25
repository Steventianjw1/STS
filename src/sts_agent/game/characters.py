"""Character identity shared by strategy, evidence and the combat pilot."""

CHARACTERS = ("IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER")
# Who takes elites selectively in every act instead of routing toward them in act 1: the two with the
# least HP and no healing after fights (``strategic.silent``, ``strategic.necrobinder``).
SELECTIVE_ELITES = frozenset({"SILENT", "NECROBINDER"})


def character_key(character: str | None) -> str | None:
    """The game ID of one of the five characters, from that ID or a display name
    ("CHARACTER.SILENT", "The Silent", "silent"); None for anything else."""
    if not isinstance(character, str):
        return None
    key = character.strip().upper().removeprefix("CHARACTER.").removeprefix("THE ")
    return key if key in CHARACTERS else None


def is_silent(character: str | None) -> bool:
    """Recognize the game ID and the display name used by older mod builds."""
    return character_key(character) == "SILENT"


def elites_selectively(character: str | None) -> bool:
    """Whether route evidence leads with the safe and elite-light routes for this character."""
    return character_key(character) in SELECTIVE_ELITES
