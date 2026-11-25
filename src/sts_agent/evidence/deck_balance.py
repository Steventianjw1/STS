"""Deck balance: how the cards added to the deck spread over damage, Block and the scaling of each.

A deck that wins needs four things at once and not too much of any one: damage, Block, something
that makes its damage grow during a fight, and something that makes its Block grow or repeat.
This module counts the observed deck in those four roles and says which are missing, short,
over-supplied or full, and what each offered card would do to that picture.

What the deck should take first changes over the run (``phase``), with the character and with the
game build (``DeckPlan``, one per character and build in ``PLANS_BY_BUILD``; ``plan_for`` answers for
the build in use, ``game.builds``). On v0.107.1 Ironclad's plan is the one every character had
before: the first picks of act 1 go to damage, because the starter deck kills too slowly for the
act 1 elites and boss, and from act 2 on Block scaling and a thin deck decide fights. The other
four follow the owner's guidance for each. The Regent's numbers were set by simulated runs
(``pilot.runs``); the Silent's, Defect's and Necrobinder's follow what the decks of the highest-rated
players hold and what those players take (``pilot.players``) and were then checked in simulated runs.
What was measured is recorded with the plans. A character also has ``tracks``: counts of what its
own mechanic needs (the Silent's Weak, the Necrobinder's Weak and Osty, the Regent's Stars gained
against Stars spent, the Defect's Frost and Weak), each with a status like a role's.

The counts are explicit rules over each card's own type and text as the game shows it (and, for
the Regent's Star amounts, the card's id); they need no catalog and no network. They count
cards, not strength: relics, upgrades and card quality are not weighed, so the result is
evidence for a judgment and never a score.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..game import builds
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
    thin_from_act: int | None = 2   # the act from which a card that fills nothing is skipped; None: no such act
    build_first: tuple = ()         # roles that lead before ``thin_from_act``, after the first picks, while missing or short
    caps: dict = field(default_factory=dict)        # front-line role -> added cards from which it is over-supplied by itself:
                                                    # one number, or one per act (the last stands for later acts)
    never_over: tuple = ()          # front-line roles that are never over-supplied
    scaling_min: dict = field(default_factory=dict)     # scaling role -> lasting sources below which it is still short
    deck_target: int | None = None  # deck size the plan holds to: at or above it only a card that fills a need is added
    deck_targets: dict = field(default_factory=dict)    # act -> the deck size held to in that act (replaces ``deck_target``)
    removals_from_act: int | None = None    # from this act the shop's card removal is bought before a card (``pilot.runs``)
    tracks: tuple = ()              # the character's own tracks, most urgent first (``_TRACKS``)
    targets: dict = field(default_factory=dict)     # numbers the tracks read (see each track)
    note: str = ""                  # the plan in one sentence, shown with the evidence


# ---------------------------------------------------------------------------
# Game build v0.107.1
# ---------------------------------------------------------------------------
#
# One plan per character. IRONCLAD is the plan measured on the logged A10 Ironclad runs and is what a
# character without an entry gets. The others put the owner's guidance into the same terms.
#
# How their numbers were set. Simulator evidence is ``pilot.runs``: engine fingerprint cc2c06eb91b21a2a,
# ascension 10, the same 80 seeds for every plan of a character, runs ended at the act 2 boss, fights
# searched with 400 simulations a decision and 1600 at bosses, a stand-in drafter, no model and no live
# run. Player evidence is ``pilot.players``, read 2026-11-25: the solo A10 standard runs of builds v0.107.1
# and v0.107.0 by the 12 highest-rated Spire Codex accounts that have them (Silent 166 runs and 3058 card
# rewards, Defect 133 and 2419, Necrobinder 137 and 2401).
#   1. Read off the decks Spire Mentor's run-policy net builds by itself, at each act boss, in runs that
#      beat the boss and runs that did not. Runs that won held one Frost card per 8 to 10 cards (Defect),
#      Star costs within a Star of the Stars gained (Regent), fewer attacks and more Block and scaling
#      than runs that lost (Necrobinder), and 3.5 damage-scaling cards at the act 2 boss (Regent) where
#      every other character's held about 2.
#   2. Each first plan drafted the same seeds as Ironclad's plan ("shared") did for that character. Paired
#      differences, plan minus shared, with standard errors:
#        SILENT       floor +0.04 +-0.71, act 1 boss +0.0 +-4.4 points, act 2 boss -1.3 +-2.8 points
#        NECROBINDER  floor -0.06 +-0.68, act 1 boss +0.0 +-5.0 points, act 2 boss -3.7 +-2.8 points
#        REGENT       floor -0.28 +-0.43, act 1 boss -2.5 +-2.5 points, act 2 boss +8.7 +-4.1 points
#        DEFECT       floor +0.42 +-0.71, act 1 boss +0.0 +-4.0 points, act 2 boss +5.0 +-4.3 points
#      So the Regent's plan beat the act 2 boss more often than the shared plan did, and it stands.
#   3. Ironclad's own plan against the net by itself: floor -0.82 +-0.72, act 2 boss +0.0 +-6.4 points,
#      with a deck 3 cards smaller. For the other four the shared plan cost act 2 boss wins against the net
#      (Regent -11.3 +-5.0 points, Defect -13.8 +-6.4, Necrobinder -7.5 +-3.5); the Regent's own plan gave
#      most of that back (-2.5 +-4.0).
#   4. The Silent's, Defect's and Necrobinder's first plans did not read decks the way the best players
#      build them: of the cards those players took, the plans called 16%, 16% and 35% worsening, and from
#      act 2 they marked every card without a role as one to skip. What the players' decks and picks say,
#      and the field for it:
#        - no deck is kept thin. Card rewards taken in acts 1, 2 and 3: 86/72/60% (Silent), 89/73/62%
#          (Defect), 84/65/54% (Necrobinder), as often with a large deck as with a small one; a card
#          with no role was taken about one time in three at any count. So ``thin_from_act=None``, and
#          no deck-size target for the Necrobinder, whose decks held 19, 25 and 32 cards at the bosses.
#        - Block was taken three times in ten or more at any count, so it is never over-supplied.
#          Damage cards fell from one in three to under one in ten, so damage has ``caps``: one more
#          than the damage cards those decks held at each act's boss (3.8/6.0/7.3 Silent, 4.3/6.8/8.8
#          Defect, 3.8/5.8/7.4 Necrobinder).
#        - Silent: Block scaling taken 80% of the times offered with none held and 65% with one,
#          damage scaling 37% and 46%: both lead and are short below two sources, full at four. Weak
#          sources held 1.7/2.7/3.5: one per 15 cards, at least two.
#        - Defect: Frost taken 51%, 46%, 32%, 40% with none to three held and 18% with four: two, three
#          past 24 cards, over-supplied from four. Weak 65% with none, 21% with one, 7% with two: one.
#          Focus, slots and Block Powers about 40% with none to two held: short below two.
#        - Necrobinder: Weak taken 82% with none and still 54% with four: three, one per 8 cards.
#          Summon cards 20 to 23% at any count: no Summon target.
#        - removals by act: 0.5/1.8/1.2 (Silent), 0.4/1.8/0.9 (Defect), 0.7/2.0/1.5 (Necrobinder).
#      The refitted plans call 12%, 8% and 9% of the cards taken worsening; a card they put first was
#      taken 33%, 38% and 58% of the times it was offered, one they call worsening 11%, 14% and 12%.
#   5. The refitted plans on the same seeds. Average floor / act 1 boss beaten / act 2 boss beaten, then
#      the plan minus the first plan of step 2, and minus the net by itself:
#        SILENT       19.3 / 45.0% / 17.5%   -0.12 +-0.17, +0.0 +-0.0, +2.5 +-3.1   -0.69 +-0.69, -5.0 +-4.7, +0.0 +-3.1
#        DEFECT       25.2 / 80.0% / 40.0%   -0.61 +-0.84, +3.7 +-4.5, +3.7 +-5.5   -0.80 +-0.52, +0.0 +-3.1, -5.0 +-5.9
#        NECROBINDER  20.9 / 57.5% / 10.0%   +1.68 +-0.86, +8.7 +-5.1, +5.0 +-3.0   +0.65 +-0.61, +5.0 +-3.9, -6.2 +-4.1
#      So the Necrobinder's plan is the stronger one (and beats the shared plan by 1.61 +-0.65 floors);
#      the Silent's and Defect's are level with their first plans, and are kept because they read decks
#      the way the players build them. Three things the simulated runs decided on the way:
#        - the shop's removal bought first in every act cost the Silent (against not buying it first:
#          floor -0.71 +-0.46, act 2 boss -6.2 +-2.7 points) and the Necrobinder (against act 2 onwards:
#          floor -1.38 +-0.71, act 1 boss -10.0 +-4.6 points), so ``removals_from_act=2``, which is also
#          where the players make theirs;
#        - the Defect's first picks: Block alone lost to Block and damage (floor -0.50 +-0.49, act 1 boss
#          -6.2 +-3.3 points); the players took a first Block card 44% and a first damage card 34% of the
#          times one was offered;
#        - a damage cap at the count where the players' pick rate halves (4 for the Silent and
#          Necrobinder, 6 for the Defect, in every act) measured the same as the caps above, which are
#          kept because they do not stop a deck below what the players' decks held.
PLANS_V0107 = {
    "IRONCLAD": DeckPlan(note="damage first in act 1, then a balanced deck that is kept thin from act 2"),
    # Owner: consistent Weak and Block, damage scaling taken for the bosses.
    "SILENT": DeckPlan(
        early_roles=("damage", "block"), thin_from_act=None, build_first=("block_scaling", "damage_scaling"),
        caps={"damage": (5, 7, 9)}, never_over=("block",), scaling_min={"block_scaling": 2, "damage_scaling": 2},
        scaling_full=4,
        tracks=("weak",), targets={"weak_per_cards": 15, "weak_min": 2}, removals_from_act=2,
        note="Block that grows and Shiv scaling first, Weak kept up, Block, draw and utility added in every act; "
             "plain attacks stop after the first few"),
    # Owner: removals and a smaller deck, Block and Osty's HP preserved, then scaling. The players get the
    # smaller deck from removals and from refusing attacks; Weak is what they take to protect Osty and the deck.
    "NECROBINDER": DeckPlan(
        early_roles=("block",), early_picks=2, thin_from_act=None, front_min=99, caps={"damage": (5, 7, 9)},
        scaling_full=3,
        tracks=("weak", "osty"), targets={"weak_per_cards": 8, "weak_min": 3}, removals_from_act=2,
        note="Weak and Block first so Osty and the deck survive, removals before additions, then energy and "
             "scaling; plain attacks stop after the first few"),
    # Owner: Star gain balanced against Star cost, Block alongside, scaling by Stars or by the sword. Forge
    # cards are common and add up, so five sources fill damage scaling here, not two.
    "REGENT": DeckPlan(
        early_roles=("damage", "block"), scaling_full=5, late_first=("block_scaling", "damage_scaling"),
        tracks=("stars",), targets={"star_deficit": 3, "star_slack": 2},
        note="Stars gained must pay for the Star costs held; Block alongside, and scaling from Star payoffs or Forge"),
    # Owner: mixed builds that generally defend with Frost; scaling by Orbs or by physical attacks.
    "DEFECT": DeckPlan(
        early_roles=("block", "damage"), thin_from_act=None, front_min=99, build_first=("block_scaling",),
        caps={"damage": (6, 8, 10)},
        scaling_min={"block_scaling": 2}, scaling_full=4, tracks=("frost", "weak"),
        targets={"frost_per_cards": 12, "frost_min": 2, "frost_max": 4, "weak_per_cards": 0, "weak_min": 1, "weak_over": 1},
        removals_from_act=2,
        note="Frost and Block first, one Weak source, Focus and Orb slots for scaling; draw and energy added in every "
             "act, attacks only while few are held"),
}


# ---------------------------------------------------------------------------
# Game build v0.111 (v0.111.0 and its hotfix v0.111.1)
# ---------------------------------------------------------------------------
#
# Every character's plan is refitted to what this build's best players' decks hold and take. Player
# evidence is ``pilot.players``, read 2026-11-25: the solo A10 standard runs of builds v0.111.1 and v0.111.0
# by the 20 highest-rated Spire Codex accounts that have them, up to 40 runs each:
#     IRONCLAD     532 runs, 397 won, 10142 card rewards     decks at the act bosses 19.1 / 25.7 / 32.3 cards
#     SILENT       531 runs, 437 won,  9954 card rewards                             21.0 / 28.1 / 35.1
#     DEFECT       471 runs, 308 won,  8663 card rewards                             19.7 / 26.9 / 34.6
#     REGENT       475 runs, 344 won,  8557 card rewards                             18.9 / 25.4 / 32.8
#     NECROBINDER  537 runs, 371 won,  9634 card rewards                             18.8 / 25.2 / 31.2
# The same rules set every plan's numbers ("pick rate" is how often a card of the kind was taken when it
# was offered, by how many of the kind the deck already held; a count with under 40 offers is not read):
#   - no thin phase and no deck-size target: a card with no role was taken about one time in three at any
#     count and in every act (Ironclad 38/35/32% by act, Silent 32/35/35, Defect 27/34/35, Regent 36/33/30,
#     Necrobinder 36/40/41), and a reward was taken as often with a large deck as with a small one.
#   - a front-line role is capped when its pick rate with six or more held is under half its rate with
#     none, at one more than the mean those decks held at each act's boss, rounded up; otherwise it is
#     never over-supplied. Damage fell that far for all five (to 9, 8, 13, 8 and 11%) and held 3.5/5.4/7.0
#     (Ironclad), 3.8/5.6/7.0 (Silent), 4.2/6.5/8.5 (Defect), 2.2/3.7/4.8 (Regent), 3.6/5.9/7.6
#     (Necrobinder). Block fell that far for the Ironclad alone (38% to 18%, holding 2.5/4.3/5.8), by a
#     hair, and there the simulated runs chose: no cap on Block (see below).
#   - a scaling role is short while its pick rate is above the highest rate of a card with no role, and
#     full from the count where the rate is under 20%. Block scaling stays above it through three held
#     for the Ironclad (46/55/54/66%), Silent (88/75/68/60%), Defect (48/44/41/39%) and Regent
#     (42/39/39/38%): short below four. Damage scaling does for the Silent's first two (44/37%). It falls
#     under 20% for the Ironclad alone (14% with four); elsewhere it holds between 20 and 30% as far as
#     the offers reach, so ``scaling_full`` is set past them.
#   - the first picks (the first three rewards of act 1) go to the roles taken at least as often there as
#     a card with no role: damage scaling and Block for the Ironclad (40 and 34% against 32%) and the
#     Necrobinder (47 and 41% against 29%), every role for the Silent and the Defect (whose no-role
#     cards were taken 24 and 19%). The Regent's rates are level (Block 39%, damage scaling 36%, no role
#     34%, damage 26%), and there the simulated runs chose: damage and Block (see below).
#   - tracks. Weak: the Ironclad took a first source 55% and a second 31%: one. The Silent 38% with one,
#     25 to 32% with two to four, 14% with five: two, too many from five. The Defect 44/22/12%: one, a
#     third is too many. The Necrobinder 72/67/57/55/46%: four, one per 7 cards. The Regent 30% with one,
#     11% with three: one, too many from three. Frost (Defect) 58/47% then 27 to 34% with two to five
#     and 14% with six: two, too many from six. Stars (Regent): a card that costs more Stars than it
#     gains was taken 26 to 30% while the deck's Star gain covered its Star costs and 16 to 19% once it
#     did not; a card that gains Stars 26 to 31%, and 33 to 39% from three Stars owed; with four or more
#     Stars spare the two swap (23% and 30%). Owed up to 3, spare up to 3. A card that kills Osty
#     (Necrobinder) was taken 29% with none held (39% in act 1) and 11% with one: one is carried.
#   - removals by act: 0.4/1.6/1.1, 0.6/1.6/1.2, 0.5/1.8/0.9, 0.5/1.8/1.0, 0.8/2.0/1.2 in the order above:
#     bought first from act 2 on.
#
# Simulator evidence is ``pilot.runs`` on Spire Mentor's v0.111.0 engine (release 0.11.8, fingerprint
# b3df13b15b37b75e), run 2026-11-25: ascension 10, runs ended at the act 2 boss, fights searched with 400
# simulations a decision and 1600 at bosses, a stand-in drafter, no model and no live run. Every plan of
# a character drafted the same seeds: 120 for Spire Mentor's run-policy net alone, 60 for the plan carried
# over from v0.107.1 and for the first refit, 60 or 80 for a variant. Average floor / act 1 boss beaten /
# act 2 boss beaten of the plans below, then the plan minus the carried-over plan, and minus the net:
#     IRONCLAD     27.6 / 83.3% / 55.0%   +1.58 +-0.80, +8.3 +-4.3, +13.3 +-7.3   -1.17 +-0.73, -5.0 +-3.7, -8.3 +-6.8
#     SILENT       21.4 / 56.7% / 33.3%   +0.00 +-0.00, +0.0 +-0.0, +0.0 +-2.4    -1.05 +-0.83, -5.0 +-5.5, -1.7 +-5.6
#     DEFECT       25.7 / 76.7% / 45.0%   +0.07 +-0.06, +0.0 +-0.0, +3.3 +-2.3    -0.33 +-0.67, -5.0 +-3.7, -3.3 +-6.7
#     REGENT       23.3 / 73.8% / 25.0%   +0.00 +-0.44, -5.0 +-2.8, +5.0 +-6.9    +1.04 +-0.72, +5.0 +-3.9, +2.5 +-5.0
#     NECROBINDER  19.5 / 46.7% / 15.0%   +0.33 +-0.35, +0.0 +-0.0, +3.3 +-2.3    -0.13 +-0.52, -3.3 +-2.3, -3.3 +-4.7
# (the net alone: 27.4 / 83.3% / 57.5%, 23.0 / 63.3% / 34.2%, 25.9 / 80.0% / 45.8%, 22.8 / 68.3% / 24.2% and
# 19.8 / 49.2% / 18.3%). So no refit is behind the plan it replaces, the Ironclad's is ahead of it, and all
# five are within noise of the net by itself, as on v0.107.1. They are kept because they read decks the
# way this build's players build them: of the cards those players took, the refits call 7% (Ironclad),
# 11% (Silent), 6% (Defect), 11% (Regent) and 9% (Necrobinder) worsening, where the carried-over plans
# call 20, 12, 9, 14 and 11%, and a card a refit puts first was taken 43, 38, 39, 33 and 54% of the times
# it was offered (27, 34, 38, 28 and 56% before).
# Two numbers the rules above left open were set by these runs:
#   - Ironclad, Block. With Block capped at the players' holding plus one (4, 6 and 7 by act) the plan
#     reached 27.3 / 81.7% / 50.0%; without the cap floor +0.35 +-0.27 and act 2 boss +5.0 +-3.7 points:
#     no cap. Damage and Block as the first picks instead was behind (floor -0.45 +-0.64).
#   - Regent, first picks. Block and damage scaling first reached 23.7 / 71.7% / 25.0% and lost the act 1
#     boss more often than the carried-over plan (-8.3 +-4.3 points); damage and Block first gave floor
#     +0.58 +-0.60 and act 2 boss +5.0 +-3.7 points against it. Three other changes to that refit did not
#     help: the shop's removal not bought first (floor -0.12 +-0.15), no cap on damage cards (floor -0.03
#     +-0.55, act 2 boss -3.3 +-5.3 points) and a deck kept thin from act 2 (floor -0.47 +-0.32, act 2
#     boss -6.7 +-5.2 points).
PLANS_V0111 = {
    "IRONCLAD": DeckPlan(
        early_roles=("damage_scaling", "block"), thin_from_act=None, front_min=99, build_first=("block_scaling",),
        caps={"damage": (5, 7, 8)}, scaling_min={"block_scaling": 4}, scaling_full=4,
        tracks=("weak",), targets={"weak_per_cards": 0, "weak_min": 1}, removals_from_act=2,
        note="one Weak source and Block that grows first, draw, energy and Exhaust utility added in every act; "
             "attacks stop once the deck holds what the best decks hold"),
    "SILENT": DeckPlan(
        early_roles=("damage_scaling", "damage", "block"), thin_from_act=None,
        build_first=("block_scaling", "damage_scaling"), caps={"damage": (5, 7, 8)}, never_over=("block",),
        scaling_min={"block_scaling": 4, "damage_scaling": 2}, scaling_full=7,
        tracks=("weak",), targets={"weak_per_cards": 0, "weak_min": 2, "weak_over": 3}, removals_from_act=2,
        note="Block that grows first, then Shiv scaling, two Weak sources, Block, draw and utility added in every "
             "act; plain attacks stop after the first few"),
    "NECROBINDER": DeckPlan(
        early_roles=("damage_scaling", "block"), thin_from_act=None, front_min=99, caps={"damage": (5, 7, 9)},
        scaling_full=5,
        tracks=("weak", "osty"), targets={"weak_per_cards": 7, "weak_min": 4, "osty_dies_ok": 1}, removals_from_act=2,
        note="Weak first and in numbers, Block and a first damage multiplier, removals before additions from act 2, "
             "energy and draw in every act; plain attacks stop after the first few"),
    "REGENT": DeckPlan(
        early_roles=("damage", "block"), thin_from_act=None, front_min=99, build_first=("block_scaling",),
        caps={"damage": (4, 5, 6)}, scaling_min={"block_scaling": 4}, scaling_full=7,
        tracks=("stars", "weak"),
        targets={"star_deficit": 3, "star_slack": 3, "weak_per_cards": 0, "weak_min": 1, "weak_over": 2},
        removals_from_act=2,
        note="Star costs paid for by Star gain, Block that grows first, Block and Star utility added in every act; "
             "few plain attacks"),
    "DEFECT": DeckPlan(
        early_roles=("block", "damage", "damage_scaling"), thin_from_act=None, front_min=99,
        build_first=("block_scaling",), caps={"damage": (6, 8, 10)}, scaling_min={"block_scaling": 4}, scaling_full=7,
        tracks=("frost", "weak"),
        targets={"frost_per_cards": 99, "frost_min": 2, "frost_max": 6, "weak_per_cards": 0, "weak_min": 1, "weak_over": 1},
        removals_from_act=2,
        note="two Frost cards and Block first, one Weak source, Focus, Orb slots and Block Powers while under four; "
             "draw and energy added in every act, attacks only while few are held"),
}

PLANS_BY_BUILD = {"v0.107.1": PLANS_V0107, "v0.111.0": PLANS_V0111}


def plans(build=None) -> dict:
    """Every character's deck plan for a game build (the build in use when None)."""
    return PLANS_BY_BUILD[builds.get(build).key]


