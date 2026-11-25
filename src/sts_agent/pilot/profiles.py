"""How the pilot searches and picks in one fight.

A ``Profile`` is the whole tailoring surface: the search budget, the engine's search
knobs, what a win is worth by the HP and potions it keeps, how the root move is picked,
whether potions are kept for the fights that need them, and what decides between moves
the search scores alike (the relics held, the least useful card to give up). ``MENTOR`` reproduces Spire Mentor's
deployed advisor and is the baseline every other profile is measured against. Profiles
tuned per encounter by ``sim.tune`` are kept in ``tuned/`` beside this file, one
``<ENCOUNTER_ID>.json`` each, and override the tier defaults. One file per encounter lets
machines tune different encounters and merge their results through Git without a conflict.

Everything above was measured with Ironclad, whose files sit in ``tuned/`` itself. The other four
characters have a folder each, ``tuned/<CHARACTER>/``: per-encounter files of the same shape, and
one ``_character.json`` with what was fitted for the character as a whole (``CharacterTuning``):
the knobs of its tier defaults and how its search's share of won playouts reads as a win chance.
The simulator plays each character's playouts with that character's own net, so that reading is
not Ironclad's.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

TUNED_DIR = Path(__file__).with_name("tuned")
CHARACTER_FILE = "_character.json"      # in a character's folder: what was fitted for the character as a whole

# What a fight's outcome is worth to the run. A win is 1 plus the engine's own worth of the HP it
# keeps (full HP = 2/7); a loss ends the run.
HP_WORTH = 2.0 / 7.0
LOSS_VALUE = -1.25


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
    playouts: int | None = None             # possible worlds each close move is played out in; None = the tier's rule, 0 = off
    playout_sims: int = 1000                # simulations of the small search that plays those fights to the end
    playout_band: float = 0.01              # raced moves within this of the best are played out
    playout_width: int = 2                  # at most this many of them

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
# The line is drawn on the real win chance (``win_chance``): 0.9968 is where the search's own share
# of won playouts is 0.90, the line all of the above was measured at. Letting a fight get worse
# before the potions go costs more than they are worth: at 0.95 the same 96 elite fights kept 0.33
# more potions for 3.4 +-1.0 HP and one more loss, at 0.90 0.38 more for 4.0 +-1.1 HP and two.
HOLD_TIERS = frozenset({"Weak", "Normal", "Elite"})


def holds_potions(tier) -> bool:
    """Whether a fight of ``tier`` keeps its potions until it is in doubt."""
    return tier in HOLD_TIERS


# The search's own win chance is the share of its playouts that were won, and its playouts are
# finished by a fast policy that plays far below the search, so it understates the fight: alone that
# policy wins 39-63% of act-1 boss fights, 5-20% of act-2 and 2-4% of act-3 ones, where the search-
# driven player wins about 86%, 44% and 11%. The order is sound (a higher share is a likelier win),
# so a monotone map turns the share into the chance the fight is won:
# sigmoid(intercept + slope * logit(share)). Fitted on simulated fights the pilot played to the end
# with these defaults, every decision's share against the fight's outcome, each fight counted once:
#   Boss   72 fights:  a share of 0.2% is 1% (0.9% of those fights were won), 5% is 43% (43%),
#          17% is 85% (91%), 38% is 97% (94%), 62% and above is 99% or more (100%).
#   other  96 elite and 144 hallway fights, the hallway ones all won:  0.5% is 8% (12%),
#          5.5% is 53% (43%), 18% is 84% (83%), 37% is 94% (95%), 65% is 98% (98%).
# Refit when the budget, the playouts or the knobs of the search change. Spire Mentor shows its own
# such map on its HUD (fitted at its budget); this one is what the pilot's rules and log read.
WIN_ODDS = {"Boss": (4.394, 1.568), None: (3.355, 1.087)}      # (intercept, slope) by tier; None = every other tier


def win_chance(raw, tier=None, character=None) -> float:
    """The chance the fight is won, for the search's own ``p_win`` in a fight of ``tier``.

    ``character`` (a game ID) reads it by that character's own fit when one was made for the tier.
    """
    odds = character_tuning(character).win_odds if character else {}
    intercept, slope = odds.get("Boss" if tier == "Boss" else "other") or WIN_ODDS.get(tier) or WIN_ODDS[None]
    p = min(max(float(raw), 1e-3), 1 - 1e-3)        # the search reports 0 and 1 for none or all of its playouts
    return 1.0 / (1.0 + math.exp(-(intercept + slope * math.log(p / (1 - p)))))


# Playing close moves out (``search.play_out``): each is followed to the end of the fight through the
# same possible worlds by a small search, in place of the engine's fast playout policy. Measured in
# boss fights: a player of 50 to 400 simulations a move wins no more than that policy does (Vantom
# 50% against 48%, the Queen 4% against 3%); one of 1000 wins more (Vantom 67%, the Insatiable 37%
# against 19%) at about 0.4 s a fight on four cores, still short of the pilot itself (80% and 63%).
# And it finds nothing to choose: over 64 raced boss decisions, each with the race's pick and its
# runner-up played out in the same 48 worlds, the runner-up was +0.3 points of wins and +0.007 value
# ahead, the difference passed two standard errors 3 times (chance gives 3), and picking by one half
# of the worlds gained +0.004 +-0.011 value on the other half. Moves the race holds close are close,
# so no tier plays them out; eight worlds for two moves would cost several seconds a decision.
PLAYOUT_WORLDS: dict = {}     # tier -> possible worlds each close move is played out in; a tier left out plays none


def playouts_for(tier) -> int:
    """How many possible worlds a fight of ``tier`` plays each of its close moves out in (0 = none)."""
    return PLAYOUT_WORLDS.get(tier, 0)


# The other four characters. Everything above was measured with Ironclad; the simulator plays each
# character's playouts with that character's own net, so it was measured again for each (2026-11-25,
# fingerprint cc2c06eb91b21a2a; fight starts of simulated A10 runs, acts 1 and 2; 32 elite and 32 hallway
# fights a character, the pilot at these defaults against Mentor's search on the same fights, and the same
# fight starts again from half HP for fights in doubt). What it showed:
#   - Wins: the pilot won every fight Mentor's search won and one more hallway fight for the Silent and one
#     more hallway and elite fight for the Necrobinder (+3.1 +-3.1 points each); the Regent, the Defect and
#     Ironclad's logged fight starts were level.
#   - HP and potions: with nothing on the belt the pilot ended elites level with Mentor's search (-0.2 +-0.9
#     HP over 48 fights of all five characters); with potions on it, 3.8 +-1.1 HP lower (91 fights) while
#     keeping 0.5 more potions a fight, which is 7.6 HP per potion kept: the price of the hold, measured at
#     7.9 for Ironclad above. No character stands apart from that beyond its own noise.
#   - The win chance: each character's own fit of ``win_chance`` (``sim.fit_win_odds``, 112 fights each)
#     would hold potions above a share of 0.975 (Silent), 0.966 (Defect), 0.937 (Necrobinder) and 0.674
#     (Regent), where Ironclad's reading holds them above 0.900 and Ironclad's own fight starts gave 0.903.
#     A hundred fights do not place that line (the Regent lost 8 of 112, nearly all read as lost from the first
#     decision). What they do settle is whether Ironclad's line is safe for the character: of the decisions it
#     read as safe, 61 to 90 fights' worth per character, at most 0.1 of a fight's worth sat in a lost fight
#     (the line allows 0.2 to 0.3). So every character keeps Ironclad's reading; ``python -m sts_agent.pilot
#     calibrate`` keeps a character's own fit only when its fights show the line to be unsafe.
#   - The knobs: the two that came closest for Ironclad's elites (three playouts a leaf, dominance pruning)
#     were compared with the default on each character's 32 elite fight starts at Mentor's thinking time.
#     The default stayed for the Necrobinder, the Regent and the Defect. For the Silent three playouts a leaf
#     came out 2.0 standard errors ahead (value +0.0053 +-0.0026), and on 64 fresh fights it was not (+0.0021
#     +-0.0509, at twice the time per decision): thirty-two fights "find" such a winner by luck, so it stays
#     the default as well.
# The knobs were not tuned per encounter for these characters, and their bosses were not fought at all: that
# took about two hours a boss on sixteen threads for Ironclad and chose the default 24 times of 24. The
# fight starts for it are kept (``snapshots_<character>.json``, acts 1 and 2 only) and ``tune --character``
# does it. ``tuned/<CHARACTER>/_character.json`` holds what was compared for each character.
@dataclass(frozen=True)
class CharacterTuning:
    """What was fitted for one character as a whole; empty for Ironclad, whose numbers are this module's own."""
    knobs: dict = field(default_factory=dict)       # tier -> Profile fields changed from that tier's default
    win_odds: dict = field(default_factory=dict)    # "Boss" / "other" -> (intercept, slope) of ``win_chance``
    evidence: dict = field(default_factory=dict)


