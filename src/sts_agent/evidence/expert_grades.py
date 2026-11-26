"""What the best players' v0.111 tier lists say about single cards, and which cards do the same job.

Evidence of a card's general strength, never a rule: a grade says how strong the experts rate a card in a typical
v0.111 deck, not whether this deck needs it. An S card that solves nothing this deck lacks can be worth less than
a B card that answers the fight it is losing; the model weighs the two (``decision.prompts`` CARD STRENGTH).

Sources (``expert_grades_v0111.json``, one entry per character and card id):
- NaveGreed, "NEW Tier Lists For ALL Characters | Patch v0.111.0" (2026-11-10): every card, from his linked
  TierMaker charts; weighted most.
- Opem, "Final Tier Lists Beta Patch v0.111.0" (2026-11-12): his untapped.gg boards read from video frames,
  partly legible; where only "both rate it high/low" was read the view says so.
- Older lists by JapaneseExport, Opem and vmService (v0.98.2 to v0.110.1, or dated): counted only for a card
  Spire Codex's change history shows unchanged between that list's patch and v0.111.
grade: S >= 85, A >= 68, B >= 50, C >= 30, D below, on points S 90 / A 75 / B 60 / C 40 / D 20 (top tiers 100);
NaveGreed's v0.111 tier weighs 50-70%, Opem's v0.111 board 25-35% where read, the valid older lists the rest.
disputed: the v0.111 views or the v0.111 and older views are two tiers or more apart. jobs: what the card's v0.111
text does (AoE, Block, card draw, Weak, Shivs, Frost orbs, Star gain, Doom, ...), read from Spire Codex.
notes: conditions and reasons the two v0.111 videos give, paraphrased from their transcripts.

It belongs to the v0.111 version of the agent (``BUILDS``); the saved v0.107.1 version's frames are as they were.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..game import builds
from ..game.characters import character_key

BUILDS = ("v0.111.0",)
DATA = Path(__file__).with_name("expert_grades_v0111.json")
STRONGER_BY = 15        # points a same-job card must lead by to be named as stronger
MAX_NAMED = 3

BASIS = ("grade from the best players' v0.111 tier lists (NaveGreed's v0.111 list weighs most, Opem's v0.111 board "
         "next, older lists only for cards unchanged since): S, A, B, C, D. It says how strong the card is in a "
         "typical v0.111 deck, not whether this deck needs it. disputed: the experts are two tiers or more apart.")


def active() -> bool:
    return builds.active().key in BUILDS


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))["characters"]


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower().removesuffix("+"))


@lru_cache(maxsize=8)
def _by_name(character: str) -> dict:
    return {_norm(e["name"]): cid for cid, e in _data().get(character, {}).items()}


def _table(character) -> dict:
    return _data().get(character_key(character) or "", {})


def lookup(character, card) -> tuple[str, dict] | None:
    """(card id, entry) for a card dict (id, else name) of this character; None when the lists do not rate it."""
    key = character_key(character) or ""
    table = _data().get(key, {})
    cid = str(card.get("id") or card.get("card_id") or "").strip().upper().removeprefix("CARD.").removesuffix("+")
    for cand in (cid, re.sub(r"_(IRONCLAD|SILENT|DEFECT|REGENT|NECROBINDER)$", "", cid)):
        if cand in table:
            return cand, table[cand]
    cid = _by_name(key).get(_norm(card.get("name") or card.get("card_name")))
    return (cid, table[cid]) if cid else None


def _named(entry) -> str:
    return f"{entry['name']} ({entry['grade']})"


def offer(character, card, deck=None) -> dict | None:
    """The experts' view of one offered card and how it compares with cards that do the same job."""
    found = lookup(character, card)
    if not found:
        return None
    cid, e = found
    row = {"grade": e["grade"], "views": e["views"], "jobs": e["jobs"]}
    for k in ("disputed", "notes"):
        if e.get(k):
            row[k] = e[k]
    held, seen = {}, set()
    for c in deck or []:
        if not isinstance(c, dict):
            continue
        hit = lookup(character, c)
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            held[hit[0]] = hit[1]
    table = _table(character)
    same = []
    for job in e["jobs"]:
        in_deck = [_named(h) for i, h in held.items() if job in h["jobs"] and i != cid]
        stronger = sorted((o for i, o in table.items() if i != cid and job in o["jobs"] and o["type"] == e["type"]
                           and o["score"] >= e["score"] + STRONGER_BY), key=lambda o: -o["score"])
        item = {"job": job}
        if in_deck:
            item["deck_already_has"] = in_deck[:6]
        if stronger:
            item["stronger_for_this_job"] = [_named(o) for o in stronger[:MAX_NAMED]]
        if len(item) > 1:
            same.append(item)
    if same:
        row["same_job"] = same
    return row


def deck_text(character, deck) -> str | None:
    """The grades of the deck's rated cards, best first, for the planner's removal and upgrade judgments."""
    rows, seen = [], set()
    for c in deck or []:
        if not isinstance(c, dict):
            continue
        hit = lookup(character, c)
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            rows.append(hit[1])
    if not rows:
        return None
    rows.sort(key=lambda e: -e["score"])
    disputed = [e["name"] for e in rows if e.get("disputed")]
    return ("Expert grades of the deck's rated cards (starters are not rated): "
            + ", ".join(_named(e) for e in rows) + "."
            + (f" Disputed between the experts: {', '.join(disputed)}." if disputed else "")
            + " A grade is general strength, not what this deck needs.")


