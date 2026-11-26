"""What a boss fight asks of the deck: rules over the boss's reference data, plus owner guidance.

Every demand names the capability the fight rewards, the kind of card that supplies it, and why:
either the data a rule read (a monster count, a power's own description, a move's listed effect)
or, where marked, guidance from the person running the agent, who knows what the reference data
omits. Guidance wins over the rules: it can add a need and drop one a rule produced. Neither is
a strength score: both ignore relics, potions and synergies, and a fight with no entry has no
*known* special demand, which is not the same as having none.
"""
from __future__ import annotations

import re

from ..game.characters import character_key
from .elite_readiness import _cid, card_traits

# need -> the kind of card that supplies it
TAKE = {
    "aoe": "attacks that hit ALL enemies",
    "multi_hit": "attacks that hit several times, and several cheap attacks rather than one big hit",
    "fast_scaling": "damage that grows quickly inside the fight (Strength gain, damage Powers, cards that ramp "
                    "every turn) and is in play early; slow or late setup does not count",
    "block": "strong non-starter Block cards",
    "weak": "cards that apply Weak, to cut its attacks while the deck works",
    "draw": "extra card draw",
    "big_cards": "a few big attacks and big Block cards rather than many small ones",
    "exhaust": "cards that Exhaust other cards (they remove the added cards for the rest of the fight), "
               "and extra draw to get past them",
    "attack_based": "Block, draw and setup that come from Attacks and Powers rather than Skills",
    "single_card_impact": "cards that do a lot on their own (one big Block, one big hit); a hand of cheap cards "
                          "does little on the affected turns",
    "scaling": "damage that keeps growing over a long fight (Strength, Powers) rather than front-loaded burst only",
    "artifact": "one of two answers, chosen by what the deck already is: EITHER enough cheap debuff applications "
                "to strip its Artifact and then land the ones that matter (for a deck already built on Vulnerable "
                "or Weak; deck_cards lists its debuff sources), OR damage and Block that work without any debuff "
                "landing (for a deck with few debuff sources). Do not half-do both",
    "avoid_exhaust": "no cards whose purpose is to Exhaust the cards this boss adds; answer it with damage instead",
}
# need -> the card_traits flag that supplies it; a need without one is advice that card data cannot check
TRAIT = {"aoe": "aoe_attack", "multi_hit": "multi_hit_attack", "exhaust": "exhausts_other_cards",
         "fast_scaling": "strength_gain", "block": "non_basic_block", "weak": "applies_weak", "draw": "draws_cards",
         "artifact": "applies_debuff"}
# Needs with two valid answers: the matching deck cards are listed, but "met" stays undecided.
_EITHER_OR = {"artifact"}
# A character whose damage grows through its own mechanic rather than Strength: the cards that speak of
# that mechanic are listed as possible scaling pieces, and whether they add up is left to judgment.
_SCALING_NEEDS = {"fast_scaling", "scaling"}
_SCALING_TEXT = {
    "SILENT": re.compile(r"\b(?:shivs?|poison)\b", re.I),
    "NECROBINDER": re.compile(r"\b(?:doom|osty|souls?)\b", re.I),
    "REGENT": re.compile(r"\b(?:forges?|sovereign blade)\b|\b(?:gain|spend)[^.]*\bstars?\b", re.I),
    "DEFECT": re.compile(r"\b(?:focus|orbs?|lightning|dark)\b|\bincrease the damage of ALL\b", re.I),
}
_SCALING_BASIS = "Possible scaling pieces. Judge the deck's generators, payoffs, and setup time together."

# The game's own power descriptions, matched by what they say rather than by power name, so the
# same rule fires on any monster that carries a power with that effect.
_POWER_RULES = (
    ("multi_hit", re.compile(r"only loses? 1 HP|(?:damage taken|HP loss)[^.]* to 1\b", re.I)),
    ("artifact", re.compile(r"negates? [^.]*debuff", re.I)),
    ("attack_based", re.compile(r"whenever you play a skill", re.I)),
    ("single_card_impact", re.compile(r"only play \d+ cards?", re.I)),
    ("scaling", re.compile(r"\brevives?\b", re.I)),
    ("aoe", re.compile(r"when an ally dies", re.I)),
    ("exhaust", re.compile(r"\b(?:shuffle|add)\b[^.]*\binto your\b", re.I)),
)
_ADDS_CARD = re.compile(r"^Add (.+?) to (?:your )?(discard|draw|hand|deck)", re.I)
_APPLIES = re.compile(r"^Apply (\w+)$")
_MAX_WHY = 3
# Listed in this order: what decides which cards to take first, cautions last.
_ORDER = ("aoe", "multi_hit", "fast_scaling", "block", "weak", "draw", "big_cards", "exhaust", "attack_based",
          "single_card_impact", "scaling", "artifact", "avoid_exhaust")