def plan_for(character, build=None) -> DeckPlan:
    """The deck plan of ``character`` (a game ID or display name) on a game build (the one in use when None);
    Ironclad's for a character without one."""
    table = plans(build)
    return table.get(character_key(character) or "IRONCLAD", table["IRONCLAD"])


def phase_rules(plan: DeckPlan) -> dict:
    roles = [r.replace("_", " ") for r in plan.early_roles]
    first = ", ".join(roles[:-1]) + (" and " if roles[:-1] else "") + roles[-1]
    lead = ", then ".join(r.replace("_", " ") for r in plan.late_first)
    lead = (lead[0].upper() + lead[1:].replace("block", "Block")
            + (" comes first while it is missing" if len(plan.late_first) == 1 else " come first while missing"))
    thin = plan.thin_from_act
    build = ("the rest of act 1: fill missing or short roles first and skip cards that worsen the balance; a solid "
             "neutral card is still worth taking, because the deck is still too small to win act 1 boss fights")
    if thin is None or thin > 2:        # the deck keeps building past act 1
        ahead = ", then ".join(r.replace("_", " ").replace("block", "Block") for r in plan.build_first)
        build = (f"after the first picks, {'in every act' if thin is None else f'until act {thin}'}: fill missing or short "
                 f"roles and tracks first{f' ({ahead} leading)' if ahead else ''} and skip cards that worsen the balance; "
                 "a solid card that fills nothing (draw, energy, a debuff) is still worth taking")
    return {
        EARLY: f"the first {plan.early_picks} cards added in act 1: {first} "
               f"{'come first and are' if len(plan.early_roles) != 1 else 'comes first and is'} never "
               "over-supplied; a card of another role is taken only when it is clearly the strongest card offered",
        BUILD: build,
        LATE: f"{'every act' if (thin or 9) <= 1 else f'act {thin} onwards'}: {lead}"
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
# v0.111 lowered three Star costs (Alignment and Resonance 3 to 2, Guiding Star 2 to 1); the Stars gained did not change.
_STAR_COSTS = {"v0.107.1": STAR_COST, "v0.111.0": {**STAR_COST, "ALIGNMENT": 2, "RESONANCE": 2, "GUIDING_STAR": 1}}

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
    return _STAR_COSTS[builds.active().key].get(_cid(card), 0)


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


def _track_weak(plan, deck_size, marks, names, act=None):
    """Weak (or lost enemy Strength): enough sources to keep it on the enemy, and no more than the deck uses."""
    count = marks["weak"]
    per, least = plan.targets.get("weak_per_cards", 10), plan.targets.get("weak_min", 2)
    over = plan.targets.get("weak_over", 2)
    target = max(least, math.ceil(deck_size / per)) if per else least
    status = "missing" if count == 0 else "short" if count < target else "covered" if count < target + over else "over_supplied"
    wanted = f"one per {per} cards in the deck (at least {least})" if per else f"{least} in the deck"
    return {"count": count, "target": target, "cards": names["weak"], "status": status,
            "rule": f"cards that apply Weak or lower enemy Strength, the starter's included: {wanted} keeps what "
                    f"enemies hit with down; {over} more than that is too many"}


def _track_summon(plan, deck_size, marks, names, act=None):
    """The Necrobinder: Osty's HP is Block that lasts, so the deck keeps Summon cards."""
    count, target = marks["summon"], plan.targets.get("summon_min", 2)
    status = "missing" if count == 0 else "short" if count < target else "covered"
    return {"count": count, "target": target, "cards": names["summon"], "status": status,
            "rule": f"cards that Summon, the starter's included: at least {target} keep Osty alive to take hits"}


def _track_osty(plan, deck_size, marks, names, act=None):
    count, allowed = marks["osty_dies"], plan.targets.get("osty_dies_ok", 0)
    rule = "cards that kill Osty for a one-time effect: each one gives up the HP the Summon cards built"
    if allowed:
        rule += f"; {allowed} can be carried, another is too many"
    return {"count": count, **({"allowed": allowed} if allowed else {}), "cards": names["osty_dies"],
            "status": "over_supplied" if count > allowed else "covered", "rule": rule}


def _deck_target(plan, act):
    """The deck size the plan holds to in ``act`` (an unknown act is read as act 1); None when it holds to none."""
    if plan.deck_targets:
        return plan.deck_targets.get(act if type(act) is int else 1)
    return plan.deck_target


def _track_deck_size(plan, deck_size, marks, names, act=None):
    target = _deck_target(plan, act)
    status = "covered" if target is None or deck_size < target else "over_supplied"
    held = (f"the {target} cards this plan holds to" + (" in this act" if plan.deck_targets else "")
            if target is not None else "no target in this act")
    return {"count": deck_size, "target": target, "cards": [], "status": status,
            "rule": f"deck size against {held}: at or above a target a card is added only "
                    "when it fills a missing or short role or track or adds scaling the deck is not yet full of, and "
                    "a removal comes before an addition"}


def _track_stars(plan, deck_size, marks, names, act=None):
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


def _track_frost(plan, deck_size, marks, names, act=None):
    """The Defect: Frost Orbs are Block that repeats, and a bigger deck needs more cards that Channel them."""
    count = marks["frost"]
    target = max(plan.targets.get("frost_min", 2), math.ceil(deck_size / plan.targets.get("frost_per_cards", 10)))
    most = plan.targets.get("frost_max")
    status = ("missing" if count == 0 else "short" if count < target else
              "over_supplied" if most is not None and count >= most else "covered")
    return {"count": count, "target": target, "cards": names["frost"], "status": status,
            "rule": f"cards that Channel Frost: one per {plan.targets.get('frost_per_cards', 10)} cards in the deck (at "
                    f"least {plan.targets.get('frost_min', 2)}) give the deck Block that repeats every turn"
                    + (f"; from {most} on another adds little" if most is not None else "")}


_TRACKS = {"weak": _track_weak, "summon": _track_summon, "osty": _track_osty, "deck_size": _track_deck_size,
           "stars": _track_stars, "frost": _track_frost}
NEEDS, EXCESS = ("missing", "short"), ("over_supplied", "full")


def _cap(value, act) -> int:
    """A role's cap in ``act``: the number itself, or the act's entry of a per-act list (an unknown act is act 1)."""
    if isinstance(value, (list, tuple)):
        return value[min(max(act if type(act) is int else 1, 1), len(value)) - 1]
    return value


def _limits(plan: DeckPlan, act=None) -> dict:
    return {"front_min": plan.front_min, "front_lead": plan.front_lead, "scaling_full": plan.scaling_full,
            "deck_target": _deck_target(plan, act),
            **({"star_deficit": plan.targets.get("star_deficit", 3)} if "stars" in plan.tracks else {}),
            **({"caps": {role: _cap(value, act) for role, value in plan.caps.items()}} if plan.caps else {}),
            **({"never_over": list(plan.never_over)} if plan.never_over else {}),
            **({"scaling_min": dict(plan.scaling_min)} if plan.scaling_min else {})}


def _statuses(counts, limits) -> dict:
    out = {}
    caps, never, least = limits.get("caps") or {}, limits.get("never_over") or (), limits.get("scaling_min") or {}
    for role, other in (FRONT, FRONT[::-1]):
        mine, theirs = counts[role], counts[other]
        ahead = mine >= limits["front_min"] and mine - theirs >= limits["front_lead"] and role not in never
        out[role] = ("over_supplied" if ahead or (role in caps and mine >= caps[role]) else
                     "missing" if mine == 0 else
                     "short" if theirs >= limits["front_min"] and theirs - mine >= limits["front_lead"] else "balanced")
    for role in SCALING:
        out[role] = ("missing" if counts[role] == 0 else "short" if counts[role] < least.get(role, 1) else
                     "covered" if counts[role] < limits["scaling_full"] else "full")
    return out


def _rules(plan: DeckPlan) -> dict:
    """``RULES`` as this plan applies them: its own limits on damage, Block and scaling are said where it has any."""
    if not (plan.caps or plan.never_over or plan.scaling_min):
        return RULES
    front = RULES["front_line_status"]
    for role, cap in plan.caps.items():
        if isinstance(cap, (list, tuple)):
            cap = (f"{cap[0]} added cards in act 1" + "".join(f", {n} in act {i}" for i, n in enumerate(cap[1:-1], 2))
                   + f" and {cap[-1]} after")
        else:
            cap = f"{cap} added cards"
        front += f"; {role.replace('block', 'Block')} is also over-supplied from {cap}, whatever the other holds"
    for role in plan.never_over:
        front += f"; {role.replace('block', 'Block')} is never over-supplied"
    scaling = RULES["scaling_status"] + "".join(
        f"; {role.replace('_', ' ').replace('block', 'Block')} is short below {n} sources" for role, n in plan.scaling_min.items())
    return {**RULES, "front_line_status": front, "scaling_status": scaling}


def phase(act, added, plan: DeckPlan | None = None) -> str:
    """Which deck-building phase applies: by act, and in act 1 by the cards added beyond the starter.
    An unknown act is read as act 1. Without a plan, Ironclad's of the build in use."""
    plan = plan or plan_for("IRONCLAD")
    thin = plan.thin_from_act
    if act is not None and act >= 2:
        return LATE if thin is not None and act >= thin else BUILD
    if added < plan.early_picks:
        return EARLY
    return LATE if thin is not None and thin <= 1 else BUILD


def _priority(stage, status, plan, tracks) -> list:
    """The roles and tracks to take first in this phase, most urgent first."""
    wanted = [name for name in plan.tracks if tracks[name]["status"] in NEEDS]
    if stage == EARLY:
        return wanted + list(plan.early_roles)
    short = [r for r in ROLES if status[r] in NEEDS]
    lead = [r for r in (plan.late_first if stage == LATE else plan.build_first) if r in short]
    return wanted + lead + [r for r in short if r not in lead]


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
    act = act if type(act) is int else None
    limits = _limits(plan, act)
    status = _statuses(counts, limits)
    tracks = {name: _TRACKS[name](plan, size, marks, {k: sorted(v)[:MAX_NAMES] for k, v in mark_names.items()}, act)
              for name in plan.tracks}
    stage = phase(act, added, plan)
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
        "limits": limits,
        "rules": _rules(plan),
    }


