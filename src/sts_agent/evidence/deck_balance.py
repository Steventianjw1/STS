"""Deck balance: how the cards added to the deck spread over damage, Block and the scaling of each.

A deck that wins needs four things at once and not too much of any one: damage, Block, something
that makes its damage grow during a fight, and something that makes its Block grow or repeat.
This module counts the observed deck in those four roles and says which are missing, short,
over-supplied or full, and what each offered card would do to that picture.

What the deck should take first changes over the run (``phase``) and with the character
(``DeckPlan``, one per character in ``PLANS``). Ironclad's plan is the one every character had
before: the first picks of act 1 go to damage, because the starter deck kills too slowly for the
act 1 elites and boss, and from act 2 on Block scaling and a thin deck decide fights. The other
four follow their owner's guidance, with the numbers set by simulated runs (``pilot.runs``; what
was measured is recorded beside each plan). A character also has ``tracks``: counts of what its
own mechanic needs (the Silent's Weak, the Necrobinder's Osty and deck size, the Regent's Stars
gained against Stars spent, the Defect's Frost), each with a status like a role's.

The counts are explicit rules over each card's own type and text as the game shows it (and, for
the Regent's Star amounts, the card's id); they need no catalog and no network. They count
cards, not strength: relics, upgrades and card quality are not weighed, so the result is
evidence for a judgment and never a score.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..game.characters import character_key

FRONT = ("damage", "block")
SCALING = ("damage_scaling", "block_scaling")
ROLES = FRONT + SCALING
MAX_NAMES = 10              # card names listed per role

EARLY, BUILD, LATE = "early_damage", "act1_build", "late_purity"


@dataclass(frozen=True)
class DeckPlan:
    """What one character's deck takes first, and when another card of a role stops helping."""
    early_roles: tuple = ("damage", "damage_scaling")   # what the first picks of act 1 go to
    early_picks: int = 3            # act 1 cards added beyond the starter before ``early_roles`` stop coming first
    front_min: int = 4              # damage or Block is over-supplied from this many added cards ...
    front_lead: int = 2             # ... when it also holds this many more than the other
    scaling_full: int = 2           # lasting sources of one kind of scaling beyond which another adds little
    late_first: tuple = ("block_scaling",)   # roles that lead from ``thin_from_act`` on while missing or short
    thin_from_act: int = 2          # the act from which a card that fills nothing is skipped
    deck_target: int | None = None  # deck size the plan holds to: at or above it only a card that fills a need is added
    tracks: tuple = ()              # the character's own tracks, most urgent first (``_TRACKS``)
    targets: dict = field(default_factory=dict)     # numbers the tracks read (see each track)
    note: str = ""                  # the plan in one sentence, shown with the evidence


# One plan per character. IRONCLAD is the plan measured on the logged A10 Ironclad runs and is what a
# character without an entry gets. The others put the owner's guidance into the same terms. Their numbers
# were first read off the decks Spire Mentor's run-policy net builds in simulated A10 runs (about 80 runs a
# character, the deck at each act boss, runs that beat the boss against runs that did not) and then compared
# as whole plans against Ironclad's (``pilot.runs``; the comparison is recorded with each plan). Simulator
# evidence throughout: a stand-in drafter, a small fight search, no live run.
PLANS = {
    "IRONCLAD": DeckPlan(note="damage first in act 1, then a balanced deck that is kept thin from act 2"),
    # Owner: consistent Weak and Block, damage scaling taken for the bosses.
    "SILENT": DeckPlan(
        early_roles=("damage", "block"), scaling_full=3, late_first=("damage_scaling", "block_scaling"),
        tracks=("weak",), targets={"weak_per_cards": 10, "weak_min": 2},
        note="Weak applied every turn and reliable Block come first; damage scaling is taken for each act's boss"),
    # Owner: removals and a smaller deck, Block and Osty's HP preserved, then scaling.
    "NECROBINDER": DeckPlan(
        early_roles=("block", "damage"), early_picks=2, thin_from_act=1, deck_target=18, scaling_full=3,
        late_first=("damage_scaling", "block_scaling"), tracks=("deck_size", "osty", "summon"),
        targets={"summon_min": 2},
        note="a small deck: removals before additions, Block and Osty's HP kept up, and room left only for scaling"),
    # Owner: Star gain balanced against Star cost, Block alongside, scaling by Stars or by the sword.
    "REGENT": DeckPlan(
        early_roles=("damage", "block"), scaling_full=5, late_first=("block_scaling", "damage_scaling"),
        tracks=("stars",), targets={"star_deficit": 3, "star_slack": 2},
        note="Stars gained must pay for the Star costs held; Block alongside, and scaling from Star payoffs or Forge"),
    # Owner: mixed builds that generally defend with Frost; scaling by Orbs or by physical attacks.
    "DEFECT": DeckPlan(
        early_roles=("damage", "block"), scaling_full=4, late_first=("block_scaling", "damage_scaling"),
        tracks=("frost",), targets={"frost_per_cards": 10, "frost_min": 2},
        note="Frost for defense in every build; scaling from Orbs and Focus or from Attacks that grow"),
}


