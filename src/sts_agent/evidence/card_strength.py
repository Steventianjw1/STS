"""How strong an offered card is by itself: what the best players do with it, and what the owner knows.

The deck balance (``deck_balance``) says which role a card fills, never whether the card is good. In the
first live v0.111 run that left every card of a wanted role equal: True Grit was bought over Battle
Trance because Block was the lighter side, and cards the best players take one time in seven were added
because they filled nothing and worsened nothing. This module supplies the missing reading:

- ``read`` per offered card, from how often the selected expert players take the card when it is offered
  as a reward (the profile pick rate; at a shop the buy rate is near zero for every card and says nothing,
  so the same reward rate is used there): often_taken, sometimes_taken, rarely_taken, or unknown.
- the owner's notes: what the person running the agent knows about a card. A note outranks the rate, as
  the owner's guidance on a boss outranks the rules over its data (``boss_demands``).
- ``expert``: the grade the best players' v0.111 tier lists give the card, their views and conditions, and the
  cards that do the same job (``expert_grades``). Like the rate it is evidence of general strength, weighed
  against what the deck needs, never a rule.

It belongs to the v0.111 version of the agent (``BUILDS``); the saved v0.107.1 version's frames are as
they were.
"""
from __future__ import annotations

from ..game import builds
from . import expert_grades
from ..game.characters import character_key

BUILDS = ("v0.111.0",)
OFTEN_PCT, RARELY_PCT = 40.0, 20.0      # of the times the card was offered as a reward
STRONG, WEAK = "strong", "weak"

# What the owner said of single cards after watching the first live v0.111 run (2026-11-26), by character
# and card id: (verdict, the reason in their words).
OWNER_NOTES = {
    "IRONCLAD": {
        "BATTLE_TRANCE": (STRONG, "one of the best cards on the character"),
        "UNMOVABLE": (STRONG, "one of the best block engines in the game"),
        "EXPECT_A_FIGHT": (WEAK, "a horrible card"),
        "DISMANTLE": (WEAK, "a horrible card"),
    },
}
# Upgrades the owner ranks: card id -> the note shown with that card's upgrade evidence.
OWNER_UPGRADE_NOTES = {
    "IRONCLAD": {
        "STAMPEDE": "its upgrade costs 1 less energy: a much better upgrade than Bash's",
    },
}
OWNER = "Owner's guidance: "


def active() -> bool:
    """Whether the game build in use carries card strength in its frames."""
    return builds.active().key in BUILDS


def _id(value) -> str:
    return str(value or "").strip().upper().replace(" ", "_").removesuffix("+")


def owner_note(character, card_id) -> tuple | None:
    return (OWNER_NOTES.get(character_key(character) or "") or {}).get(_id(card_id))


def upgrade_note(character, card_id) -> str | None:
    note = (OWNER_UPGRADE_NOTES.get(character_key(character) or "") or {}).get(_id(card_id))
    return OWNER + note if note else None


def read_of(rate_pct) -> str:
    if type(rate_pct) not in (int, float):
        return "unknown"
    return "often_taken" if rate_pct >= OFTEN_PCT else "rarely_taken" if rate_pct < RARELY_PCT else "sometimes_taken"


def _reward_rate(option) -> tuple:
    """(rate_pct, offers) of the reward pick rate in one expert-evidence option row; (None, None) without one."""
    if not isinstance(option, dict):
        return None, None
    row = option if option.get("metric") == "pick_rate" else option.get("reward_pick_rate")
    if not isinstance(row, dict):
        return None, None
    return row.get("rate_pct"), row.get("denominator")


def offers(cards, options, character, deck=None) -> dict | None:
    """The strength reading of each offered card, or None when no card is offered.

    ``cards`` are (action_id, card details) pairs, ``options`` the expert evidence rows for this screen
    (``decision_support.options``; None when the evidence is unavailable), ``deck`` the permanent deck (for the
    same-job comparison)."""
    by_action = {o.get("action_id"): o for o in options or [] if isinstance(o, dict)}
    rows = []
    for action_id, card in cards:
        card_id = card.get("id") or card.get("card_id")
        rate, seen = _reward_rate(by_action.get(action_id))
        row = {"action_id": action_id, "name": card.get("name") or card.get("card_name"),
               "expert_pick_rate_pct": rate, "times_offered": seen, "read": read_of(rate)}
        note = owner_note(character, card_id)
        if note:
            row["read"] = "often_taken" if note[0] == STRONG else "rarely_taken"
            row["owner_note"] = OWNER + note[1]
        expert = expert_grades.offer(character, card, deck) if expert_grades.active() else None
        if expert:
            row["expert"] = expert
        rows.append(row)
    if not rows:
        return None
    return {
        "label": "CARD STRENGTH: how the selected expert players treat each offered card as a reward, the "
                 "owner's notes, and the best players' tier-list grade with the cards that do the same job. Says "
                 "how good the card is by itself, not what this deck lacks: evidence to weigh, not a rule.",
        "basis": f"read is often_taken from {OFTEN_PCT:.0f}% of the times the card was offered as a reward, "
                 f"rarely_taken under {RARELY_PCT:.0f}%, sometimes_taken between, unknown without an adequate "
                 "sample. An owner_note sets read and outranks the rate. Rates pool every game version and are "
                 "not conditioned on the deck. expert: " + expert_grades.BASIS + " same_job lists, for each job "
                 "the card does, the deck's cards that already do it and stronger cards for it that may be offered "
                 "later.",
        "cards": rows,
    }
