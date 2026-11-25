"""Whole simulated runs: the measurement a character's deck plan is judged by.

The simulator plays a run from its first map choice to its end: the map, events, shops, rest sites
and every fight. Spire Mentor's run-policy net of the character (its deployed "champion") makes the
choices between fights, and the simulator's own small search plays the fights. An ``arm`` says who
decides the card rewards:

    net         the champion alone: the reference, and a strong drafter that knows no plan
    shared      the champion's own preferences inside what the plan every character had (Ironclad's)
                leaves open
    character   the same inside the character's own plan (``evidence.deck_balance.PLANS``)
    character:scaling_full=4;targets.frost_min=3;early_roles=block+damage;caps.damage=[5,7,9]
                a variant of that plan, for choosing its numbers (``shared:...`` varies Ironclad's)

A plan arm reads the same evidence the planner and the decision model read (``deck_balance.balance``
and ``offer_effects``) and obeys it literally (``choose_reward``); from the act the plan names
(``removals_from_act``), or where it holds the deck to a size, it also buys the shop's removal before
anything else. That stands in for a model told to obey the plan. It is not that model, nothing here
calls one, and the fights are played by a far smaller search than the pilot's, so the results rank
plans against each other and say little about how far the agent itself gets.

Every fight a run enters is also a fight start for the pilot's comparisons (``sim.Scenario``):
``fight_starts`` picks them out of the finished runs, which is where the four characters without
logged runs get theirs.

These are simulator results. Nothing here is a live run.
"""
from __future__ import annotations

import json
import re
import statistics
import time
import tomllib
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from ..evidence import deck_balance
from ..game.characters import character_key
from . import engine

SOURCES = ("data/mentor_build", "data/mentor_source")     # full checkouts of Mentor's source (docs/PILOT_TUNING.md)
CHAMPIONS = Path("sts2-pycli") / "data" / "ppo"
CARD_TEXT = Path("sts2-pycli") / "localization_eng" / "cards.json"
SIMS, BOSS_SIMS = 1000, 4000        # the engine authors' own evaluation budget for a run's fights
MAX_STEPS = 60000

# How hard a plan steers the champion's preferences at a card reward. The champion is nearly always sure
# of one card (0.97 and more), so its probabilities are first flattened (the fourth root: a card it likes
# 16 times less scores half), then multiplied by the weight of what the plan says of the card, and the
# highest product is taken (skipping has weight 1). So a card for the plan's first priority beats a card
# that fills nothing unless the champion likes it 256 times less, one that fills a missing or short role
# or track unless 16 times less; a card that worsens the balance is never taken; once the plan keeps the
# deck thin a card that fills nothing is taken only when the champion all but rules out skipping; and in
# the first picks a card for the plan's first roles is taken whenever one is offered.
WEIGHT = {"priority": 4.0, "improves": 2.0, "mixed": 1.0, "neutral": 1.0, "neutral_thin": 0.25, "worsens": 0.0}
FLATTEN = 0.25


def source_dir(given=None) -> Path:
    for folder in ([given] if given else SOURCES):
        if (Path(folder) / CHAMPIONS / "champions.toml").is_file():
            return Path(folder)
    raise engine.EngineError("whole-run simulation needs a full checkout of Spire Mentor's source (the run-policy nets "
                             f"and the card text); none found in {given or ' or '.join(SOURCES)}. See docs/PILOT_TUNING.md")


def champion(source, character) -> tuple[str, str | None]:
    """(path of the character's run-policy net, the features it was trained with)."""
    folder = Path(source) / CHAMPIONS
    row = tomllib.loads((folder / "champions.toml").read_text(encoding="utf-8"))[character.lower()]
    return str(folder / row["onnx"]), (row.get("features") or None)


def _render(text) -> str:
    """A card's localized text template as roughly the sentence the game shows (numbers are placeholders)."""
    text = re.sub(r"\{[^{}]*:energyIcons\((\d*)\)\}", lambda m: f"{m.group(1) or 1} Energy", text)
    text = re.sub(r"\{[^{}]*:starIcons\((\d*)\)\}", lambda m: f"{m.group(1) or 2} Stars", text)
    text = text.replace("{singleStarIcon}", "Star")
    for _ in range(4):                       # templates nest
        text = re.sub(r"\{\w+:plural:[^{}|]*\|([^{}]*)\}", lambda m: m.group(1), text)
        text = re.sub(r"\{IfUpgraded:show:[^{}|]*\|([^{}]*)\}", lambda m: m.group(1) or "X", text)
        text = re.sub(r"\{\w+:(?:show|cond|choose)[^{}]*\}|\{InCombat:[^{}]*\}", "", text)
        text = re.sub(r"\{\w+(?::[^{}]*)?\}", "3", text)
    return text


