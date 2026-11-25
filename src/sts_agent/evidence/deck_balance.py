"""Deck balance: how the cards added to the deck spread over damage, Block and the scaling of each.

A deck that wins needs four things at once and not too much of any one: damage, Block, something
that makes its damage grow during a fight, and something that makes its Block grow or repeat.
This module counts the observed deck in those four roles and says which are missing, short,
over-supplied or full, and what each offered card would do to that picture.

What the deck should take first changes over the run (``phase``). The first picks of act 1 go to
damage: the starter deck kills too slowly for the act 1 elites and boss, and a simulator check of
drafting (synthetic, act 1 fights only) found a damage-first deck winning as often as a balanced
one, while skipping cards to stay balanced lost most act 1 boss fights. From act 2 on, enemies hit
hard enough that Block scaling and a thin deck decide fights, so the balance rules are applied
strictly there.

The counts are explicit rules over each card's own type and text as the game shows it; they need
no catalog and no network. They count cards, not strength: relics, upgrades and card quality are
not weighed, so the result is evidence for a judgment and never a score.
"""
from __future__ import annotations

import re

FRONT = ("damage", "block")
SCALING = ("damage_scaling", "block_scaling")
ROLES = FRONT + SCALING
# A front-line role is over-supplied once it holds FRONT_MIN added cards and FRONT_LEAD more than
# the other; before FRONT_MIN the deck is too small for a lead to mean anything.
FRONT_MIN, FRONT_LEAD = 4, 2
SCALING_FULL = 2            # lasting sources of one kind of scaling beyond which another adds little
MAX_NAMES = 10              # card names listed per role
EARLY_PICKS = 3             # act 1 cards added beyond the starter before damage stops coming first

EARLY, BUILD, LATE = "early_damage", "act1_build", "late_purity"
PHASE_RULES = {
    EARLY: f"the first {EARLY_PICKS} cards added in act 1: damage and damage scaling come first and are never "
           "over-supplied; a card of another role is taken only when it is clearly the strongest card offered",
    BUILD: "the rest of act 1: fill missing or short roles first and skip cards that worsen the balance; a solid "
           "neutral card is still worth taking, because the deck is still too small to win act 1 boss fights",
    LATE: "act 2 onwards: Block scaling comes first while it is missing, then the other missing or short roles; "
          "skip cards that worsen the balance and, for a thin deck, skip neutral cards unless clearly strong",
}

_MARKUP = re.compile(r"\[/?[A-Za-z_]+(?:[=:][^\]]*)?\]")
_GAINS_BLOCK = re.compile(r"\bgains? (?:another )?(?:\d+ )?(?:additional )?Block\b|\bdouble your Block\b", re.I)
_STRENGTH = re.compile(r"\bgain \d+ Strength\b(?! this turn)", re.I)          # lasting Strength only
_RAMPS = re.compile(r"\bincrease this card's damage\b", re.I)
_BLOCK_STAT = re.compile(r"\bgain \d+ (?:Dexterity|Plating)\b(?! this turn)", re.I)
# A Power stays for the fight, so one that deals damage, raises damage taken or replays Attacks
# makes damage grow; one that speaks of Block makes Block grow or repeat.
_POWER_DAMAGE = re.compile(r"\bdeals? [^.]{0,30}\bdamage\b|\benem(?:y|ies) takes? [^.]*\bdamage\b|\bAttacks?\b"
                           r"|\bplayed against\b|\bPoison\b|\bShivs?\b", re.I)
# Damage that does not come from the card's own hit: generated Shivs and applied Poison.
_INDIRECT_DAMAGE = re.compile(r"\bShivs?\b|\bappl(?:y|ies) \d+ Poison\b", re.I)
_POWER_BLOCK = re.compile(r"\bBlock\b", re.I)
_DEALS_DAMAGE = re.compile(r"\bdeals? [^.]{0,30}\bdamage\b", re.I)
_DEAD_TYPES = ("curse", "status")