OWNER = "Owner's guidance: "
# Per boss encounter: needs the person running the agent adds (with their reason) and rule-derived
# needs they drop. Kept as given; the reasons are theirs, not read from the reference data.
_GUIDANCE = {
    "KNOWLEDGE_DEMON_BOSS": {"add": {
        "fast_scaling": "this fight is won by fast damage scaling",
        "block": "it also needs real Block while the damage ramps"}},
    "LAGAVULIN_MATRIARCH_BOSS": {"add": {
        "fast_scaling": "this fight is won by fast damage scaling",
        "big_cards": "bigger attacks and bigger Block cards are worth more here than several smaller ones"}},
    "WATERFALL_GIANT_BOSS": {"add": {
        "fast_scaling": "this fight is won by fast damage scaling",
        "block": "it needs Block alongside the damage",
        "weak": "Weak on it does the same job as Block here"}},
    "SOUL_FYSH_BOSS": {"drop": ("multi_hit",), "add": {
        "fast_scaling": "multi-hit attacks are not what this fight needs; fast damage scaling is"}},
    "THE_INSATIABLE_BOSS": {"drop": ("exhaust",), "add": {
        "fast_scaling": "the real requirement is fast damage scaling, before its Frantic Escape cards get too "
                        "expensive to keep playing",
        "avoid_exhaust": "exhausting the cards it adds can get you consumed and killed"}},
    "QUEEN_BOSS": {"add": {
        "draw": "her binding ties up the first cards drawn each turn, so the deck needs more draw to play through it"}},
    "KAISER_CRAB_BOSS": {"add": {
        "fast_scaling": "the second claw has to die before its big hit, which takes scaling damage",
        "weak": "Weak blunts the claws' hits while the first one is being killed"}},
}


def fight_demands(monsters, encounter_id=None) -> list | None:
    """Demands of one boss fight from its monsters' ``MonsterReference.profile`` entries.

    None when the fight is neither in the reference data nor in the guidance (unknown); an empty
    list when it is known and nothing fires. Each demand: ``need``, ``take`` (the kind of card
    that supplies it) and ``why`` (at most three reasons: the data a rule read, or owner guidance).
    """
    guidance = _GUIDANCE.get(_cid(encounter_id) or "", {})
    known = [m for m in monsters or [] if isinstance(m, dict) and not m.get("missing")]
    if not known and not guidance:
        return None
    why: dict[str, list] = {}

    def add(need, reason):
        reasons = why.setdefault(need, [])
        if reason not in reasons:
            reasons.append(reason)

    total = sum(m.get("count") or 1 for m in monsters or [] if isinstance(m, dict))
    if total > 1:
        names = ", ".join(str(m.get("name")) + (f" x{m['count']}" if m.get("count") else "")
                          for m in monsters if isinstance(m, dict))
        add("aoe", f"{total} monsters fought together: {names}")
    for m in known:
        name, glossary = m.get("name"), m.get("power_glossary") or {}
        sources = {p: "starts with" for p in m.get("powers_on_spawn") or []}
        for move in m.get("moves") or []:
            effects = move["effects"] if isinstance(move.get("effects"), list) else []
            for effect in effects:
                found = _ADDS_CARD.match(str(effect))
                if found:
                    add("exhaust", f"{name}'s move {move['title']} adds {found.group(1)} to your {found.group(2).lower()} pile")
                applied = _APPLIES.match(str(effect))
                if applied:
                    sources.setdefault(applied.group(1), f"applies with its move {move['title']}")
            if (any(str(i.get("type")).lower() == "status" for i in move.get("intents") or [])
                    and not any(_ADDS_CARD.match(str(e)) for e in effects)):
                add("exhaust", f"{name}'s move {move['title']} has a status intent (it adds a card to your piles; "
                               "which card is not in the reference data)")
        for power, source in sources.items():
            text = glossary.get(power)
            if not text:
                continue
            for need, rule in _POWER_RULES:
                if rule.search(text):
                    add(need, f"{name} {source} {power}: {text}")
    for need in guidance.get("drop") or ():
        why.pop(need, None)
    for need, reason in (guidance.get("add") or {}).items():
        why.setdefault(need, []).insert(0, OWNER + reason)   # their reason leads
    return [{"need": need, "take": TAKE[need], "why": why[need][:_MAX_WHY]} for need in _ORDER if need in why]


def demand_text(demands, budget: int = 950) -> str:
    """Compact text of a fight's demands for a bounded field; every need survives, reasons shrink first."""
    if demands is None:
        return "The boss is not in the reference data: what it asks of the deck is unknown."
    if not demands:
        return ("Nothing known asks for a special deck against this boss, beyond enough damage for its HP and "
                "Block for its largest hit.")
    head = ("From rules over the boss's reference data and, where marked, the run owner's guidance; relics and "
            "potions are not counted. For this fight, in the deck built during this act:")

    def render(max_reasons, with_take):
        out = head
        for d in demands:
            reasons = "; ".join(str(w).rstrip(".") for w in d["why"][:max_reasons])
            out += f" [{d['need']}] " + (f"take {d['take']}. Because: {reasons}." if with_take else f"{reasons}.")
        return out
    for max_reasons, with_take in ((_MAX_WHY, True), (1, True), (1, False)):
        text = render(max_reasons, with_take)
        if len(text) <= budget:
            return text
    return text[:budget]


