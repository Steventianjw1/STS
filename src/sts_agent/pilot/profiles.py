"""How the pilot searches and picks in one fight.

A ``Profile`` is the whole tailoring surface: the search budget, the engine's search
knobs, what a win is worth by the HP and potions it keeps, how the root move is picked,
whether potions are kept for the fights that need them, and whether the relics held
decide between moves the search scores alike. ``MENTOR`` reproduces Spire Mentor's
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