def plan_for(character) -> DeckPlan:
    """The deck plan of ``character`` (a game ID or display name); Ironclad's for anything else."""
    return PLANS.get(character_key(character) or "IRONCLAD", PLANS["IRONCLAD"])


def phase_rules(plan: DeckPlan) -> dict:
    first = " and ".join(r.replace("_", " ") for r in plan.early_roles)
    lead = ", then ".join(r.replace("_", " ") for r in plan.late_first)
    lead = (lead[0].upper() + lead[1:].replace("block", "Block")
            + (" comes first while it is missing" if len(plan.late_first) == 1 else " come first while missing"))
    return {
        EARLY: f"the first {plan.early_picks} cards added in act 1: {first} come first and are never "
               "over-supplied; a card of another role is taken only when it is clearly the strongest card offered",
        BUILD: "the rest of act 1: fill missing or short roles first and skip cards that worsen the balance; a solid "
               "neutral card is still worth taking, because the deck is still too small to win act 1 boss fights",
        LATE: f"{'every act' if plan.thin_from_act <= 1 else f'act {plan.thin_from_act} onwards'}: {lead}"
              ", then the other missing or short roles; skip cards that worsen the balance "
              "and, for a thin deck, skip neutral cards unless clearly strong",
    }


_MARKUP = re.compile(r"\[/?[A-Za-z_]+(?:[=:][^\]]*)?\]")
_GAINS_BLOCK = re.compile(r"\bgains? (?:another )?(?:\d+ )?(?:additional )?Block\b|\bdouble your Block\b", re.I)
_STRENGTH = re.compile(r"\bgain \d+ Strength\b(?! this turn)", re.I)          # lasting Strength only
_RAMPS = re.compile(r"\bincrease (?:(?:this card's|its) damage|the damage of ALL)\b", re.I)
_BLOCK_STAT = re.compile(r"\bgain \d+ (?:Dexterity|Plating)\b(?! this turn)|\bincrease this card's Block\b", re.I)
# A Power stays for the fight, so one that deals damage, raises damage taken or replays Attacks
# makes damage grow; one that speaks of Block makes Block grow or repeat.
_POWER_DAMAGE = re.compile(r"\bdeals? [^.]{0,30}\bdamage\b|\benem(?:y|ies) takes? [^.]*\bdamage\b|\bAttacks?\b"
                           r"|\bplayed against\b|\bPoison\b|\bShivs?\b", re.I)
# The same for the other characters' own ways of dealing damage: Doom, Orbs that hit, enemies losing HP.
_POWER_DAMAGE_MORE = re.compile(r"\bDoom\b(?! to yourself)|\b(?:Lightning|Dark|Glass)\b|\btakes? \d+ damage\b|\bReplay\b"
                                r"|\benem(?:y|ies) loses? (?:\d+|that much) HP\b", re.I)
# Damage that does not come from the card's own hit: generated Shivs, applied Poison or Doom, Orbs that hit.
_INDIRECT_DAMAGE = re.compile(r"\bShivs?\b|\bappl(?:y|ies) \d+ (?:Poison|Doom)\b|\benem(?:y|ies) loses? \d+ HP\b"
                              r"|\bChannels? (?:\w+ )?(?:Lightning|Dark|Glass)\b", re.I)
