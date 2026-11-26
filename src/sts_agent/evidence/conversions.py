"""Which cards make a resource and which cards turn it into damage or Block, by rules over their text.

A scaling card is worth what the deck converts it into: lasting Strength into attacks (an attack that hits
several times uses it on every hit), Dexterity into Block cards, Focus into the Orbs the deck Channels, a
Shiv generator into the Powers that pay per Shiv, Exhaust into the cards that pay per Exhausted card, Poison,
Doom, Vulnerable and Forge into the cards that read them. This counts both halves for each resource the deck
or an offer touches. It is a fact for judgment, not a limit: few converters for many sources means the next
source adds little, and a payoff with no source is a card waiting for one.

The patterns were checked against the v0.111 card text in the Spire Codex catalog (2026-11-26). Counts are
of cards (copies counted), ignore relics, and read the text as observed, so an upgrade's numbers do not matter.
"""
from __future__ import annotations

import re

_MARKUP = re.compile(r"\[/?[A-Za-z_]+(?:[=:][^\]]*)?\]")
_DEAD_TYPES = ("status", "curse")
_I = re.I

# resource -> (what makes it, what converts it, how to read the converters)
# "stat": the converters are ordinary cards that use the stat; "payoff": they are cards that pay per use.
RESOURCES = {
    "Strength": (re.compile(r"\bgain (?:\d+|X) Strength\b(?! this turn)", _I), None, "stat"),
    "Dexterity": (re.compile(r"\bgain (?:\d+|X) Dexterity\b(?! this turn)", _I), None, "stat"),
    "Focus": (re.compile(r"\bgain (?:\d+|X) Focus\b(?! this turn)", _I),
              re.compile(r"\bChannels?\b|\bfor each Channeled Orb\b|\bEvoke\b", _I), "stat"),
    "Shiv": (re.compile(r"\badd\b[^.]*\bShivs?\b", _I),
             re.compile(r"\bShivs? (?:deal|gain|now)\b|\bthe first Shiv\b|\bevery Shiv\b", _I), "payoff"),
    "Exhaust": (re.compile(r"\bExhaust (?:\d+|an?|all|up to|your|X)\b|\bExhaust it\b|\band Exhaust it\b", _I),
                re.compile(r"\bWhenever a card is Exhausted\b|\bin your Exhaust Pile\b|\bExhausted this combat\b"
                           r"|\bif you have Exhausted\b|\bWhen this card is Exhausted\b", _I), "payoff"),
    "Poison": (re.compile(r"\bapply (?:\d+ |X )?Poison\b|\bapply \d+ Poison\b|\bthey apply \d+ Poison\b", _I),
               re.compile(r"\bPoison is triggered\b|\bPoison triggers\b|\bequal to Poison\b|\bhas Poison\b", _I),
               "payoff"),
    "Doom": (re.compile(r"(?<!Whenever you )\bapply (?:\d+ |X )?Doom\b(?! to yourself)|\bapply that much Doom\b|\bApply Doom equal\b"
                        r"|\bapply \d+ Doom to (?:a random enemy|the enemy|ALL enemies)\b", _I),
             re.compile(r"\benemy's Doom\b|\bIf you applied Doom\b|\bWhenever you apply Doom\b|\bas much Doom as HP\b"
                        r"|\bDoom already on\b", _I), "payoff"),
    "Vulnerable": (re.compile(r"\bappl(?:y|ies) (?:\d+|X) (?:Weak and )?Vulnerable\b|\bDouble the enemy's Vulnerable\b",
                              _I),
                   re.compile(r"\bfor each Vulnerable\b|\bVulnerable enemies\b|\bIf the enemy is Vulnerable\b"
                              r"|\bWhenever you apply Vulnerable\b|\bVulnerable and Weak are twice\b", _I), "payoff"),
    "Forge": (re.compile(r"\bForges? \d+\b", _I), re.compile(r"\bSovereign Blade\b", _I), "payoff"),
}
_GAINS_BLOCK = re.compile(r"\bgains? (?:another )?(?:\d+ )?(?:additional )?Block\b|\bBlock equal to\b", _I)
_MULTI_HIT = re.compile(r"\bdamage\b[^.]*\b(?:twice|\d+ times|X times)\b", _I)
_DEALS_DAMAGE = re.compile(r"\bdamage\b", _I)


def _text(card) -> str:
    return _MARKUP.sub("", str(card.get("description") or card.get("card_description") or "")).replace("\n", " ")


def _type(card) -> str:
    return str(card.get("type") or card.get("card_type") or "").strip().lower()


def _cost(card):
    raw = card.get("cost")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def card_parts(card) -> dict:
    """resource -> "source", "converter" or "both", for the resources this one card makes or converts."""
    if not isinstance(card, dict) or _type(card) in _DEAD_TYPES:
        return {}
    text, kind = _text(card), _type(card)
    out = {}
    for resource, (source, converter, how) in RESOURCES.items():
        makes = bool(source.search(text))
        if resource == "Strength":
            uses = kind == "attack" and bool(_DEALS_DAMAGE.search(text))
        elif resource == "Dexterity":
            uses = kind != "power" and bool(_GAINS_BLOCK.search(text))
        else:
            uses = bool(converter.search(text))
        if makes or uses:
            out[resource] = "both" if makes and uses else "source" if makes else "converter"
    return out