_NO_TUNING = CharacterTuning()
_character_cache: dict = {}


def tuned_dir(character=None, folder=TUNED_DIR) -> Path:
    """Where a character's tuned files are kept: ``tuned/`` for Ironclad, ``tuned/<CHARACTER>/`` otherwise."""
    return Path(folder) if character in (None, "IRONCLAD") else Path(folder) / character


def character_tuning(character, folder=TUNED_DIR, fresh=False) -> CharacterTuning:
    """The character-wide tuning kept for ``character`` (a game ID); nothing for Ironclad or an untuned character."""
    if character in (None, "IRONCLAD"):
        return _NO_TUNING
    key = (character, str(folder))
    if fresh or key not in _character_cache:
        try:
            data = json.loads((tuned_dir(character, folder) / CHARACTER_FILE).read_text(encoding="utf-8"))
            known = {f.name for f in fields(Profile)}
            _character_cache[key] = CharacterTuning(
                knobs={tier: {k: v for k, v in row.items() if k in known} for tier, row in (data.get("knobs") or {}).items()},
                win_odds={k: (float(v[0]), float(v[1])) for k, v in (data.get("win_odds") or {}).items()},
                evidence=data.get("evidence") or {})
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            _character_cache[key] = _NO_TUNING
    return _character_cache[key]