_POWER_BLOCK = re.compile(r"\bBlock\b", re.I)
_DEALS_DAMAGE = re.compile(r"\bdeals? [^.]{0,30}\bdamage\b", re.I)
# The other characters' Block: Osty takes the hits a Summon gives him HP for, and Frost gains Block every turn.
_SUMMONS = re.compile(r"\bSummons? (?:\d+|X)\b", re.I)
_FROST = re.compile(r"\bChannels? (?:\w+ )?Frost\b", re.I)
# Scaling that is neither Strength nor a Power: the Sovereign Blade grows with every Forge, Orbs with Focus.
_FORGES = re.compile(r"\bForges? \d+\b", re.I)
_FOCUS = re.compile(r"\bgain \d+ Focus\b(?! this turn)", re.I)
_ORBS = re.compile(r"\bOrbs?\b", re.I)
# What lasts and keeps damage off: Intangible, prevented HP loss, a Power that weakens what enemies hit with.
_POWER_GUARD = re.compile(r"\bIntangible\b|\bprevent the next\b|\b(?:it|enem(?:y|ies)) loses? \d+ Strength\b"
                          r"|\bappl(?:y|ies) \d+ Weak\b", re.I)
_WEAKENS = re.compile(r"\bappl(?:y|ies|ying)\b[^.]*\bWeak\b|\benem(?:y|ies) loses? (?:\d+|X) Strength\b"
                      r"|\benemies lose (?:\d+|X) Strength\b", re.I)
_OSTY_DIES = re.compile(r"\bOsty dies\b|\bhe dies\b", re.I)
_STAR_COST_ONLY = re.compile(r"\D")
_DEAD_TYPES = ("curse", "status")

# Stars a Regent card gains each time it is played (base values of the pinned build; a Power's are per turn).
# By id, because the game writes Star amounts as icons that a text rule cannot count.
STAR_GAIN = {"VENERATE": 2, "GATHER_LIGHT": 1, "GLOW": 1, "HIDDEN_CACHE": 4, "SOLAR_STRIKE": 1, "SHINING_STRIKE": 2,
             "BIG_BANG": 1, "CONVERGENCE": 1, "ROYAL_GAMBLE": 9, "GENESIS": 2, "THE_SEALED_THRONE": 4}
# Their Star costs, for a deck observed without the card's own star_cost field.
STAR_COST = {"SEVEN_STARS": 7, "DECISIONS_DECISIONS": 6, "COMET": 5, "NEUTRON_AEGIS": 5, "ROYAL_GAMBLE": 5,
             "DEVASTATE": 4, "THE_SMITH": 4, "ALIGNMENT": 3, "ASTRAL_PULSE": 3, "DYING_STAR": 3, "GAMMA_BLAST": 3,
             "REFLECT": 3, "RESONANCE": 3, "THE_SEALED_THRONE": 3, "FALLING_STAR": 2, "GUIDING_STAR": 2,
             "METEOR_SHOWER": 2, "PARTICLE_WALL": 2, "QUASAR": 2, "CLOAK_OF_STARS": 1, "CRESCENT_SPEAR": 1}

RULES = {
    "counted": "cards added beyond the starter (rarity not Basic); curses and statuses are counted apart; a card "
               "can hold more than one role, and a card with none is listed under other (draw, energy, debuffs)",
    "damage": "an Attack that deals damage, or a card that adds Shivs, applies Poison or Doom, makes an enemy lose "
              "HP, or Channels Lightning, Dark or Glass",
    "block": "a card that is not a Power and gains Block, Summons (Osty takes the hits) or Channels Frost",
    "damage_scaling": "lasting Strength gain, an attack that raises its own damage, Forge, lasting Focus, or a Power "
                      "that deals damage, raises the damage enemies take, replays Attacks or works through Doom or Orbs",
    "block_scaling": "lasting Dexterity, Plating or Focus, a card that raises its own Block, or a Power that gains, "
                     "keeps or multiplies Block, Summons, grants Intangible or weakens what enemies hit with",
    "front_line_status": "damage and block: over_supplied when a role holds at least front_min added cards and at "
                         "least front_lead more than the other; the other is then short; missing when it holds "
                         "none; otherwise balanced",
    "scaling_status": "missing with no source, covered with at least one, full at scaling_full or more",
    "limits": "counts cards, not strength: relics, upgrades and card quality are not weighed",
}


