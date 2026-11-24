"""Character identity shared by strategy and evidence adapters."""


def is_silent(character: str | None) -> bool:
    """Recognize the game ID and the display name used by older mod builds."""
    if not isinstance(character, str):
        return False
    return character.strip().upper().removeprefix("CHARACTER.").removeprefix("THE ") == "SILENT"