class Catalog:
    """Card facts for the simulator's ids, in the shape the live game's card facts have."""

    def __init__(self, rs, source):
        text = json.loads((Path(source) / CARD_TEXT).read_text(encoding="utf-8"))
        self.ids = dict(rs.list_registry_id_forms("card"))          # "StrikeSilent" -> "STRIKE_SILENT"
        self.relic_ids = dict(rs.list_registry_id_forms("relic_id"))
        self.potion_ids = dict(rs.list_registry_id_forms("potion"))
        self.cards = {}
        for cid, kind, rarity in rs.card_kinds():
            key = cid if f"{cid}.description" in text else cid.removesuffix("_CARD")
            self.cards[cid] = {"id": cid, "name": text.get(f"{key}.title", cid), "type": kind.capitalize(),
                               "rarity": rarity.capitalize(), "description": _render(text.get(f"{key}.description", ""))}

    def card_id(self, name) -> str:
        name = str(name).removeprefix("CARD.")       # a card reward names its offers "CARD.SLICE", the deck "Slice"
        return self.ids.get(name, name)

    def card(self, name) -> dict:
        cid = self.card_id(name)
        if cid not in self.cards and cid + "_CARD" in self.cards:    # a card named like its Power, as other sources spell it
            cid += "_CARD"
        return self.cards.get(cid) or {"id": cid, "name": cid, "type": None, "rarity": None, "description": ""}

    def deck(self, names) -> list:
        return [self.card(n) for n in names]

    def relic_id(self, name) -> str:
        name = str(name).removeprefix("RELIC.")
        return self.relic_ids.get(name, name)

    def potion_id(self, name) -> str:
        name = str(name).removeprefix("POTION.")
        return self.potion_ids.get(name, name)


def plan_of(arm, character) -> deck_balance.DeckPlan | None:
    """The deck plan an arm drafts by; None for the champion alone."""
    name, _, changes = arm.partition(":")
    if name == "net":
        return None
    if name not in ("shared", "character"):
        raise ValueError(f"unknown arm {arm!r}")
    plan = deck_balance.PLANS["IRONCLAD"] if name == "shared" else deck_balance.plan_for(character)
    tables = {key: dict(getattr(plan, key)) for key in ("targets", "caps", "scaling_min", "deck_targets")}
    fields = {}
    for change in filter(None, changes.split(";")):
        key, _, value = change.partition("=")
        if key in ("early_roles", "late_first", "build_first", "never_over", "tracks"):
            fields[key] = tuple(filter(None, value.split("+")))      # role lists are joined with "+": early_roles=block+damage
            continue
        value = json.loads(value) if value[:1] in "-0123456789ntf[" else value      # caps.damage=[5,7,9]: one per act
        table, _, entry = key.partition(".")
        if entry and table in tables:           # targets.frost_min=3, caps.damage=5, deck_targets.2=26
            if value is None:
                tables[table].pop(int(entry) if table == "deck_targets" else entry, None)
            else:
                tables[table][int(entry) if table == "deck_targets" else entry] = value
        else:
            fields[key] = value
    return replace(plan, **tables, **fields)


def choose_reward(state, effects, probs) -> int | None:
    """Which offered card a drafter obeying the plan takes: an index into ``effects``, or None to skip.

    ``probs`` are the champion's own probabilities for each offer, the skip last. They are flattened
    (``FLATTEN``) and the plan multiplies them (``WEIGHT``): a card for the phase's first priority
    counts four times, one that fills a missing or short role or track twice, one that worsens the
    balance never, and a card that fills nothing a quarter once the plan keeps the deck thin.
    """
    thin = state["phase"] == deck_balance.LATE
    liked = [max(float(p), 0.0) ** FLATTEN for p in probs]
    scores = []
    for row, p in zip(effects, liked):
        kind = ("worsens" if row["effect"] == "worsens" else "priority" if row["fills_priority"] else
                "improves" if row["effect"] == "improves" else "mixed" if row["effect"] == "mixed" else
                "neutral_thin" if thin else "neutral")
        scores.append(p * WEIGHT[kind])
    forced = state["phase"] == deck_balance.EARLY and any(r["fills_priority"] and r["effect"] != "worsens" for r in effects)
    best = max(range(len(scores)), key=scores.__getitem__, default=None)
    if best is None or scores[best] <= 0 or (not forced and scores[best] <= liked[-1]):
        return None
    return best