def _field(card, key):
    value = card.get(key)
    return value if value is not None else card.get("card_" + key)


def _text(card) -> str:
    return _MARKUP.sub("", str(_field(card, "description") or ""))


def _cid(card) -> str:
    cid = str(_field(card, "id") or "").strip().upper().removeprefix("CARD.")
    return cid.removesuffix("_CARD")


def card_roles(card) -> tuple:
    """The roles one card holds, read from its type and text. Empty for support cards, curses and statuses."""
    kind = str(_field(card, "type") or "").strip().lower()
    text = _text(card)
    if kind in _DEAD_TYPES:
        return ()
    power, roles = kind == "power", []
    if (kind == "attack" and re.search(r"\bdamage\b", text, re.I)) or (not power and _INDIRECT_DAMAGE.search(text)):
        roles.append("damage")
    if not power and (_GAINS_BLOCK.search(text) or _SUMMONS.search(text) or _FROST.search(text)):
        roles.append("block")
    guards = bool(power and (_POWER_GUARD.search(text) or _SUMMONS.search(text) or _FROST.search(text)))
    blocks = power and _POWER_BLOCK.search(text) and not _DEALS_DAMAGE.search(text)
    orbs = bool(_FOCUS.search(text) or (power and _ORBS.search(text) and not blocks and not _POWER_DAMAGE_MORE.search(text)))
    if (_STRENGTH.search(text) or _RAMPS.search(text) or _FORGES.search(text) or orbs
            or (power and _POWER_DAMAGE.search(text) and (not guards or _DEALS_DAMAGE.search(text)))
            or (power and _POWER_DAMAGE_MORE.search(text) and not blocks and not guards)):
        roles.append("damage_scaling")
    if _BLOCK_STAT.search(text) or blocks or guards or orbs:
        roles.append("block_scaling")
    return tuple(roles)


# ---------------------------------------------------------------------------
# character tracks: one count each of what a character's own mechanic needs
# ---------------------------------------------------------------------------

def _star_cost(card) -> int:
    raw = _field(card, "star_cost")
    if raw is not None and not _STAR_COST_ONLY.search(str(raw).strip() or "x"):
        return int(str(raw).strip())
    return STAR_COST.get(_cid(card), 0)


def card_marks(card) -> dict:
    """What one card is to the character tracks: Weak applied, Osty summoned or lost, Frost, Stars gained and spent."""
    kind = str(_field(card, "type") or "").strip().lower()
    if kind in _DEAD_TYPES:
        return {}
    text = _text(card)
    marks = {"weak": bool(_WEAKENS.search(text)), "summon": bool(_SUMMONS.search(text)),
             "osty_dies": bool(_OSTY_DIES.search(text)), "frost": bool(_FROST.search(text)),
             "star_gain": STAR_GAIN.get(_cid(card), 0), "star_cost": _star_cost(card)}
    return {k: v for k, v in marks.items() if v}


def _track_weak(plan, deck_size, marks, names):
    """The Silent: Weak (or lost enemy Strength) often enough to be up every turn."""
    count = marks["weak"]
    target = max(plan.targets.get("weak_min", 2), math.ceil(deck_size / plan.targets.get("weak_per_cards", 10)))
    status = "missing" if count == 0 else "short" if count < target else "covered" if count <= target + 1 else "over_supplied"
    return {"count": count, "target": target, "cards": names["weak"], "status": status,
            "rule": f"cards that apply Weak or lower enemy Strength, the starter's included: one per "
                    f"{plan.targets.get('weak_per_cards', 10)} cards in the deck (at least "
                    f"{plan.targets.get('weak_min', 2)}) keeps Weak on the enemy most turns; two more than that is too many"}


def _track_summon(plan, deck_size, marks, names):
    """The Necrobinder: Osty's HP is Block that lasts, so the deck keeps Summon cards."""
    count, target = marks["summon"], plan.targets.get("summon_min", 2)
    status = "missing" if count == 0 else "short" if count < target else "covered"
    return {"count": count, "target": target, "cards": names["summon"], "status": status,
            "rule": f"cards that Summon, the starter's included: at least {target} keep Osty alive to take hits"}