def save_character_tuning(character, *, knobs=None, win_odds=None, evidence=None, folder=TUNED_DIR) -> None:
    """Merge ``knobs`` (by tier), ``win_odds`` (by "Boss"/"other") and ``evidence`` (by key) into the character's file."""
    path = tuned_dir(character, folder) / CHARACTER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for key, new in (("knobs", knobs), ("win_odds", win_odds), ("evidence", evidence)):
        data[key] = {**(data.get(key) or {}), **(new or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_suffix(".json.tmp")
    draft.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    draft.replace(path)
    _character_cache.pop((character, str(folder)), None)


def default_profile(tier, act=None, character=None) -> Profile:
    """The untuned profile of a tier, with the knobs fitted for ``character`` as a whole when there are any."""
    base = DEFAULTS.get(tier) or DEFAULTS["Normal"]
    knobs = character_tuning(character).knobs.get(tier if tier in DEFAULTS else "Normal") if character else None
    return replace(base, **knobs) if knobs else base


def mentor_profile(tier) -> Profile:
    return MENTOR_BOSS if tier == "Boss" else MENTOR


def load_tuned(folder=TUNED_DIR, character=None) -> dict[str, Profile]:
    """Every tuned profile of ``character`` (Ironclad when None) in ``folder``, by encounter id; a file that
    does not read as one is left out."""
    out = {}
    for path in sorted(tuned_dir(character, folder).glob("*.json")):
        try:
            out[path.stem] = Profile.from_dict(json.loads(path.read_text(encoding="utf-8"))["profile"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def save_tuned(key, profile: Profile, evidence: dict, folder=TUNED_DIR, character=None) -> None:
    """Record ``profile`` as the tuned one for ``key`` together with the numbers that chose it."""
    folder = tuned_dir(character, folder)
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / f"{key}.json.tmp"          # written whole, then swapped in: a fight starting now reads old or new
    draft.write_text(json.dumps({"profile": profile.to_dict(), "evidence": evidence}, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    draft.replace(folder / f"{key}.json")


def profile_for(encounter_id, tier, act=None, tuned: dict[str, Profile] | None = None, character=None) -> Profile:
    """The tuned profile of this encounter for ``character``, else the character's default of its tier and act.

    ``tuned`` are that character's per-encounter profiles (``load_tuned``); read from disk when None.
    """
    tuned = load_tuned(character=character) if tuned is None else tuned
    return tuned.get(encounter_id) or default_profile(tier, act, character)
