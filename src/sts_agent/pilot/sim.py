"""Whole simulated fights: the measurement every profile is judged by.

A ``Scenario`` is a fight as a logged run entered it (deck, relics, potions, HP, the
encounter). ``play`` fights it in the simulator with one profile, the hidden draw order
and enemy rolls fixed by a world seed, so two profiles can be compared on the same
fights. ``bench`` compares named arms; ``tune`` searches the profile space for one
encounter and records the winner.

These are simulator results. The engine is the one Spire Mentor verifies against the
game, but nothing here is a live fight.
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import engine
from .profiles import Profile, default_profile, holds_potions, hp_carry, load_tuned, mentor_profile, profile_for, save_tuned
from .search import CORES, decide, is_potion_action

# What a fight's outcome is worth to the run. A win is 1, plus the engine's own default
# worth of the HP it keeps (full HP = 2/7) scaled by how much of that HP survives to the next
# fight (``profiles.hp_carry``), plus a potion's worth per potion kept; a loss ends the run.
HP_WORTH = 2.0 / 7.0
POTION_WORTH = 0.03
LOSS_VALUE = -1.25
MAX_ACTIONS = 2000
ACCEPT_SE = 2.0     # how many standard errors a tuned candidate must beat the default by
SNAPSHOT_PATH = Path(__file__).with_name("snapshots.json")   # fight starts kept in the repository, for a machine without the logs


def mentor_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SpireMentor" / "logs"


@dataclass(frozen=True)
class Scenario:
    name: str                # the log it came from
    encounter: str
    tier: str
    act: int
    floor: int | None
    deck: tuple
    relics: tuple
    potions: tuple
    hp: int
    max_hp: int
    ascension: int
    drop_chance_pct: int | None = None

    def build(self, rs, world_seed):
        return rs.build_scenario_state(world_seed, "Ironclad", list(self.deck), self.hp, self.max_hp,
                                       list(self.relics), self.encounter, self.ascension,
                                       potions=list(self.potions) or None)

    def against(self, encounter: dict) -> Scenario:
        """The same run snapshot entering a different encounter."""
        return replace(self, encounter=encounter["id"], tier=encounter["tier"], act=encounter["act"],
                       name=f"{self.name}@{encounter['id']}")


def scenarios_from_logs(rs, log_dir=None) -> tuple[list[Scenario], Counter]:
    """Every fight start in Spire Mentor's per-fight logs that the engine can rebuild."""
    table = engine.encounters(rs)
    out, skipped = [], Counter()
    for path in sorted(glob.glob(str(Path(log_dir or mentor_log_dir()) / "*.jsonl"))):
        meta, first = {}, None
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "meta":
                    meta.update(row.get("data") or {})
                elif row.get("type") == "state" and (row.get("data") or {}).get("decision") == "combat_play":
                    first = row["data"]
                    break
        if first is None:
            skipped["no combat state"] += 1
            continue
        if first.get("round") != 1:
            skipped["log starts mid-fight"] += 1
            continue
        if str(meta.get("character") or "Ironclad").lower() != "ironclad":
            skipped["not Ironclad"] += 1
            continue
        player = first.get("player") or {}
        room = (first.get("context") or {}).get("room_type")
        enc = engine.find_encounter(table, [e.get("kind") or e.get("name") for e in first.get("enemies") or []], room)
        if enc is None:
            skipped[f"unknown encounter ({room})"] += 1
            continue
        scenario = Scenario(
            name=Path(path).stem, encounter=enc["id"], tier=enc["tier"], act=enc["act"], floor=first.get("floor"),
            deck=tuple(c["id"] + ("+1" if c.get("upgraded") else "") for c in first.get("master_deck") or []),
            relics=tuple(r["id"] for r in player.get("relics") or []),
            potions=tuple(p["id"] for p in player.get("potions") or [] if not p.get("empty") and p.get("id")),
            hp=int(player.get("hp") or 0), max_hp=int(player.get("max_hp") or 0),
            ascension=int(meta.get("ascension") or 0))
        try:
            scenario.build(rs, 1)
        except Exception as exc:            # a card, relic or potion this engine build does not model
            skipped[f"engine cannot build: {str(exc)[:60]}"] += 1
            continue
        out.append(scenario)
    return out, skipped


def save_snapshots(scenarios, path=SNAPSHOT_PATH) -> None:
    """Keep ``scenarios`` in the repository, one per line, so a clone without the logs can tune with them."""
    Path(path).write_text("[\n" + ",\n".join(json.dumps(asdict(s)) for s in scenarios) + "\n]\n", encoding="utf-8")