def upgrade_hint(character, card_id) -> str | None:
    e = _table(character).get(str(card_id or "").strip().upper().removesuffix("+"))
    return f"Expert note: {e['upgrade']}" if e and e.get("upgrade") else None


def _ladder(character, job, n=5) -> str:
    rows = sorted((e for e in _table(character).values() if job in e["jobs"]), key=lambda e: -e["score"])
    return ", ".join(_named(e) for e in rows[:n])


# What the two v0.111 videos say a run of each character loses to and how its packages are chosen, in the players'
# own terms (NaveGreed sorts cards by how reliably they fix the run's known failure points).
FAILURE_POINTS = {
    "IRONCLAD": (
        "Ironclad runs fail first at fights with several enemies and at the act boss's long fight. NaveGreed takes "
        "the first good AoE card he sees for the first (Inferno best); Opem rates the AoE commons low, so treat that "
        "as a judgment of how much this deck still loses to several enemies. For the boss: Demon Form, Barricade "
        "and Primal Force (NaveGreed) or Corruption (both); Rampage is an emergency answer for an act 1 boss only. "
        "Vulnerable should not rest on Break alone: the boss with Artifact needs a repeatable source (Tremble, "
        "Taunt). Both players' top tier is draw, energy and Block engines rather than raw damage."),
    "SILENT": (
        "Both players build Shivs over Poison: Phantom Blades, Accuracy and Infinite Blades get a deck into Shivs "
        "cleanly, while Poison cards feel weak without Accelerant. Poison is taken to solve a specific fight (the "
        "Waterfall Giant, the Eels), and a deck already in Shivs stops taking it: hybrid decks do not pay off. Opem "
        "marks Sly cards down because the Silent rarely has spare energy; NaveGreed rates several of them Strong."),
    "DEFECT": (
        "The Status engine is Status generators (Overclock, Boost Away, TURBO) paired with Flak Cannon; Compact was "
        "nerfed and no longer covers it. Frost and Focus remain the Block core."),
    "REGENT": (
        "Opem's rule of thumb: a card costing more than 3 Stars is usually weak. Act 1 several-enemy fights: Astral "
        "Pulse handles the Frog Parasite and the Eels, Collision Course the Skulking Colony. NaveGreed says he loses "
        "without Orbit, and his list leans toward his infinite-combo style (Glow, Alignment, Spoils of Battle). "
        "Upgrade-hungry cards (Quasar, Supermassive, Photon Cut) are weak until upgraded."),
    "NECROBINDER": (
        "Early runs fail in hallway fights: Defy, Deathbringer, Bone Shards, Delay and Enfeebling Touch answer them, "
        "and Bone Shards falls off in acts 2 and 3. Late bosses are answered by Lethality plus a big attack "
        "(Eradicate, Squeeze, The Scythe); The Scythe is worth most taken early, until late act 2. Move off relying "
        "on Unleash early, or the deck is stuck trading Block for damage."),
}
_LADDER_JOBS = {
    "IRONCLAD": ("AoE", "Block", "Card draw", "Energy", "Vulnerable", "Damage scaling power", "Block scaling power"),
    "SILENT": ("Shivs", "Block", "Card draw", "AoE", "Weak / enemy Strength loss", "Poison", "Block scaling power"),
    "DEFECT": ("Frost orbs", "Status", "Card draw", "Block scaling power", "Damage scaling power", "AoE", "Energy"),
    "REGENT": ("Star spender", "Star gain", "AoE", "Block", "Forge", "Card draw"),
    "NECROBINDER": ("Doom", "Osty", "Block", "Energy", "Card draw", "Single-target damage"),
}


def planner_text(character) -> str | None:
    """The experts' view for the strategic planner: how to weigh grades, what runs lose to, the best cards per job."""
    key = character_key(character) or ""
    if not active() or key not in FAILURE_POINTS:
        return None
    ladders = "; ".join(f"{job}: {_ladder(key, job)}" for job in _LADDER_JOBS[key])
    return (
        "WHAT THE BEST PLAYERS RATE, AND HOW TO WEIGH IT. Two top players' v0.111 tier lists (NaveGreed, who sorts "
        "cards by how reliably they fix the run's known failure points, and Opem) and older lists for unchanged "
        "cards grade every card S, A, B, C or D: current_facts.opportunities \"expert_grades\" gives the deck's, "
        "and card rewards and shops show each offered card's grade, the experts' views and conditions, and which "
        "cards do the same job. A grade is how strong a card is in a typical deck, never a rule and never a reason "
        "on its own: what a card is worth here is how much it fixes what this deck is losing to (deck_needs, the "
        "act boss's needs, an uncovered losing condition, the balance and what the deck converts) and how well it "
        "does that job. An S card that solves nothing the deck lacks can be worth less than a B card that answers "
        "the fight it is losing; between two cards that do the same job, the higher grade usually does it better, "
        "and a card adds less when the deck already holds a stronger one for that job. Where the experts disagree "
        "(disputed) or attach a condition, the deck's situation decides. Use the grades in standing_plan: name the "
        "strong cards that answer each deck_needs entry, and give removal_targets and upgrade_targets a reason "
        "that weighs grade and job.\n"
        f"{FAILURE_POINTS[key]}\n"
        f"Best-graded cards per job (grade in brackets; the job, not the grade, is the point): {ladders}.")