def _removal(rm, catalog, state) -> dict | None:
    """The shop removal a thin-deck plan buys first: a starter Strike, or a starter Defend when Block is the
    over-supplied side; None when the shop offers none or the deck holds neither."""
    offered = {a["deck_idx"]: a for a in rm.available_actions() if a.get("action") == "shop_remove_card"}
    if not offered:
        return None
    deck = [catalog.card_id(n) for n in rm.master_deck()]
    order = ("DEFEND_", "STRIKE_") if state["status"]["block"] == "over_supplied" else ("STRIKE_", "DEFEND_")
    for prefix in order:
        for idx, cid in enumerate(deck):
            if cid.startswith(prefix) and idx in offered:
                return offered[idx]
    return None


def _call(value):
    return value() if callable(value) else value


def play_run(rs, catalog, table, character, seed, arm, *, source, ascension=10, sims=SIMS, boss_sims=BOSS_SIMS,
             stop_after_bosses=None) -> dict:
    """One simulated run of ``character`` with ``arm`` deciding the card rewards; its outcome and its fights.

    ``stop_after_bosses`` ends the run once that many bosses are beaten (2 = at the door of act 3):
    most A10 runs end before it, and the long fights after it cost most of a batch's time.
    """
    name = engine.character_name(character)
    model, features = champion(source, character)
    rm = rs.RunManager(seed=seed, character=name, ascension_level=ascension)
    agent = rs.PpoRunAgent(seed=seed, sims=sims, boss_sims=boss_sims, model_path=model, features=features)
    plan = plan_of(arm, character)
    tiers = {e["id"]: e for e in table}
    fights, open_fight = [], None
    picked = skipped = removed = bought_removal = 0
    started = time.perf_counter()

    def balance():
        return deck_balance.balance(catalog.deck(rm.master_deck()), rm.act_index, character, plan=plan)

    def shape():        # the deck by the character's own plan, whatever arm drafted it: what the arms are compared on
        own = deck_balance.balance(catalog.deck(rm.master_deck()), rm.act_index, character)
        return {"added": {role: row["count"] for role, row in own["added"].items()},
                "tracks": {name: row["count"] for name, row in own["tracks"].items()}}

    def close_fight():
        nonlocal open_fight
        if open_fight is not None:
            open_fight["hp_after"] = rm.current_hp
            open_fight["won"] = not (rm.is_over() and not rm.player_won and rm.current_hp <= 0)
            fights.append(open_fight)
            open_fight = None

    for _ in range(MAX_STEPS):
        if rm.is_over():
            break
        phase = rm.phase()
        if phase == "combat":
            if open_fight is None:
                combat = rm.combat_state()
                encounter = _call(combat.encounter_id) if combat is not None else None
                entry = tiers.get(encounter) or {}
                open_fight = {
                    "floor": rm.total_floor(), "act": rm.act_index, "encounter": encounter, "tier": entry.get("tier"),
                    "hp": rm.current_hp, "max_hp": rm.max_hp, **shape(),
                    "deck": [catalog.card_id(n) + ("+1" if up else "") for n, up in rm.master_deck_detailed()],
                    "relics": [catalog.relic_id(r) for r in rm.relics()],
                    "potions": [catalog.potion_id(p) for p in rm.potions()]}
        else:
            close_fight()
            if stop_after_bosses and sum(f["tier"] == "Boss" and f["won"] for f in fights) >= stop_after_bosses:
                break
            if plan is not None and phase == "card_reward":
                scored = agent.evaluate_with_actions(rm)
                if scored is not None:
                    actions, probs = scored[0], scored[1]
                    offers = [(a, p) for a, p in zip(actions, probs) if a.get("action") == "pick_card_reward" and a.get("idx") is not None]
                    skip = next((p for a, p in zip(actions, probs) if a.get("idx") is None), 0.0)
                    options = rm.card_reward_option_ids()
                    if offers and len(options) > max(a["idx"] for a, _ in offers):
                        state = balance()
                        effects = deck_balance.offer_effects(state, [(a["idx"], catalog.card(options[a["idx"]])) for a, _ in offers])
                        choice = choose_reward(state, effects, [p for _, p in offers] + [skip])
                        action = offers[choice][0] if choice is not None else next(
                            (a for a in actions if a.get("idx") is None), None)
                        if action is not None:
                            rm.take_action(action)
                            picked += choice is not None
                            skipped += choice is None
                            continue
            removal_first = plan is not None and (plan.deck_target is not None or (
                plan.removals_from_act is not None and rm.act_index >= plan.removals_from_act))
            if removal_first and phase == "shop" and not bought_removal:
                action = _removal(rm, catalog, balance())
                bought_removal = 1           # one removal a shop; asked once whether it could be bought or not
                if action is not None:
                    rm.take_action(action)
                    removed += 1
                    continue
            if phase != "shop":
                bought_removal = 0
        record = agent.step(rm)
        if record is None:
            break
        action = record.get("action") or {}
        if action.get("action") == "pick_card_reward":
            picked += action.get("idx") is not None
            skipped += action.get("idx") is None
        elif action.get("action") == "shop_remove_card":
            removed += 1
    close_fight()
    return {"character": character, "arm": arm, "seed": seed, "ascension": ascension, "won": bool(rm.player_won),
            "floor": rm.total_floor(), "act": rm.act_index, "hp": rm.current_hp, "max_hp": rm.max_hp,
            "bosses_beaten": sum(f["tier"] == "Boss" and f["won"] for f in fights),
            "elites_beaten": sum(f["tier"] == "Elite" and f["won"] for f in fights),
            "deck_size": len(rm.master_deck()), "picked": picked, "skipped": skipped, "removed": removed,
            **shape(), "relics": len(rm.relics()), "fights": fights, "seconds": round(time.perf_counter() - started, 1)}