def _matches(need, card, traits, own_scaling):
    """``own_scaling``: the text rule of a character whose scaling is its own mechanic, else None."""
    if own_scaling is not None and need in _SCALING_NEEDS:
        return traits["strength_gain"] or bool(own_scaling.search(card.get("description") or ""))
    return traits.get(TRAIT.get(need), False)


def _deck_cards(deck, cards, need, own_scaling):
    names = []
    for entry in deck:
        card = cards.get(_cid(entry.get("id")))
        if card and _matches(need, card, card_traits(card, entry.get("upgraded", entry.get("is_upgraded")) is True),
                             own_scaling):
            names.append(card.get("name") or entry.get("id"))
    return sorted(set(names))


def coverage(demands, deck, cards, *, character=None) -> list | None:
    """Per demand, the deck cards that supply it by catalog traits. ``met`` None = not decided here."""
    if demands is None:
        return None
    own_scaling = _SCALING_TEXT.get(character_key(character))
    out = []
    for d in demands:
        need = d["need"]
        by_mechanic = own_scaling is not None and need in _SCALING_NEEDS
        if need not in TRAIT and not by_mechanic:
            out.append({"need": need, "deck_cards": None, "met": None, "basis": "advice only: card data cannot check it"})
        elif deck is None:
            out.append({"need": need, "deck_cards": None, "met": None, "basis": "deck unknown"})
        elif by_mechanic:
            names = _deck_cards(deck, cards, need, own_scaling)
            out.append({"need": need, "deck_cards": names, "met": None, "basis": _SCALING_BASIS})
        elif need in _EITHER_OR:
            out.append({"need": need, "deck_cards": _deck_cards(deck, cards, need, own_scaling), "met": None,
                        "basis": "two valid answers: deck_cards are the deck's debuff sources; many favour "
                                 "stripping, few favour not relying on debuffs"})
        else:
            names = _deck_cards(deck, cards, need, own_scaling)
            out.append({"need": need, "deck_cards": names, "met": bool(names)})
    return out


# The needs a card's own text shows, for cards whose catalog entry has not been read (the offers on a screen and the
# observed deck). Only these two, because deck_balance counts both as plain damage: an attack that hits ALL enemies
# and one that hits several times fill the same role there, so a deck full of single hits reads as over-supplied
# for a boss that one of them answers.
_TEXT_NEEDS = {
    "aoe": re.compile(r"\bdamage\b[^.]*\bto ALL enemies\b|\bALL enemies\b[^.]*\bdamage\b"),
    "multi_hit": re.compile(r"\bdamage\b[^.]*\b(?:twice|\d+ times|X times)\b", re.I),
}
_TEXT_MARKUP = re.compile(r"\[/?[A-Za-z_]+(?:[=:][^\]]*)?\]")


def text_needs(card) -> set:
    """The ``_TEXT_NEEDS`` an attack's description says it supplies; empty for any other card."""
    if not isinstance(card, dict) or str(card.get("type") or "").strip().lower() != "attack":
        return set()
    text = _TEXT_MARKUP.sub("", str(card.get("description") or ""))
    return {need for need, rule in _TEXT_NEEDS.items() if rule.search(text)}


def open_text_needs(act_boss, deck) -> dict:
    """need -> the boss names asking for it, for each ``_TEXT_NEEDS`` demand of this act's boss (``act_boss`` as the
    coordinator builds it) that no card in the observed deck supplies by its text. Empty when either is unknown."""
    if not isinstance(act_boss, dict) or not isinstance(deck, list):
        return {}
    held = set().union(*(text_needs(entry) for entry in deck if isinstance(entry, dict)))
    out: dict[str, list] = {}
    for boss in act_boss.get("bosses") or []:
        for d in boss.get("deck_demands") or []:
            if d.get("need") in _TEXT_NEEDS and d["need"] not in held:
                out.setdefault(d["need"], []).append(str(boss.get("name") or boss.get("id") or "the act boss"))
    return out


def offered_answers(offered_ids, cards, bosses, *, character=None) -> list:
    """Which boss demands each offered card supplies, and the low-death builds at that boss it defines."""
    own_scaling = _SCALING_TEXT.get(character_key(character))
    out = []
    for cid in offered_ids:
        card = cards.get(cid)
        if card is None:
            out.append({"id": cid, "status": "unknown_card"})
            continue
        traits = card_traits(card)
        answers, builds = [], []
        for boss in bosses:
            for d in boss.get("demands") or []:
                need = d["need"]
                if _matches(need, card, traits, own_scaling):
                    answer = {"need": need, "boss": boss.get("name"), "already_met_by_deck": d.get("met")}
                    if own_scaling is not None and need in _SCALING_NEEDS:
                        answer["basis"] = _SCALING_BASIS
                    answers.append(answer)
            for b in (boss.get("builds") or {}).get("lowest_death_rate") or []:
                if cid in b["defining_cards"]:
                    builds.append({"boss": boss.get("name"), "build": b["name"], "death_rate_pct": b["death_rate_pct"]})
        out.append({"id": cid, "name": card.get("name"), "answers_boss_demands": answers,
                    "defines_low_death_builds": builds})
    return out