def _label(card) -> str:
    return str(card.get("name") or card.get("id") or "?")


def deck_conversions(deck) -> list | None:
    """Per resource the deck makes (or holds a payoff for): its sources and converters. None for an unknown deck.

    A stat's converters (attacks for Strength, Block cards for Dexterity) are counted only when the deck holds a
    source, since every deck holds attacks and Block cards."""
    if not isinstance(deck, list):
        return None
    rows: dict = {}
    multi_hit = cheap = 0
    for entry in deck:
        if not isinstance(entry, dict):
            continue
        n = entry["count"] if type(entry.get("count")) is int and entry["count"] > 0 else 1
        for resource, part in card_parts(entry).items():
            row = rows.setdefault(resource, {"sources": 0, "converters": 0, "source_cards": [], "converter_cards": []})
            if part in ("source", "both"):
                row["sources"] += n
                row["source_cards"].append(_label(entry) + (f" x{n}" if n > 1 else ""))
            if part in ("converter", "both"):
                row["converters"] += n
                row["converter_cards"].append(_label(entry) + (f" x{n}" if n > 1 else ""))
        if _type(entry) == "attack" and _DEALS_DAMAGE.search(_text(entry)):
            multi_hit += n if _MULTI_HIT.search(_text(entry)) else 0
            cheap += n if (_cost(entry) is not None and _cost(entry) <= 1) else 0
    out = []
    for resource in RESOURCES:
        row = rows.get(resource)
        if not row or (RESOURCES[resource][2] == "stat" and not row["sources"]):
            continue
        item = {"resource": resource, "sources": row["sources"], "converters": row["converters"],
                "source_cards": row["source_cards"][:8],
                "converter_cards": row["converter_cards"][:8] if RESOURCES[resource][2] == "payoff"
                or resource == "Focus" else None,
                "reading": _reading(resource, row, multi_hit, cheap)}
        if resource == "Strength":
            item.update({"attacks_hitting_several_times": multi_hit, "attacks_costing_0_or_1": cheap})
        out.append(item)
    return out


def _reading(resource, row, multi_hit, cheap) -> str:
    s, c = row["sources"], row["converters"]
    if resource == "Strength":
        return (f"{s} lasting Strength source(s) against {c} attacks, {multi_hit} of them hitting several times "
                f"(each hit gains the Strength) and {cheap} costing 0 or 1")
    if resource == "Dexterity":
        return f"{s} lasting Dexterity source(s) against {c} cards that gain Block"
    if not s:
        return f"{c} card(s) that pay for {resource} with nothing in the deck that makes it: they wait for a source"
    if not c:
        return (f"{s} card(s) make {resource} and nothing in the deck pays for it: each works on its own, "
                f"and a payoff would multiply them")
    return f"{s} card(s) make {resource} and {c} pay for it"


def offer_conversions(deck_rows, offers) -> list:
    """For each offered card that makes or converts a resource: which, and how much of the other half the deck holds.

    ``deck_rows`` is ``deck_conversions(deck)``; ``offers`` is [(action_id, card details)]. A stat converter
    (an attack, a Block card) is listed only when the deck holds a source of that stat."""
    held = {r["resource"]: r for r in deck_rows or []}
    out = []
    for action_id, card in offers:
        adds = []
        for resource, part in card_parts(card).items():
            row = held.get(resource)
            if part == "converter" and RESOURCES[resource][2] == "stat" and not row:
                continue
            adds.append({"resource": resource, "as": part,
                         "deck_sources": row["sources"] if row else 0,
                         "deck_converters": row["converters"] if row else 0})
        if adds:
            out.append({"action_id": action_id, "name": _label(card), "adds": adds})
    return out


def summary_text(rows) -> str:
    """One bounded paragraph of the deck's conversions, for the planner's opportunity entry."""
    if rows is None:
        return "The permanent deck is not observed: what it makes and converts is unknown."
    if not rows:
        return "The deck makes no lasting Strength, Dexterity or Focus and holds no Shiv, Exhaust, Poison, Doom, " \
               "Vulnerable or Forge cards, so nothing here needs converting."
    parts = []
    for r in rows:
        names = "; makes: " + ", ".join(r["source_cards"]) if r["source_cards"] else ""
        pays = "; converts: " + ", ".join(r["converter_cards"]) if r.get("converter_cards") else ""
        parts.append(f"{r['resource']}: {r['reading']}{names}{pays}")
    return ("What the deck makes and what turns it into damage or Block, by rules over card text (copies counted, "
            "relics ignored; a fact for judgment, not a limit). " + ". ".join(parts) + ".")