RULES = {
    "counted": "cards added beyond the starter (rarity not Basic); curses and statuses are counted apart; a card "
               "can hold more than one role, and a card with none is listed under other (draw, energy, debuffs)",
    "damage": "an Attack that deals damage, or a card that adds Shivs or applies Poison",
    "block": "a card that is not a Power and gains Block",
    "damage_scaling": "lasting Strength gain, an attack that raises its own damage, or a Power that deals damage, "
                      "raises the damage enemies take or replays Attacks",
    "block_scaling": "lasting Dexterity or Plating, or a Power that gains, keeps or multiplies Block",
    "front_line_status": f"damage and block: over_supplied when a role holds at least {FRONT_MIN} added cards and at "
                         f"least {FRONT_LEAD} more than the other; the other is then short; missing when it holds "
                         "none; otherwise balanced",
    "scaling_status": f"missing with no source, covered with one, full with {SCALING_FULL} or more",
    "limits": "counts cards, not strength: relics, upgrades and card quality are not weighed",
}


def _field(card, key):
    value = card.get(key)
    return value if value is not None else card.get("card_" + key)


def card_roles(card) -> tuple:
    """The roles one card holds, read from its type and text. Empty for support cards, curses and statuses."""
    kind = str(_field(card, "type") or "").strip().lower()
    text = _MARKUP.sub("", str(_field(card, "description") or ""))
    if kind in _DEAD_TYPES:
        return ()
    power, roles = kind == "power", []
    if (kind == "attack" and re.search(r"\bdamage\b", text, re.I)) or (not power and _INDIRECT_DAMAGE.search(text)):
        roles.append("damage")
    if not power and _GAINS_BLOCK.search(text):
        roles.append("block")
    if _STRENGTH.search(text) or _RAMPS.search(text) or (power and _POWER_DAMAGE.search(text)):
        roles.append("damage_scaling")
    if _BLOCK_STAT.search(text) or (power and _POWER_BLOCK.search(text) and not _DEALS_DAMAGE.search(text)):
        roles.append("block_scaling")
    return tuple(roles)


def _statuses(counts) -> dict:
    out = {}
    for role, other in (FRONT, FRONT[::-1]):
        mine, theirs = counts[role], counts[other]
        out[role] = ("over_supplied" if mine >= FRONT_MIN and mine - theirs >= FRONT_LEAD else
                     "missing" if mine == 0 else
                     "short" if theirs >= FRONT_MIN and theirs - mine >= FRONT_LEAD else "balanced")
    for role in SCALING:
        out[role] = "missing" if counts[role] == 0 else "covered" if counts[role] < SCALING_FULL else "full"
    return out


def phase(act, added) -> str:
    """Which deck-building phase applies: by act, and in act 1 by the cards added beyond the starter.
    An unknown act is read as act 1."""
    if act is not None and act >= 2:
        return LATE
    return EARLY if added < EARLY_PICKS else BUILD


def _priority(stage, status) -> list:
    """The roles to take first in this phase, most urgent first."""
    if stage == EARLY:
        return ["damage", "damage_scaling"]
    short = [r for r in ROLES if status[r] in ("missing", "short")]
    if stage == LATE and "block_scaling" in short:
        short = ["block_scaling"] + [r for r in short if r != "block_scaling"]
    return short


def balance(deck, act=None) -> dict | None:
    """Role counts, statuses and the phase's priorities for an observed permanent deck; None when the
    deck is unknown. ``act`` is the current act number when known."""
    if not isinstance(deck, list):
        return None
    counts = {role: 0 for role in (*ROLES, "other")}
    names = {role: [] for role in counts}
    starter, dead, size, added = {role: 0 for role in FRONT}, 0, 0, 0
    for entry in deck:
        if not isinstance(entry, dict):
            continue
        n = entry.get("count") if type(entry.get("count")) is int and entry["count"] > 0 else 1
        size += n
        if str(entry.get("type") or "").strip().lower() in _DEAD_TYPES:
            dead += n
            continue
        roles = card_roles(entry)
        if str(entry.get("rarity") or "").strip().lower() == "basic":
            for role in roles:
                if role in starter:
                    starter[role] += n
            continue
        added += n
        label = str(entry.get("name") or entry.get("id") or "?") + (f" x{n}" if n > 1 else "")
        for role in roles or ("other",):
            counts[role] += n
            names[role].append(label)
    status = _statuses(counts)
    stage = phase(act if type(act) is int else None, added)
    sides = {"damage": counts["damage"] + counts["damage_scaling"], "block": counts["block"] + counts["block_scaling"]}
    return {
        "label": "DECK BALANCE: the cards added beyond the starter, counted by what they do. Rules over card text, "
                 "not a strength score.",
        "phase": stage, "phase_rule": PHASE_RULES[stage], "priority": _priority(stage, status),
        "deck_size": size, "curses_and_statuses": dead, "starter_cards": starter, "cards_added": added,
        "added": {role: {"count": counts[role], "cards": sorted(names[role])[:MAX_NAMES]} for role in counts},
        "status": status,
        "short": [r for r in ROLES if status[r] in ("missing", "short")],
        "over_supplied": [r for r in ROLES if status[r] in ("over_supplied", "full")],
        "lighter_side": None if sides["damage"] == sides["block"] else min(sides, key=sides.get),
        "rules": RULES,
    }