def load_snapshots(rs, path=SNAPSHOT_PATH) -> list[Scenario]:
    """The kept fight starts this engine can rebuild."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    out = []
    for row in rows:
        scenario = Scenario(**{**row, "deck": tuple(row["deck"]), "relics": tuple(row["relics"]), "potions": tuple(row["potions"])})
        try:
            scenario.build(rs, 1)
        except Exception:
            continue
        out.append(scenario)
    return out


def all_scenarios(rs, log_dir=None) -> tuple[list[Scenario], Counter]:
    """This machine's logged fight starts, plus the kept ones its logs do not hold; in one order everywhere."""
    found, skipped = scenarios_from_logs(rs, log_dir)
    names = {s.name for s in found}
    return sorted(found + [s for s in load_snapshots(rs) if s.name not in names], key=lambda s: s.name), skipped


def _board(rs, state) -> tuple[int, int]:
    player = rs.state_to_dict(state)["player"]
    return int(player.get("hp") or 0), len(player.get("potions") or [])


def play(rs, scenario: Scenario, profile: Profile, world_seed: int) -> dict:
    """Fight ``scenario`` to its end with ``profile``; the outcome and what it cost."""
    state = scenario.build(rs, world_seed)
    _, potions_start = _board(rs, state)
    decisions = raced = changed = 0
    thinking = 0.0
    potion_used = False
    started = time.perf_counter()
    for step in range(MAX_ACTIONS):
        if state.terminal_value()[0]:
            break
        legal = state.legal_actions()
        if not legal:
            break
        if len(legal) == 1:
            action = legal[0]
        else:
            d = decide(rs, state, profile, seed=(world_seed * 7919 + step) & 0x7FFFFFFF, hold=holds_potions(scenario.tier),
                       drop_chance_pct=scenario.drop_chance_pct, potion_used=potion_used)
            action = d.action
            decisions += 1
            thinking += d.seconds
            raced += bool(d.raced)
            changed += d.rule in ("race", "relic")
        state.apply_step(action)
        potion_used |= is_potion_action(action)
    terminal, value = state.terminal_value()
    hp, potions = _board(rs, state)
    won = bool(terminal and value > 0)
    hp = hp if won else 0
    return {"scenario": scenario.name, "encounter": scenario.encounter, "tier": scenario.tier, "act": scenario.act,
            "world": world_seed,
            "won": won, "finished": bool(terminal), "hp_start": scenario.hp, "hp_end": hp, "max_hp": scenario.max_hp,
            "hp_lost": scenario.hp - hp, "potions_used": potions_start - potions, "potions_left": potions,
            "decisions": decisions, "raced": raced, "race_changed": changed,
            "seconds_per_decision": thinking / decisions if decisions else 0.0,
            "seconds": time.perf_counter() - started,
            "value": run_value(won, hp, scenario.max_hp, potions, hp_carry(scenario.tier, scenario.act))}


def run_value(won, hp, max_hp, potions_left, carry=1.0) -> float:
    if not won:
        return LOSS_VALUE
    return 1.0 + HP_WORTH * carry * hp / max(1, max_hp) + POTION_WORTH * potions_left


def arm_profile(arm, scenario: Scenario, tuned) -> Profile:
    """Named arms of a comparison. A ``Profile`` is used as it is for every fight."""
    if isinstance(arm, Profile):
        return arm
    if arm == "mentor":
        return mentor_profile(scenario.tier)
    if arm == "pilot":
        return profile_for(scenario.encounter, scenario.tier, scenario.act, tuned)
    if arm == "default":
        return default_profile(scenario.tier, scenario.act)
    if arm == "mentor+race":            # Mentor's budget, the pilot's pick: isolates the pick
        return replace(mentor_profile(scenario.tier), pick="race", race_sims=2000)
    if arm == "mentor+carry":           # Mentor's search, HP priced by what survives the fight: isolates the carry
        carry = hp_carry(scenario.tier, scenario.act)
        return replace(mentor_profile(scenario.tier), win_hp_carry=carry if carry != 1.0 else None)
    if arm == "default-engine":         # the pilot's budget, Mentor's pick: isolates the budget
        return replace(default_profile(scenario.tier, scenario.act), pick="engine")
    if arm.startswith("mentor:"):       # Mentor's profile with fields changed: "mentor:sims=64000;threads=16"
        return replace(mentor_profile(scenario.tier), **json.loads(
            "{" + ",".join(f'"{k}":{v}' for k, v in (kv.split("=") for kv in arm[7:].split(";"))) + "}"))
    raise ValueError(f"unknown arm {arm!r}")