def _track_osty(plan, deck_size, marks, names):
    count = marks["osty_dies"]
    return {"count": count, "cards": names["osty_dies"], "status": "over_supplied" if count else "covered",
            "rule": "cards that kill Osty for a one-time effect: each one gives up the HP the Summon cards built"}


def _track_deck_size(plan, deck_size, marks, names):
    target = plan.deck_target
    status = "covered" if target is None or deck_size < target else "over_supplied"
    return {"count": deck_size, "target": target, "cards": [], "status": status,
            "rule": f"deck size against the {target} cards this plan holds to: at or above it a card is added only "
                    "when it fills a missing or short role or track, and a removal comes before an addition"}


def _track_stars(plan, deck_size, marks, names):
    """The Regent: Stars gained over one pass through the deck against the Star costs held."""
    gain, cost = marks["star_gain"], marks["star_cost"]
    owed, slack = plan.targets.get("star_deficit", 3), plan.targets.get("star_slack", 2)
    status = ("short" if cost - gain > owed else "over_supplied" if gain - cost > slack else "balanced")
    return {"count": gain - cost, "star_gain": gain, "star_cost": cost, "cards": names["star_gain"],
            "spenders": names["star_cost"], "status": status,
            "rule": "Stars the deck's cards gain in one pass (a Power counted for one turn) minus the Star costs "
                    f"held; short when the costs are more than {owed} higher (about what the starting relic's Stars "
                    f"pay for in a fight), over-supplied when more than {slack} Stars would go unspent. Stars from "
                    "relics are not counted"}


def _track_frost(plan, deck_size, marks, names):
    """The Defect: Frost Orbs are Block that repeats, and a bigger deck needs more cards that Channel them."""
    count = marks["frost"]
    target = max(plan.targets.get("frost_min", 2), math.ceil(deck_size / plan.targets.get("frost_per_cards", 10)))
    status = "missing" if count == 0 else "short" if count < target else "covered"
    return {"count": count, "target": target, "cards": names["frost"], "status": status,
            "rule": f"cards that Channel Frost: one per {plan.targets.get('frost_per_cards', 10)} cards in the deck (at "
                    f"least {plan.targets.get('frost_min', 2)}) give the deck Block that repeats every turn"}


_TRACKS = {"weak": _track_weak, "summon": _track_summon, "osty": _track_osty, "deck_size": _track_deck_size,
           "stars": _track_stars, "frost": _track_frost}
NEEDS, EXCESS = ("missing", "short"), ("over_supplied", "full")


def _limits(plan: DeckPlan) -> dict:
    return {"front_min": plan.front_min, "front_lead": plan.front_lead, "scaling_full": plan.scaling_full,
            "deck_target": plan.deck_target, **({"star_deficit": plan.targets.get("star_deficit", 3)}
                                                if "stars" in plan.tracks else {})}


def _statuses(counts, limits) -> dict:
    out = {}
    for role, other in (FRONT, FRONT[::-1]):
        mine, theirs = counts[role], counts[other]
        out[role] = ("over_supplied" if mine >= limits["front_min"] and mine - theirs >= limits["front_lead"] else
                     "missing" if mine == 0 else
                     "short" if theirs >= limits["front_min"] and theirs - mine >= limits["front_lead"] else "balanced")
    for role in SCALING:
        out[role] = "missing" if counts[role] == 0 else "covered" if counts[role] < limits["scaling_full"] else "full"
    return out


def phase(act, added, plan: DeckPlan = PLANS["IRONCLAD"]) -> str:
    """Which deck-building phase applies: by act, and in act 1 by the cards added beyond the starter.
    An unknown act is read as act 1."""
    if act is not None and act >= 2:
        return LATE
    if added < plan.early_picks:
        return EARLY
    return LATE if plan.thin_from_act <= 1 else BUILD


def _priority(stage, status, plan, tracks) -> list:
    """The roles and tracks to take first in this phase, most urgent first."""
    wanted = [name for name in plan.tracks if tracks[name]["status"] in NEEDS]
    if stage == EARLY:
        return wanted + list(plan.early_roles)
    short = [r for r in ROLES if status[r] in NEEDS]
    if stage == LATE:
        lead = [r for r in plan.late_first if r in short]
        short = lead + [r for r in short if r not in lead]
    return wanted + short


