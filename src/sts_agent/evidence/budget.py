"""Two budgets a choice between fights spends: HP the deck pays on purpose, and time before the next hard test.

``hp_costs`` lists the cards and relics whose own text makes the player lose HP. That HP comes back only at
rest sites, so it belongs in every choice between resting and upgrading and in every route.

``bet_window`` says whether the position can afford a card that does little until another kind of card
arrives (a payoff taken before its enabler). Strong players do take such cards, and say when: the deck
already survives without the card, the enabler is common, the payoff is large and the route has rest sites
or shops before the next hard test. An earlier version of this agent took HP-cost and synergy cards at low
HP on the strength of cards it did not hold, so the gate is deliberately narrow and is checked here, not
left to the model: act 1 or early act 2, damage and Block both among the added cards, at least two thirds
of HP left, and a rest site that can be reached without fighting an elite. The two conditions it cannot
check (how large the payoff is, how common the enabler) stay with the model.

Both are rules over the text and facts the game shows. They are evidence for a judgment: neither picks a
card, and neither weighs relics, upgrades or card quality. They belong to the game builds in ``BUILDS``; a
saved earlier version of the agent keeps its frames as they were.
"""
from __future__ import annotations

import re

from ..game import builds
from .deck_balance import NEEDS, _text

BUILDS = ("v0.111.0",)
BET_ACTS = (1, 2)
BET_MIN_HP = 2 / 3          # share of max HP below which no bet is open
BET_ACT2_ROOMS = 8          # act 2 is early while at least this many rooms come before its boss on every route

_MARKUP = re.compile(r"\[/?[A-Za-z_]+(?:[=:][^\]]*)?\]")
# "Lose 6 HP" is the player's own cost. "Enemies lose 5 HP" and "it loses 3 HP" are damage, and "Lose 2 Max HP"
# is a lasting cost that the prompts price over the rest of the run, not over the fights before a rest site.
_LOSE_HP = re.compile(r"(?<!enemy )(?<!enemies )(?<!they )(?<!it )\blose (\d+) HP\b", re.I)


def active() -> bool:
    """Whether the game build in use carries these budgets in its frames."""
    return builds.active().key in BUILDS


def hp_costs(deck, relics) -> dict | None:
    """The cards and relics whose text costs the player HP; None when none does or the deck is not observed."""
    if not isinstance(deck, list):
        return None
    cards = []
    for entry in deck:
        found = _LOSE_HP.search(_text(entry)) if isinstance(entry, dict) else None
        if found:
            copies = entry.get("count") if type(entry.get("count")) is int and entry["count"] > 0 else 1
            cards.append({"name": entry.get("name") or entry.get("id"), "copies": copies,
                          "hp_each_play": int(found.group(1))})
    held = []
    for relic in relics if isinstance(relics, list) else []:
        text = _MARKUP.sub("", str(relic.get("description") or "")) if isinstance(relic, dict) else ""
        found = _LOSE_HP.search(text)
        if found:
            held.append({"name": relic.get("name") or relic.get("id"), "hp": int(found.group(1)), "text": text[:200]})
    if not cards and not held:
        return None
    return {"label": "HP THIS DECK PAYS ON PURPOSE: cards and relics whose own text makes you lose HP. Rules over "
                     "visible text; a relic's text says when it is paid.",
            "cards": cards, "relics": held,
            "hp_if_each_copy_is_played_once": sum(c["copies"] * c["hp_each_play"] for c in cards),
            "note": "This HP comes back only at rest sites or through healing. Count a card once a fight when it "
                    "Exhausts and once for every pass through the deck otherwise, over the fights before the next "
                    "rest site."}


def bet_window(state, run, player, route) -> dict:
    """Whether a payoff may be taken before its enabler in this position.

    ``state`` is ``deck_balance.balance``'s result (None when the deck is not observed), ``run`` and ``player``
    the frame's facts, ``route`` the map beyond the current room as the coordinator gives it (None when the
    rooms ahead are not observed). Every condition that fails is named, so the reason for a closed window
    can be read back from a trace."""
    closed = []
    act = run.get("act") if isinstance(run, dict) else None
    ahead = (route or {}).get("ahead_over_all_continuations") or {}

    def lowest(key, default=0):
        span = ahead.get(key)
        return span.get("min", default) if isinstance(span, dict) else default

    if act not in BET_ACTS:
        closed.append("act 3: too few fights remain for a card that pays off later" if act == 3 else "the act is unknown")
    elif act == 2 and route and lowest("rooms_before_boss") < BET_ACT2_ROOMS:
        closed.append(f"act 2 is past its early rooms: a route has fewer than {BET_ACT2_ROOMS} rooms before the boss")
    if not state:
        closed.append("the deck is not observed")
    else:
        lacking = [role for role in ("damage", "block") if state["status"][role] in NEEDS]
        if lacking:
            closed.append(" and ".join(lacking).replace("block", "Block")
                          + " missing or short among the added cards: fill that first")
    hp, top = (player.get("hp"), player.get("max_hp")) if isinstance(player, dict) else (None, None)
    if type(hp) is not int or type(top) is not int or top <= 0:
        closed.append("HP is unknown")
    elif hp < BET_MIN_HP * top:
        closed.append(f"HP {hp} of {top} is under two thirds")
    if not route:
        closed.append("the rooms ahead are not observed")
    elif route.get("next_is_boss"):
        closed.append("the boss is the next room")
    else:
        if ahead.get("elite_before_next_rest_or_boss") == "on_every_route":
            closed.append("an elite stands before the next rest site on every route")
        rests = ahead.get("rest_sites_before_boss")
        if not isinstance(rests, dict) or rests.get("max", 0) < 1:
            closed.append("no rest site remains before the boss")
    return {"label": "PAYOFF BET WINDOW: whether this position can afford a card that does little until another kind "
                     "of card arrives. Rules over observed facts; it opens or closes an exception and never "
                     "recommends a card.",
            "open": not closed, "closed_by": closed,
            "rule": f"open in act 1 or early act 2 (at least {BET_ACT2_ROOMS} rooms before the boss), with neither "
                    "damage nor Block missing or short among the added cards, at least two thirds of HP, and a "
                    "rest site that some route reaches before an elite"}
