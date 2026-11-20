"""How the pilot searches and picks in one fight.

A ``Profile`` is the whole tailoring surface: the search budget, the engine's search
knobs, what a win is worth by the HP and potions it keeps, and how the root move is
picked. ``MENTOR`` reproduces Spire Mentor's deployed advisor and is the baseline every
other profile is measured against. Profiles tuned per encounter by ``sim.tune`` are
kept in ``tuned/`` beside this file, one ``<ENCOUNTER_ID>.json`` each, and override the
tier defaults. One file per encounter lets machines tune different encounters and merge
their results through Git without a conflict.
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# Spire Mentor's advisor as launched by the mod: 4000 simulations (16000 at bosses) on 4 trees, its own pick.
MENTOR = Profile(sims=4000, threads=4, pick="engine")
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


def hp_carry(tier, act) -> float:
    """How much of the HP a win keeps is still there for the next fight."""
    return HEALED_BOSS_HP_CARRY if tier == "Boss" and act in (1, 2) else 1.0


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