def balance(deck, act=None, character=None, plan: DeckPlan | None = None) -> dict | None:
    """Role counts, statuses and the phase's priorities for an observed permanent deck; None when the
    deck is unknown. ``act`` is the current act number when known, ``character`` selects the plan;
    ``plan`` replaces it (a variant under comparison in ``pilot.runs``)."""
    if not isinstance(deck, list):
        return None
    plan = plan or plan_for(character)
    counts = {role: 0 for role in (*ROLES, "other")}
    names = {role: [] for role in counts}
    marks = {key: 0 for key in ("weak", "summon", "osty_dies", "frost", "star_gain", "star_cost")}
    mark_names = {key: [] for key in marks}
    starter, dead, size, added = {role: 0 for role in FRONT}, 0, 0, 0
    for entry in deck:
        if not isinstance(entry, dict):
            continue
        n = entry.get("count") if type(entry.get("count")) is int and entry["count"] > 0 else 1
        size += n
        if str(entry.get("type") or "").strip().lower() in _DEAD_TYPES:
            dead += n
            continue
        label = str(entry.get("name") or entry.get("id") or "?") + (f" x{n}" if n > 1 else "")
        for key, value in card_marks(entry).items():        # the tracks count the starter's cards too
            marks[key] += n * (value if type(value) is int else 1)
            mark_names[key].append(label)
        roles = card_roles(entry)
        if str(entry.get("rarity") or "").strip().lower() == "basic":
            for role in roles:
                if role in starter:
                    starter[role] += n
            continue
        added += n
        for role in roles or ("other",):
            counts[role] += n
            names[role].append(label)
    status = _statuses(counts, _limits(plan))
    tracks = {name: _TRACKS[name](plan, size, marks, {k: sorted(v)[:MAX_NAMES] for k, v in mark_names.items()})
              for name in plan.tracks}
    stage = phase(act if type(act) is int else None, added, plan)
    sides = {"damage": counts["damage"] + counts["damage_scaling"], "block": counts["block"] + counts["block_scaling"]}
    return {
        "label": "DECK BALANCE: the cards added beyond the starter, counted by what they do. Rules over card text, "
                 "not a strength score.",
        "character": character_key(character), "plan": plan.note,
        "phase": stage, "phase_rule": phase_rules(plan)[stage], "priority": _priority(stage, status, plan, tracks),
        "deck_size": size, "curses_and_statuses": dead, "starter_cards": starter, "cards_added": added,
        "added": {role: {"count": counts[role], "cards": sorted(names[role])[:MAX_NAMES]} for role in counts},
        "status": status,
        "tracks": tracks,
        "short": [r for r in ROLES if status[r] in NEEDS] + [t for t in plan.tracks if tracks[t]["status"] in NEEDS],
        "over_supplied": ([r for r in ROLES if status[r] in EXCESS]
                          + [t for t in plan.tracks if tracks[t]["status"] in EXCESS]),
        "lighter_side": None if sides["damage"] == sides["block"] else min(sides, key=sides.get),
        "limits": _limits(plan),
        "rules": RULES,
    }


def _track_effect(state, marks, fills_role) -> tuple:
    """(tracks the card fills, tracks it worsens) for the deck ``state`` describes."""
    tracks, fills, worse, limits = state.get("tracks") or {}, [], [], state["limits"]
    status = {name: row["status"] for name, row in tracks.items()}
    if "weak" in status and marks.get("weak"):
        (fills if status["weak"] in NEEDS else worse if status["weak"] in EXCESS else []).append("weak")
    if "summon" in status and marks.get("summon") and status["summon"] in NEEDS:
        fills.append("summon")
    if "osty" in status and marks.get("osty_dies"):
        worse.append("osty")
    if "frost" in status and marks.get("frost") and status["frost"] in NEEDS:
        fills.append("frost")
    if "stars" in status:
        gain, cost = marks.get("star_gain", 0), marks.get("star_cost", 0)
        if status["stars"] == "short":
            (fills if gain > cost else worse if cost > gain else []).append("stars")
        elif status["stars"] == "over_supplied":
            (fills if cost > gain else worse if gain > cost else []).append("stars")
        elif cost > gain and (cost - gain) - tracks["stars"]["count"] > limits.get("star_deficit", 3):
            worse.append("stars")       # balanced now, short after this card
    if status.get("deck_size") in EXCESS and not fills and not fills_role:
        worse.append("deck_size")
    return fills, worse


