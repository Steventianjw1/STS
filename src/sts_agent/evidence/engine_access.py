"""How often the deck draws the engine its standing plan names, and what one more card or one removal does to it.

The planner names an engine (``standing_plan.engine``): two to six cards and relics the deck wants together. A
card piece is worth what the deck draws of it: adding cards that are not pieces lowers the chance of drawing a
piece early, removing one raises it, and a second copy of a piece raises it most. This computes those chances.

Model: the first pass through a shuffled deck, five cards drawn on turn 1 and five more each later turn, so
turn t has seen min(5t, deck size) cards. Extra draw, Innate, Retain, cards played back onto the draw pile and
the reshuffle are ignored, so a deck with draw finds its pieces sooner than this. Relic pieces are always active.
The chances are exact under that model (hypergeometric, joint by inclusion-exclusion over the pieces).
"""
from __future__ import annotations

from itertools import combinations
from math import comb

DRAW_PER_TURN = 5
TURNS = (1, 2, 3)


def _norm(name) -> str:
    return str(name or "").strip().casefold().removesuffix("+").strip()


def _p_none(size, copies, seen) -> float:
    """Chance that none of ``copies`` cards is among ``seen`` cards of a deck of ``size``."""
    if copies <= 0:
        return 1.0
    if seen >= size or copies > size - seen:
        return 0.0 if copies > 0 and seen > 0 else 1.0
    return comb(size - copies, seen) / comb(size, seen)


def _p_all(size, copies: list, seen) -> float:
    """Chance that every group in ``copies`` has at least one card among ``seen`` cards."""
    total = 0.0
    for r in range(len(copies) + 1):
        for group in combinations(copies, r):
            total += (-1) ** r * _p_none(size, sum(group), seen)
    return max(0.0, min(1.0, total))


def _chances(size, copies: dict) -> dict:
    out = {}
    for t in TURNS:
        seen = min(size, DRAW_PER_TURN * t)
        out[f"by_turn_{t}"] = {
            "each_piece_pct": {name: round(100 * (1 - _p_none(size, k, seen)), 1) for name, k in copies.items()},
            "all_pieces_pct": round(100 * _p_all(size, list(copies.values()), seen), 1) if copies else None}
    return out


def access(engine, deck, relics=None, offers=()) -> dict | None:
    """Draw chances of the engine's card pieces now, with one more non-piece card, with one fewer non-piece card,
    and with each offered card that is a copy of a piece. None when the engine or the deck is unknown or the
    engine has no card piece in the deck. ``offers`` is [(action_id, card details)]."""
    pieces = (engine or {}).get("pieces") if isinstance(engine, dict) else None
    if not pieces or not isinstance(deck, list):
        return None
    counts: dict = {}
    for entry in deck:
        if isinstance(entry, dict):
            n = entry["count"] if type(entry.get("count")) is int and entry["count"] > 0 else 1
            key = _norm(entry.get("name") or entry.get("id"))
            counts[key] = counts.get(key, 0) + n
    size = sum(counts.values())
    relic_names = {_norm(r.get("name") or r.get("id")) for r in relics or [] if isinstance(r, dict)}
    copies, on_relics, unmatched = {}, [], []
    for piece in pieces:
        key = _norm(piece)
        if key in counts:
            copies[str(piece)] = counts[key]
        elif key in relic_names:
            on_relics.append(str(piece))
        else:
            unmatched.append(str(piece))
    if not copies or size <= 0:
        return None
    out = {
        "label": "DRAW CHANCES OF THE STANDING PLAN'S ENGINE (computed, not observed). First pass only: 5 cards on "
                 "turn 1 and 5 more each turn, no extra draw, Innate or Retain, so a deck that draws finds its pieces "
                 "sooner than this. A fact for judging additions, removals and copies, not a limit.",
        "deck_size": size, "card_pieces": copies, "relic_pieces": on_relics,
        "now": _chances(size, copies),
        "one_more_card_not_a_piece": _chances(size + 1, copies),
    }
    if size > sum(copies.values()):
        out["one_removal_not_a_piece"] = _chances(size - 1, copies)
    if unmatched:
        out["pieces_not_found"] = unmatched
    copies_offered = []
    for action_id, card in offers:
        key = _norm((card or {}).get("name") or (card or {}).get("card") or (card or {}).get("id"))
        name = next((p for p in copies if _norm(p) == key), None)
        if name is not None:
            more = {**copies, name: copies[name] + 1}
            copies_offered.append({"action_id": action_id, "piece": name,
                                   "with_this_copy": _chances(size + 1, more)})
    if copies_offered:
        out["offered_copies_of_pieces"] = copies_offered
    return out