def _track_effect(state, marks, earns_slot) -> tuple:
    """(tracks the card fills, tracks it worsens) for the deck ``state`` describes.

    ``earns_slot``: the card fills a missing or short role, or adds scaling the deck is not yet full of,
    which is what a deck held to a target size still makes room for."""
    tracks, fills, worse, limits = state.get("tracks") or {}, [], [], state["limits"]
    status = {name: row["status"] for name, row in tracks.items()}
    if "weak" in status and marks.get("weak"):
        (fills if status["weak"] in NEEDS else worse if status["weak"] in EXCESS else []).append("weak")
    if "summon" in status and marks.get("summon") and status["summon"] in NEEDS:
        fills.append("summon")
    if "osty" in status and marks.get("osty_dies") and tracks["osty"]["count"] >= tracks["osty"].get("allowed", 0):
        worse.append("osty")
    if "frost" in status and marks.get("frost"):
        (fills if status["frost"] in NEEDS else worse if status["frost"] in EXCESS else []).append("frost")
    if "stars" in status:
        gain, cost = marks.get("star_gain", 0), marks.get("star_cost", 0)
        if status["stars"] == "short":
            (fills if gain > cost else worse if cost > gain else []).append("stars")
        elif status["stars"] == "over_supplied":
            (fills if cost > gain else worse if gain > cost else []).append("stars")
        elif cost > gain and (cost - gain) - tracks["stars"]["count"] > limits.get("star_deficit", 3):
            worse.append("stars")       # balanced now, short after this card
    if status.get("deck_size") in EXCESS and not fills and not earns_slot:
        worse.append("deck_size")
    return fills, worse