_worker: dict = {}


def _work(job):
    """One run in a worker process, which loads the simulator and the card catalog once."""
    game_dir, source, character, seed, arm, ascension, sims, boss_sims, stop_after_bosses = job
    if not _worker:
        rs = engine.load(game_dir)
        _worker.update(rs=rs, catalog=Catalog(rs, source), table=engine.encounters(rs))
    try:
        return play_run(_worker["rs"], _worker["catalog"], _worker["table"], character, seed, arm, source=source,
                        ascension=ascension, sims=sims, boss_sims=boss_sims, stop_after_bosses=stop_after_bosses)
    except Exception as exc:                # one unplayable run must not cost the batch
        return {"character": character, "arm": arm, "seed": seed, "error": f"{type(exc).__name__}: {exc}"[:300]}


def read_runs(path) -> list[dict]:
    """The runs a file of ``play`` holds; a run played twice keeps its last row."""
    rows = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rows[row["character"], row["arm"], row["seed"]] = row
    except FileNotFoundError:
        pass
    return list(rows.values())


def play(characters, arms, runs, out, *, game_dir="data/game", source=None, ascension=10, sims=SIMS, boss_sims=BOSS_SIMS,
         first_seed=1, workers=None, progress=None, stop_after_bosses=None) -> list[dict]:
    """Play ``runs`` seeds of every character with every arm, appending each finished run to ``out``.

    The same seeds go to every arm, so arms meet the same maps and the same first fights. Runs the
    file already holds are not played again, so a batch that was cut short resumes.
    """
    from multiprocessing import get_context
    source = str(source_dir(source))
    characters = [character_key(c) or c for c in characters]
    done = {(r["character"], r["arm"], r["seed"]) for r in read_runs(out) if "error" not in r}
    jobs = [(game_dir, source, c, seed, arm, ascension, sims, boss_sims, stop_after_bosses)
            for seed in range(first_seed, first_seed + runs) for c in characters for arm in arms
            if (c, arm, seed) not in done]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as fh, get_context("spawn").Pool(workers or engine_cores()) as pool:
        for i, row in enumerate(pool.imap_unordered(_work, jobs, chunksize=1), 1):
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if progress:
                progress(i, len(jobs), row)
    return read_runs(out)