def offer_effects(state, offers) -> list:
    """What taking each offered card would do to the balance.

    ``offers`` are (action_id, card) pairs. ``adds_to_over_supplied`` is judged on the deck as it
    would be after the pick, so the card that tips a role over is marked, not only the ones after it.
    In the early phase damage and its scaling count as needed and never as over-supplied.
    ``fills_priority`` marks a card for the phase's most urgent role.
    """
    if not state:
        return []
    counts = {role: state["added"][role]["count"] for role in ROLES}
    early = state.get("phase") == EARLY
    priority = state.get("priority") or []
    top = set(priority) if early else set(priority[:1])
    out = []
    for action_id, card in offers:
        roles = card_roles(card)
        after = {role: counts[role] + (role in roles) for role in ROLES}
        after_status = _statuses(after)
        over = [r for r in roles if not (early and r in priority)
                and (after_status[r] == "over_supplied" if r in FRONT else counts[r] >= SCALING_FULL)]
        fills = [r for r in roles if (early and r in priority) or state["status"][r] in ("missing", "short")]
        out.append({"action_id": action_id, "name": _field(card, "name"), "roles": list(roles) or ["other"],
                    "fills_short": fills, "fills_priority": bool(top.intersection(roles)),
                    "adds_to_over_supplied": over,
                    "effect": ("mixed" if fills and over else "improves" if fills else "worsens" if over else
                               "neutral")})
    return out


def summary_text(state) -> str:
    """One compact paragraph for a bounded text field: every role's count, cards and status."""
    if not state:
        return "The permanent deck is not observed: its balance is unknown."
    parts = []
    for role in ROLES:
        row = state["added"][role]
        cards = f" ({', '.join(row['cards'])})" if row["cards"] else ""
        parts.append(f"{role.replace('_', ' ')} {row['count']}{cards}: {state['status'][role].replace('_', ' ')}")
    other = state["added"]["other"]
    start = state["starter_cards"]
    # The verdict leads, so a bounded field that cuts the end loses detail and never the conclusion.
    return (f"Phase {state['phase']}: {state['phase_rule']}; take first: "
            f"{', '.join(state['priority']).replace('_', ' ') or 'nothing in particular'}. Missing or short: {', '.join(state['short']).replace('_', ' ') or 'none'}. Over-supplied or full: "
            f"{', '.join(state['over_supplied']).replace('_', ' ') or 'none'}. Lighter side: "
            f"{state['lighter_side'] or 'neither'}. Cards added beyond the starter, by rules over their text (a "
            f"card can hold two roles; relics and card quality are not weighed): {'; '.join(parts)}. Other added "
            f"cards (draw, energy, debuffs): {other['count']}. Starter cards still held: {start['damage']} attacks, "
            f"{start['block']} Block cards. Deck size {state['deck_size']}, of which "
            f"{state['curses_and_statuses']} curses or statuses. Status rules: damage and Block are over-supplied "
            f"at {FRONT_MIN} or more added cards and {FRONT_LEAD} or more ahead of the other; a scaling role is "
            f"covered by one lasting source and full at {SCALING_FULL}.")