def offer_effects(state, offers) -> list:
    """What taking each offered card would do to the balance.

    ``offers`` are (action_id, card) pairs. ``adds_to_over_supplied`` is judged on the deck as it
    would be after the pick, so the card that tips a role over is marked, not only the ones after it.
    In the early phase the plan's first roles count as needed and never as over-supplied.
    ``fills_priority`` marks a card for the phase's most urgent role or track. A character's tracks
    are judged with the roles: a card can fill one (Weak while it is short) or worsen one (a Star
    cost the deck's Stars do not pay for, a card that kills Osty, any card that neither fills a need
    nor adds scaling once the deck is at its target size).
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
        grows = any(r in SCALING and counts[r] < limits["scaling_full"] for r in roles)
        track_fills, track_over = _track_effect(state, card_marks(card), bool(fills) or grows)
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
        if row["target"] is None:
            return f"deck size {row['count']}, no target in this act"
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
            f"scaling role is covered by one lasting source and full at {limits['scaling_full']}."
            + "".join(f" {role.replace('block', 'Block').capitalize()} is also over-supplied from {cap} added cards."
                      for role, cap in (limits.get("caps") or {}).items())
            + "".join(f" {role.replace('block', 'Block').capitalize()} is never over-supplied."
                      for role in limits.get("never_over") or ())
            + "".join(f" {role.replace('_', ' ').replace('block', 'Block').capitalize()} is short below {n} sources."
                      for role, n in (limits.get("scaling_min") or {}).items()))
