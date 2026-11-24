"""How the pilot searches and picks in one fight.

A ``Profile`` is the whole tailoring surface: the search budget, the engine's search
knobs, what a win is worth by the HP and potions it keeps, how the root move is picked,
whether potions are kept for the fights that need them, and what decides between moves
the search scores alike (the relics held, the least useful card to give up). ``MENTOR`` reproduces Spire Mentor's
deployed advisor and is the baseline every other profile is measured against. Profiles
tuned per encounter by ``sim.tune`` are kept in ``tuned/`` beside this file, one
``<ENCOUNTER_ID>.json`` each, and override the tier defaults. One file per encounter lets
machines tune different encounters and merge their results through Git without a conflict.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

TUNED_DIR = Path(__file__).with_name("tuned")


@dataclass(frozen=True)
class Profile:
    sims: int = 8000                        # simulations of the main search
    threads: int = 8                        # independent trees the budget is split across (clamped to the cores)
    c_puct: float | None = None             # exploration; None = the engine authors' rule for the per-tree budget
    draw_widening_c: float | None = None    # how many different draws a node considers; None = deployed 2.0
    endturn_widening_c: float | None = None     # same for what follows an end of turn; None = deployed
    endturn_widening_alpha: float | None = None
    leaf_playouts: int | None = None        # playouts averaged per search leaf; None = one
    dominance_pruning: bool = False         # drop card orders the engine proves no better
    rollout_policy: str | None = None       # "net" / "heuristic" / "uniform"; None = deployed (the playout net)
    win_hp_carry: float | None = None       # worth of the HP a win keeps; None = the engine's default
    win_potion_carry: float | None = None   # worth of the potions a win keeps; None = the engine's default
    pick: str = "race"                      # "engine": Mentor's root rule; "race": re-search the close options
    race_band: float = 0.03                 # options within this much value of the best are raced (0.03 ~ 8 HP of 80)
    race_width: int = 4                     # at most this many options raced
    race_sims: int = 2000                   # simulations of each re-search, all through one option
    race_reps: int = 2                      # independent re-searches per raced option
    relic_tie: float = 0.0                  # raced moves this close count as equal and the relics held decide (0 = off)
    choice_tie: float = 0.01                # cards to give up that score this close are equal; the least useful goes (0 = off)
    matched_luck: bool = False              # the engine's common random numbers: one decision's options meet the same luck
    hold_potions: bool | None = None        # keep potions while the fight is not in doubt; None = the tier's rule

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# Spire Mentor's advisor as launched by the mod: 4000 simulations (16000 at bosses) on 4 trees, its own pick.
MENTOR = Profile(sims=4000, threads=4, pick="engine", hold_potions=False)
MENTOR_BOSS = replace(MENTOR, sims=16000)

# Untuned starting points: twice Mentor's thinking on twice the trees (the same simulations per
# tree, which its exploration constant was measured at), and the race. An encounter with a tuned
# profile does not use these.
DEFAULTS = {
    "Weak": Profile(),
    "Normal": Profile(),
    "Elite": Profile(),
    "Boss": Profile(sims=32000, race_sims=4000),
}

# After an act-1 or act-2 boss the game heals about 80% of the missing HP (six logged A10 runs:
# 28/80 -> 69, 40/77 -> 69, 21/77 -> 65, 13/80 -> 66, 17/67 -> 57, 53/80 -> 74), so HP kept in
# those fights is worth a fifth of HP kept anywhere else. This prices a fight's OUTCOME only.
# Searching those fights with the same low worth (win_hp_carry 0.2) was measured and lost:
# 64.3% of boss fights won against 71.4% (84 paired simulated fights, -7.1 +-3.3 points). HP in
# hand is also the margin that absorbs the search's own misjudgements, so no default lowers it.
HEALED_BOSS_HP_CARRY = 0.2

# Which card to exhaust. In boss fights the race scores two copies of the same card as far apart as
# two different cards (a quarter of such pairs by more than 0.0125 and 0.0084), so below about 0.01
# the search does not tell cards apart and its pick among them is chance: Mentor's search made 130
# of 211 such picks by its "most searched of the equal moves" rule, and 57 gave up a real card while
# a Strike, a Defend, a Status or a Curse was offered. ``choice_tie`` 0.01 gives up the least useful
# of the cards that close (``search._keep_worth``). Against the saved tuning fights it left 237 of
# 264 boss fights unchanged and the rest no worse (+0.8 +-0.8 points of wins, +0.1 +-0.1 HP); on 85
# logged choices it cut those odd picks from 21 to 12 and the same card came back from a second
# search 71% of the time instead of 65%.

# Matched luck (the engine's ``common_random_numbers``: the options of one decision are searched on
# the same draws and enemy rolls). With Mentor's search a second search of the same boss decision
# gave the same pick 80% of the time instead of 70%, but under the pilot's race it changed nothing
# (77% against 79%), and against the saved tuning fights it won +1.1 +-1.7 points over 264 boss
# fights and +0.9 +-2.1 over 216 fresh act-3 ones. One boss stood out, Aeonglass at +9.4 +-2.7 over
# 96 fights, so it is a candidate for ``sim.tune`` to take per encounter and no default sets it.

# The engine's price of the HP a won fight keeps, as a multiple of its own (its STS2_RUNAI_HP_WEIGHT).
# The engine reads it once per process, so it prices every search of the process alike, Mentor's
# baseline arm included; ``engine.load`` sets it when this is not 1. Measured in boss fights against
# the saved tuning fights (same deck, same world): at 2 the pilot's default won +0.4 +-2.0 points
# more and ended 1.1 +-0.6 HP higher (264 fights); at 4 Mentor's search won -4.1 +-4.6 points
# (73 fights). One HP a boss fight does not pay for a baseline that no longer is Mentor's, so it
# stays at the engine's own price. Hallway fights and elites were not measured.
HP_WEIGHT = 1.0

# Relics in the card play. The engine simulates every relic inside the fight, so a move that is
# better for a relic already scores better, and two ways of pushing relics further were measured on
# logged decks given three of Nunchaku, Tuning Fork, Iron Club, Mummified Hand, Permafrost, Pen Nib,
# Joss Paper, Pocket Watch and Pael's Legion (elites and hallway fights, the tier default otherwise):
#   - racing the best move of every other relic outcome from twice as far (0.06): 69 paired fights,
#     +0.4 +-0.3 HP, -1.4 +-1.5 points of wins, 12% more thinking. Such a move scored 0.03 to 0.16
#     worse once searched on its own, never better. Removed.
#   - ``relic_tie`` 0.005 (the relics decide between raced moves that close): 72 paired fights, all
#     won either way, -1.6 +-0.8 HP a fight; it decided 7% of the decisions and fed the relics 4% more
#     often (+1.2 +-0.6 counter steps and freed cards a fight). The race tells such moves apart
#     better than the rule does, so no default sets it; what it buys is only the counter a relic
#     carries into the next fight, which the search cannot see.


def hp_carry(tier, act) -> float:
    """How much of the HP a win keeps is still there for the next fight."""
    return HEALED_BOSS_HP_CARRY if tier == "Boss" and act in (1, 2) else 1.0


# The search drinks a potion as soon as it sees any gain, and the engine's price of a kept potion
# (8 HP) is below the noise of its own estimates. Simulated on logged fight starts:
#   hallway  114 fights holding potions, Mentor's search: 81% of the potions drunk, 61% of those in
#            round 1, 5.8 HP gained per potion, and every fight won with the belt emptied too.
#   boss     the four hardest, 96 fights: 27.1% won with no potion, 43.8% with two (+16.7 +-5.9 points);
#            16 HP fewer cost 12.5 +-4.6 points there, so a potion at such a boss is worth about 11 HP.
# So below a boss a drink waits until the fight is in doubt (``search.SAFE_WIN``). That hold kept 92%
# of the potions in the hallway fights, still won all 114 and cost 4.8 +-0.7 HP a fight (5.7 HP per
# potion kept). At elites (96 fights, a belt of two) it won exactly the fights free drinking won
# (96.9%), kept 1.1 more potions a fight and cost 8.7 +-1.7 HP (7.9 HP per potion kept); in act 3,
# where elites are in doubt, it let the potions go. Elites are the close call: taking "Elite" out of
# this set spends potions there again.
HOLD_TIERS = frozenset({"Weak", "Normal", "Elite"})


def holds_potions(tier) -> bool:
    """Whether a fight of ``tier`` keeps its potions until it is in doubt."""
    return tier in HOLD_TIERS


def default_profile(tier, act=None) -> Profile:
    return DEFAULTS.get(tier) or DEFAULTS["Normal"]


def mentor_profile(tier) -> Profile:
    return MENTOR_BOSS if tier == "Boss" else MENTOR


def load_tuned(folder=TUNED_DIR) -> dict[str, Profile]:
    """Every tuned profile in ``folder``, by encounter id; a file that does not read as one is left out."""
    out = {}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            out[path.stem] = Profile.from_dict(json.loads(path.read_text(encoding="utf-8"))["profile"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def save_tuned(key, profile: Profile, evidence: dict, folder=TUNED_DIR) -> None:
    """Record ``profile`` as the tuned one for ``key`` together with the numbers that chose it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / f"{key}.json.tmp"          # written whole, then swapped in: a fight starting now reads old or new
    draft.write_text(json.dumps({"profile": profile.to_dict(), "evidence": evidence}, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    draft.replace(folder / f"{key}.json")


def profile_for(encounter_id, tier, act=None, tuned: dict[str, Profile] | None = None) -> Profile:
    """The tuned profile of this encounter, else the default of its tier and act."""
    tuned = load_tuned() if tuned is None else tuned
    return tuned.get(encounter_id) or default_profile(tier, act)