def offer_effects(state, offers) -> list:
    """What taking each offered card would do to the balance.

    ``offers`` are (action_id, card) pairs. ``adds_to_over_supplied`` is judged on the deck as it
    would be after the pick, so the card that tips a role over is marked, not only the ones after it.
    In the early phase the plan's first roles count as needed and never as over-supplied.
    ``fills_priority`` marks a card for the phase's most urgent role or track. A character's tracks
    are judged with the roles: a card can fill one (Weak while it is short) or worsen one (a Star
    cost the deck's Stars do not pay for, a card that kills Osty, any card that fills nothing once
    the deck is at its target size).
    """
    if not state:
        return []
    limits = state["limits"]
    counts = {role: state["added"][role]["count"] for role in ROLES}
    early = state.get("phase") == EARLY
    priority = state.get("priority") or []
    top = set(priority) if early else set(priority[:1])
    out = []
    for action_id, card in offers:
        roles = card_roles(card)
        after = {role: counts[role] + (role in roles) for role in ROLES}
        after_status = _statuses(after, limits)
        over = [r for r in roles if not (early and r in priority)
                and (after_status[r] == "over_supplied" if r in FRONT else counts[r] >= limits["scaling_full"])]
        fills = [r for r in roles if (early and r in priority) or state["status"][r] in NEEDS]
        track_fills, track_over = _track_effect(state, card_marks(card), bool(fills))
        fills, over = fills + track_fills, over + track_over
        out.append({"action_id": action_id, "name": _field(card, "name"), "roles": list(roles) or ["other"],
                    "fills_short": fills, "fills_priority": bool(top.intersection(fills)),
                    "adds_to_over_supplied": over,
                    "effect": ("mixed" if fills and over else "improves" if fills else "worsens" if over else
                               "neutral")})
    return out


def _track_text(name, row) -> str:
    if name == "stars":
        return (f"Stars gained per pass {row['star_gain']} against Star costs held {row['star_cost']}: "
                f"{row['status'].replace('_', ' ')}")
    if name == "deck_size":
        return f"deck size {row['count']} against a target of {row['target']}: {row['status'].replace('_', ' ')}"
    label = {"weak": "Weak sources", "summon": "Summon cards", "osty": "cards that kill Osty", "frost": "Frost cards"}[name]
    target = f" of {row['target']} wanted" if row.get("target") else ""
    return f"{label} {row['count']}{target}: {row['status'].replace('_', ' ')}"


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
    limits = state["limits"]
    tracks = "; ".join(_track_text(name, row) for name, row in (state.get("tracks") or {}).items())
    # The verdict leads, so a bounded field that cuts the end loses detail and never the conclusion.
    return (f"Phase {state['phase']}: {state['phase_rule']}; take first: "
            f"{', '.join(state['priority']).replace('_', ' ') or 'nothing in particular'}. Missing or short: {', '.join(state['short']).replace('_', ' ') or 'none'}. Over-supplied or full: "
            f"{', '.join(state['over_supplied']).replace('_', ' ') or 'none'}. "
            + (f"This character's plan: {state['plan']}. Its tracks: {tracks}. " if tracks else "")
            + f"Lighter side: "
            f"{state['lighter_side'] or 'neither'}. Cards added beyond the starter, by rules over their text (a "
            f"card can hold two roles; relics and card quality are not weighed): {'; '.join(parts)}. Other added "
            f"cards (draw, energy, debuffs): {other['count']}. Starter cards still held: {start['damage']} attacks, "
            f"{start['block']} Block cards. Deck size {state['deck_size']}, of which "
            f"{state['curses_and_statuses']} curses or statuses. Status rules: damage and Block are over-supplied "
            f"at {limits['front_min']} or more added cards and {limits['front_lead']} or more ahead of the other; a "
            f"scaling role is covered by one lasting source and full at {limits['scaling_full']}.")