def run_fights(rs, jobs, parallel=None, progress=None) -> list[dict]:
    """``jobs`` are (scenario, profile, world_seed); results come back in the same order."""
    def one(job):
        scenario, profile, world = job
        try:
            return play(rs, scenario, profile, world)
        except Exception as exc:            # one unplayable fight must not cost the batch
            return {"scenario": scenario.name, "encounter": scenario.encounter, "tier": scenario.tier,
                    "world": world, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if parallel is None:
        widest = max((p.threads for _, p, _ in jobs), default=1)
        parallel = max(1, CORES // max(1, min(widest, CORES)))
    out = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="pilot-fight") as pool:
        pending = {pool.submit(one, job): i for i, job in enumerate(jobs)}
        for done, future in enumerate(as_completed(pending), 1):
            out[pending[future]] = future.result()
            if progress:
                progress(done, len(jobs), out[pending[future]])
    return out


def summarize(results) -> dict:
    ok = [r for r in results if "error" not in r]
    if not ok:
        return {"fights": 0, "errors": len(results)}
    mean = statistics.fmean
    return {"fights": len(ok), "errors": len(results) - len(ok),
            "win_rate": round(mean(r["won"] for r in ok), 4),
            "hp_lost": round(mean(r["hp_lost"] for r in ok), 2),
            "hp_end": round(mean(r["hp_end"] for r in ok), 2),
            "potions_used": round(mean(r["potions_used"] for r in ok), 2),
            "value": round(mean(r["value"] for r in ok), 4),
            "seconds_per_decision": round(mean(r["seconds_per_decision"] for r in ok), 3),
            "race_changed_per_fight": round(mean(r["race_changed"] for r in ok), 2)}


def paired(base, other) -> dict:
    """``other`` minus ``base`` on the fights both played, with the standard error of the mean difference."""
    theirs = {(o["scenario"], o["world"]): o for o in other if "error" not in o}
    pairs = [(b, theirs[b["scenario"], b["world"]]) for b in base
             if "error" not in b and (b["scenario"], b["world"]) in theirs]
    if len(pairs) < 2:
        return {"fights": len(pairs)}
    out = {"fights": len(pairs)}
    for key in ("value", "hp_end", "won"):
        diffs = [float(o[key]) - float(b[key]) for b, o in pairs]
        out[key] = round(statistics.fmean(diffs), 4)
        out[key + "_se"] = round(statistics.stdev(diffs) / len(diffs) ** 0.5, 4)
    return out


def bench(rs, scenarios, arms, worlds=2, tuned=None, parallel=None, progress=None, sink=None) -> dict:
    """Play every scenario in ``worlds`` worlds with every arm; the first arm is the baseline.

    ``sink(row)`` receives each finished fight (with its ``arm``) at once, so a long
    comparison that is cut short keeps what it played; ``report`` rebuilds the table from them.
    """
    tuned = load_tuned() if tuned is None else tuned
    names = [a if isinstance(a, str) else f"profile{i}" for i, a in enumerate(arms)]
    fights = {}
    for name, arm in zip(names, arms):
        jobs = [(s, arm_profile(arm, s, tuned), 1000 + w) for s in scenarios for w in range(worlds)]

        def tick(i, n, result, name=name):
            if sink:
                sink({"arm": name, **result})
            if progress:
                progress(name, i, n, result)
        fights[name] = run_fights(rs, jobs, parallel, tick)
    return report(fights)


def report(fights: dict) -> dict:
    """The comparison table of ``{arm: [fight rows]}``; the first arm is the baseline."""
    names = list(fights)
    tiers = sorted({r["tier"] for rows in fights.values() for r in rows}, key=["Weak", "Normal", "Elite", "Boss"].index)
    out = {"arms": names, "by_tier": {}}
    for tier in tiers + ["all"]:
        keep = lambda rows: [r for r in rows if tier == "all" or r["tier"] == tier]
        out["by_tier"][tier] = {name: summarize(keep(fights[name])) for name in names}
        for name in names[1:]:
            out["by_tier"][tier][f"{name} - {names[0]}"] = paired(keep(fights[names[0]]), keep(fights[name]))
    return out


def read_fights(path) -> dict:
    """``{arm: [fight rows]}`` from a file ``bench --out`` wrote; a fight played twice keeps its last row."""
    arms: dict = defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            arms[row.pop("arm")][row["scenario"], row["world"]] = row
    return {arm: list(rows.values()) for arm, rows in arms.items()}


# One change each to the default of an encounter's tier and act. The carries run from 0 (what a
# win keeps is worth nothing, so spend it) to 1 (the engine's full worth).
KNOBS = {
    "default": {},
    "engine-pick": {"pick": "engine"},
    "wide-draws": {"draw_widening_c": 4.0},
    "wide-turns": {"endturn_widening_c": 23.148},
    "leaf-playouts-3": {"leaf_playouts": 3},
    "dominance": {"dominance_pruning": True},
    "hp-half": {"win_hp_carry": 0.5},
    "potions-free": {"win_potion_carry": 0.0},
    "potions-half": {"win_potion_carry": 0.5},
    "deep-race": {"race_band": 0.05, "race_width": 6, "race_reps": 3},
}


def candidate(name, tier, act=None, tuning=False) -> Profile:
    """Candidate ``name`` for an encounter of ``tier`` in ``act``; ``"mentor"`` is Mentor's own profile.

    ``tuning`` gives it Mentor's search budget instead of the tier's: candidates are compared
    cheaply and on equal thinking time, and the knobs that win are then used at the tier's budget.
    """
    if name == "mentor":
        return mentor_profile(tier)
    base = default_profile(tier, act)
    knobs = dict(KNOBS[name])
    if tuning:
        mentor = mentor_profile(tier)
        base = replace(base, sims=mentor.sims, threads=mentor.threads, race_sims=max(1000, mentor.sims // 8))
    return replace(base, **knobs)


def tune(rs, encounter: dict, pool: list[Scenario], fights=8, rounds=3, progress=None, folder=None) -> dict:
    """Successive halving over ``KNOBS`` on one encounter, with Mentor's profile alongside.

    ``pool`` are run snapshots (logged fight starts of the encounter's act). Every surviving
    candidate plays the same fights each round, the better half by mean run value stays, and
    the fight count doubles; ``default`` and ``mentor`` always stay, as the two references.
    The encounter keeps the default knobs unless a candidate beats them on the last round by
    more than ``ACCEPT_SE`` standard errors of the paired difference. With ten candidates the
    best of them is ahead by one standard error from luck alone more often than not (a 16-fight
    trial on one elite "found" such a winner), hence two, and hence enough fights to reach it.
    """
    tier, act = encounter["tier"], encounter["act"]
    usable = []
    for s in (s.against(encounter) for s in pool):
        try:
            s.build(rs, 1)
            usable.append(s)
        except Exception:
            continue
    if not usable:
        raise ValueError(f"no logged run snapshot can enter {encounter['id']}")
    alive = ["mentor", *KNOBS]
    history, played, results = [], 0, {}
    for rnd in range(rounds):
        n = fights * 2 ** rnd
        batch = [(usable[(played + i) % len(usable)], 5000 + played + i) for i in range(n)]
        played += n
        results = {}
        for name in alive:
            jobs = [(s, candidate(name, tier, act, tuning=True), w) for s, w in batch]
            results[name] = run_fights(rs, jobs, None, (lambda i, m, r, name=name: progress(rnd, name, i, m, r)) if progress else None)
        ranked = sorted(alive, key=lambda name: -summarize(results[name]).get("value", LOSS_VALUE))
        history.append({"round": rnd + 1, "fights": n, "ranked": [{"name": name, **summarize(results[name])} for name in ranked]})
        if rnd < rounds - 1:
            keep = ranked[:max(2, (len(ranked) + 1) // 2)]
            alive = keep + [name for name in ("default", "mentor") if name not in keep]
    chosen, margin = "default", {}
    for name in ranked:                      # best first
        if name == "default":
            break
        versus = paired(results["default"], results[name])
        if versus.get("fights", 0) >= 2 and versus["value"] > ACCEPT_SE * versus["value_se"]:
            chosen, margin = name, versus
            break
    evidence = {"chosen": chosen, "versus_default": margin,
                "versus_mentor": paired(results["mentor"], results[chosen]) if chosen != "mentor" else {},
                "last_round": history[-1], "snapshots": len(usable),
                "kind": "simulator, candidates at Mentor's search budget"}
    save_tuned(encounter["id"], candidate(chosen, tier, act), evidence, **({"folder": folder} if folder else {}))
    return {"encounter": encounter["id"], "history": history, **evidence}