def engine_cores() -> int:
    import os
    return os.cpu_count() or 1


def _mean_se(values) -> tuple:
    values = [float(v) for v in values]
    if not values:
        return None, None
    return (round(statistics.fmean(values), 3),
            round(statistics.stdev(values) / len(values) ** 0.5, 3) if len(values) > 1 else None)


def summarize(rows) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"runs": 0, "errors": len(rows)}
    floor, floor_se = _mean_se(r["floor"] for r in ok)
    return {"runs": len(ok), "errors": len(rows) - len(ok), "floor": floor, "floor_se": floor_se,
            "act1_boss_beaten": round(statistics.fmean(r["bosses_beaten"] >= 1 for r in ok), 3),
            "act2_boss_beaten": round(statistics.fmean(r["bosses_beaten"] >= 2 for r in ok), 3),
            "won": round(statistics.fmean(r["won"] for r in ok), 3),
            "elites_beaten": _mean_se(r["elites_beaten"] for r in ok)[0],
            "deck_size": _mean_se(r["deck_size"] for r in ok)[0], "picked": _mean_se(r["picked"] for r in ok)[0],
            "skipped": _mean_se(r["skipped"] for r in ok)[0], "removed": _mean_se(r["removed"] for r in ok)[0]}


def paired(base, other) -> dict:
    """``other`` minus ``base`` over the seeds both played, with the standard error of the mean difference."""
    theirs = {r["seed"]: r for r in other if "error" not in r}
    pairs = [(b, theirs[b["seed"]]) for b in base if "error" not in b and b["seed"] in theirs]
    out = {"runs": len(pairs)}
    if len(pairs) < 2:
        return out
    for key, value in (("floor", lambda r: r["floor"]), ("act1_boss_beaten", lambda r: r["bosses_beaten"] >= 1),
                       ("act2_boss_beaten", lambda r: r["bosses_beaten"] >= 2), ("won", lambda r: r["won"])):
        out[key], out[key + "_se"] = _mean_se(float(value(o)) - float(value(b)) for b, o in pairs)
    return out


def report(rows) -> dict:
    """Per character: every arm's summary, and every other arm against the first one played (paired by seed)."""
    by = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by[row["character"]][row["arm"]].append(row)
    out = {}
    for character, arms in by.items():
        names = list(arms)
        out[character] = {"arms": {name: summarize(arms[name]) for name in names},
                          "versus": {f"{name} - {base}": paired(arms[base], arms[name])
                                     for base in names for name in names if name != base}}
    return out


def fight_starts(rows, per_tier=None, arm=None) -> list[dict]:
    """Fight starts of the finished runs as ``sim.Scenario`` rows, spread over floors.

    ``per_tier`` caps how many are kept of each tier ({"Boss": 24, ...}; a tier left out keeps all).
    ``arm`` keeps one arm's runs only.
    """
    found = defaultdict(list)
    for row in rows:
        if "error" in row or (arm and row["arm"] != arm):
            continue
        for fight in row["fights"]:
            if not fight.get("encounter") or not fight.get("tier"):
                continue
            found[fight["tier"]].append({
                "name": f"sim_{row['character'].lower()}_{row['arm'].partition(':')[0]}_{row['seed']}_f{fight['floor']}",
                "encounter": fight["encounter"], "tier": fight["tier"], "act": fight["act"], "floor": fight["floor"],
                "deck": fight["deck"], "relics": fight["relics"], "potions": fight["potions"], "hp": fight["hp"],
                "max_hp": fight["max_hp"], "ascension": row["ascension"], "drop_chance_pct": None,
                "character": row["character"]})
    out = []
    for tier, items in found.items():
        items.sort(key=lambda s: (s["act"], s["floor"], s["name"]))
        keep = (per_tier or {}).get(tier)
        if keep and len(items) > keep:
            # the same number from each act the runs reached, spread over its floors
            acts = sorted({s["act"] for s in items})
            share, picked = max(1, keep // len(acts)), []
            for act in acts:
                pool = [s for s in items if s["act"] == act]
                step = max(1, len(pool) // share)
                picked += pool[::step][:share]
            items = picked
        out += items
    return sorted(out, key=lambda s: s["name"])
