"""``python -m sts_agent.pilot``: simulator comparisons and tuning, of fights and of whole runs.

    scenarios [--save]        the fight starts found in Spire Mentor's logs and kept in the repository
    bench  [--arms a,b,...]   play them with each arm; the first arm is the baseline
    report FILE               the comparison table of a file `bench --out` wrote
    tune   --encounter ID     search the profile space for one encounter (or --tier / --all)
    show                      the profile each encounter would be played with
    engine                    where the simulator was loaded from, and a fingerprint to compare machines by
    runs   --out FILE         whole simulated runs per character and deck-plan arm (`pilot.runs`)
    runs-report FILE          the comparison table of a file `runs` wrote
    fight-starts FILE         fight starts of those runs, kept per character for bench and tune (--save)

bench, tune, scenarios and show take --character (IRONCLAD by default; the others fight from the
fight starts `fight-starts --save` kept).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from ..game.characters import CHARACTERS, character_key
from . import engine, runs, sim
from .profiles import TUNED_DIR, character_tuning, load_tuned, profile_for, save_character_tuning


def _lower_priority() -> bool:
    """Put this process below normal CPU priority (Windows); False where that is not possible."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return bool(kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000))   # BELOW_NORMAL_PRIORITY_CLASS
    except (AttributeError, OSError):
        return False


def _character(value) -> str:
    key = character_key(value)
    if key is None:
        raise argparse.ArgumentTypeError(f"{value!r} is not one of {', '.join(CHARACTERS)}")
    return key


def _runs_table(report) -> str:
    lines = []
    for character, block in report.items():
        lines.append(f"{character}:")
        for arm, row in block["arms"].items():
            if row.get("runs"):
                lines.append(f"  {arm:<44} runs {row['runs']:>3}  floor {row['floor']:>5.1f} ±{row['floor_se'] or 0:.1f}  "
                             f"act 1 boss {row['act1_boss_beaten']:.3f}  act 2 boss {row['act2_boss_beaten']:.3f}  won {row['won']:.3f}  "
                             f"deck {row['deck_size']:.1f}  picked {row['picked']:.1f}  removed {row['removed']:.1f}"
                             + (f"  errors {row['errors']}" if row["errors"] else ""))
        for name, row in block["versus"].items():
            if row.get("runs", 0) >= 2:
                lines.append(f"    {name:<50} floor {row['floor']:+.2f} ±{row['floor_se']:.2f}   act 1 boss "
                             f"{row['act1_boss_beaten']:+.3f} ±{row['act1_boss_beaten_se']:.3f}   act 2 boss "
                             f"{row['act2_boss_beaten']:+.3f} ±{row['act2_boss_beaten_se']:.3f}   ({row['runs']} paired seeds)")
    return "\n".join(lines)


def _pick(scenarios, args):
    keep = [s for s in scenarios
            if (not args.tier or s.tier in args.tier.split(","))
            and (not args.encounter or s.encounter in args.encounter.split(","))]
    if args.limit:
        step = max(1, len(keep) // args.limit)          # spread over the logs rather than the first few
        keep = keep[::step][:args.limit]
    return keep


def _table(report) -> str:
    lines = []
    for tier, rows in report["by_tier"].items():
        lines.append(f"{tier}:")
        for name, row in rows.items():
            if " - " in name:
                if row.get("fights", 0) >= 2:
                    lines.append(f"    {name:<28} value {row['value']:+.4f} ±{row['value_se']:.4f}   "
                                 f"hp {row['hp_end']:+.2f} ±{row['hp_end_se']:.2f}   wins {row['won']:+.3f} ±{row['won_se']:.3f}"
                                 f"   ({row['fights']} paired fights)")
            elif row.get("fights"):
                lines.append(f"  {name:<30} fights {row['fights']:>3}  win {row['win_rate']:.3f}  hp lost {row['hp_lost']:>6.2f}"
                             f"  potions {row['potions_used']:.2f}  value {row['value']:.4f}  {row['seconds_per_decision']:.2f}s/decision"
                             + (f"  errors {row['errors']}" if row["errors"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sts_agent.pilot", description=__doc__.splitlines()[0])
    ap.add_argument("--game-dir", default="data/game", help="pinned_game_dir holding the game copy with Spire Mentor")
    ap.add_argument("--logs", default=None, help="Spire Mentor's log folder (default: %%LOCALAPPDATA%%/SpireMentor/logs)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scenarios")
    s.add_argument("--save", action="store_true", help="keep them in the repository (snapshots.json), for machines without the logs")
    sh = sub.add_parser("show")
    sub.add_parser("engine")
    b = sub.add_parser("bench")
    b.add_argument("--arms", default="mentor,pilot")
    b.add_argument("--worlds", type=int, default=2)
    b.add_argument("--parallel", type=int, default=None, help="fights played at once (default: cores / search threads)")
    b.add_argument("--out", default=None, help="append every finished fight to this JSON-lines file")
    b.add_argument("--hp-scale", type=float, default=None,
                   help="enter every fight with this share of the fight start's HP (0.5: fights in doubt, for `calibrate`)")
    r = sub.add_parser("report")
    r.add_argument("file")
    r.add_argument("--arms", default=None, help="arms to show, baseline first (default: all, in file order)")
    t = sub.add_parser("tune")
    t.add_argument("--all", action="store_true", help="every encounter of the engine's table")
    t.add_argument("--fights", type=int, default=8, help="fights per candidate in the first round")
    t.add_argument("--rounds", type=int, default=3)
    t.add_argument("--snapshots", type=int, default=12, help="logged run snapshots used per encounter")
    t.add_argument("--tuned-dir", default=None, help="write the results to this folder instead of the pilot's tuned/")
    t.add_argument("--skip-tuned", action="store_true", help="leave out encounters already tuned (resume a batch, or share one with another machine)")
    t.add_argument("--reverse", action="store_true", help="take the encounters last first (a second machine meets the first in the middle)")
    t.add_argument("--whole-tier", action="store_true",
                   help="tune the character's knobs for each --tier as a whole, on its fight starts as they are, instead of per encounter")
    t.add_argument("--knobs", default=None, help="candidate names to compare, comma separated (default: all of sim.KNOBS)")
    c = sub.add_parser("calibrate")
    c.add_argument("file", help="a file `bench --out` wrote")
    c.add_argument("--arm", default="pilot", help="whose fights to read (the arm the character will fight with)")
    c.add_argument("--save", action="store_true", help="keep the fit as the character's win-chance reading")
    w = sub.add_parser("runs")
    w.add_argument("--characters", default=",".join(CHARACTERS), help="game IDs, comma separated")
    w.add_argument("--arms", default="net,shared,character", help="net, shared, character, or character:field=value;... (see pilot.runs)")
    w.add_argument("--runs", type=int, default=40, help="seeds per character; every arm plays the same ones")
    w.add_argument("--first-seed", type=int, default=1)
    w.add_argument("--ascension", type=int, default=10)
    w.add_argument("--sims", type=int, default=runs.SIMS, help="search simulations per decision of a fight")
    w.add_argument("--boss-sims", type=int, default=runs.BOSS_SIMS)
    w.add_argument("--stop-after-bosses", type=int, default=None, help="end a run once it has beaten this many bosses (2: at the door of act 3)")
    w.add_argument("--source", default=None, help="a full checkout of Spire Mentor's source (default: data/mentor_build)")
    w.add_argument("--workers", type=int, default=None, help="runs played at once (default: the cores)")
    w.add_argument("--out", required=True, help="append every finished run to this JSON-lines file; runs it holds are not replayed")
    wr = sub.add_parser("runs-report")
    wr.add_argument("file")
    wr.add_argument("--arms", default=None, help="arms to show, the one to compare against first (default: all)")
    f = sub.add_parser("fight-starts")
    f.add_argument("file", help="a file `runs` wrote")
    f.add_argument("--arm", default=None, help="take the fight starts of this arm's runs only")
    f.add_argument("--per-tier", default="Weak=16,Normal=32,Elite=32,Boss=24", help="how many to keep of each tier, per character")
    f.add_argument("--save", action="store_true", help="keep them in the repository (snapshots_<character>.json)")
    for p in (s, sh, b, t, f, c):
        p.add_argument("--character", type=_character, default=None if p is f else "IRONCLAD",
                       help="whose fight starts" + (" (default: every character in the file)" if p is f else " (default IRONCLAD)"))
    for p in (b, t):
        p.add_argument("--low-priority", action="store_true", help="run below normal CPU priority, so a game being played keeps the processor")
        p.add_argument("--tier", default=None, help="Weak,Normal,Elite,Boss")
        p.add_argument("--encounter", default=None, help="encounter ids, comma separated")
        p.add_argument("--limit", type=int, default=0, help="at most this many scenarios")
    args = ap.parse_args(argv)

    if args.cmd == "report":
        fights = sim.read_fights(args.file)
        names = args.arms.split(",") if args.arms else list(fights)
        print(_table(sim.report({name: fights[name] for name in names})))
        return 0
    if args.cmd == "calibrate":
        fights = [r for r in sim.read_fights(args.file).get(args.arm, [])
                  if "error" not in r and r.get("character", "IRONCLAD") == args.character]
        fitted = {}
        for group, rows in (("Boss", [r for r in fights if r["tier"] == "Boss"]), ("other", [r for r in fights if r["tier"] != "Boss"])):
            fit = sim.fit_win_odds(rows)
            won = sum(r["won"] for r in rows)
            print(f"{args.character} {group}: {len(rows)} fights, {won} won: "
                  + (f"intercept {fit[0]}, slope {fit[1]}" if fit else "no fit (it needs at least 3 won and 3 lost fights)"))
            if fit:
                fitted[group] = fit
        if args.save and fitted:
            save_character_tuning(args.character, win_odds=fitted, evidence={"win_odds": {
                "arm": args.arm, "fights": len(fights), "kind": "simulator; every decision's share of won playouts against "
                "the fight's outcome, each fight weighing the same"}})
            print(f"  kept for {args.character}")
        return 0
    if args.cmd == "runs-report":
        rows = runs.read_runs(args.file)
        if args.arms:
            order = args.arms.split(",")
            rows = sorted((r for r in rows if r["arm"] in order), key=lambda r: order.index(r["arm"]))
        print(_runs_table(runs.report(rows)))
        return 0
    if args.cmd == "runs":
        started = time.time()
        characters = [_character(c) for c in args.characters.split(",")]
        print(f"runs: {args.runs} seeds x {len(characters)} characters x arms {args.arms}, ascension {args.ascension}, "
              f"fights searched with {args.sims} simulations ({args.boss_sims} at bosses) (simulator)", flush=True)
        rows = runs.play(characters, args.arms.split(","), args.runs, args.out, game_dir=args.game_dir, source=args.source,
                         ascension=args.ascension, sims=args.sims, boss_sims=args.boss_sims, first_seed=args.first_seed,
                         workers=args.workers, stop_after_bosses=args.stop_after_bosses, progress=lambda i, n, r: print(
                             f"  {i}/{n} {r['character']} {r['arm']} seed {r['seed']}: " + (r.get("error") or
                             f"floor {r['floor']}{' WON' if r['won'] else ''} in {r['seconds']:.0f}s"), flush=True)
                         if sys.stdout.isatty() or i % 20 == 0 or i == n else None)
        print(_runs_table(runs.report(rows)))
        print(f"done in {time.time() - started:.0f}s")
        return 0
    rs = engine.load(args.game_dir)
    if args.cmd == "fight-starts":
        caps = {k: int(v) for k, v in (kv.split("=") for kv in args.per_tier.split(",") if kv)}
        rows = runs.read_runs(args.file)
        for character in ([args.character] if args.character else sorted({r["character"] for r in rows})):
            found = runs.fight_starts([r for r in rows if r["character"] == character], caps, args.arm)
            usable = []
            for row in found:
                scenario = sim.Scenario(**{**row, "deck": tuple(row["deck"]), "relics": tuple(row["relics"]), "potions": tuple(row["potions"])})
                try:
                    scenario.build(rs, 1)
                    usable.append(scenario)
                except Exception:
                    continue
            print(f"{character}: {len(usable)} fight starts usable of {len(found)} picked: "
                  f"{dict(sorted(Counter((s.tier, s.act) for s in usable).items()))}")
            if args.save and usable and character == "IRONCLAD":
                print("  not kept: Ironclad's kept fight starts are the logged ones (scenarios --save)")
            elif args.save and usable:
                sim.save_snapshots(usable, sim.snapshot_path(character))
                print(f"  kept in {sim.snapshot_path(character)}")
        return 0
    if args.cmd == "engine":
        print(f"simulator: {Path(rs.__file__).parent}")
        print(f"encounters: {len(rs.list_encounters())}")
        print(f"fingerprint: {engine.fingerprint(rs)}")
        return 0
    character = getattr(args, "character", None) or "IRONCLAD"
    scenarios, skipped = sim.all_scenarios(rs, args.logs, character)
    if args.cmd == "scenarios":
        print(f"{len(scenarios)} fight starts usable; skipped: {dict(skipped)}")
        if args.save:
            sim.save_snapshots(scenarios)
            print(f"kept in {sim.SNAPSHOT_PATH}")
        for (tier, enc), n in sorted(Counter((s.tier, s.encounter) for s in scenarios).items()):
            print(f"  {tier:<7} {enc:<34} {n}")
        return 0
    if args.cmd == "show":
        tuned, whole = load_tuned(character=character), character_tuning(character)
        if whole.knobs or whole.win_odds:
            print(f"{character}: tier knobs {whole.knobs or 'none'}; win chance fit {whole.win_odds or 'Ironclad\'s'}")
        for e in engine.encounters(rs):
            p = profile_for(e["id"], e["tier"], e["act"], tuned, character)
            print(f"act {e['act']} {e['tier']:<7} {e['id']:<34} {'tuned  ' if e['id'] in tuned else 'default'} "
                  f"sims {p.sims} x{p.threads} pick {p.pick}"
                  + (f" hp carry {p.win_hp_carry}" if p.win_hp_carry is not None else "")
                  + (f" potion carry {p.win_potion_carry}" if p.win_potion_carry is not None else ""))
        return 0
    started = time.time()
    if args.low_priority:
        print(f"below-normal priority: {'on' if _lower_priority() else 'could not be set'}", flush=True)
    if args.cmd == "bench":
        chosen = _pick(scenarios, args)
        if not chosen:
            print("no scenario matches", file=sys.stderr)
            return 2
        if args.hp_scale:
            chosen = [replace(s, hp=max(1, round(s.hp * args.hp_scale)), name=f"{s.name}@hp{round(args.hp_scale * 100)}")
                      for s in chosen]
        print(f"bench: {len(chosen)} scenarios x {args.worlds} worlds, arms {args.arms} (simulator)", flush=True)
        out = None
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            out = open(args.out, "a", encoding="utf-8")
        try:
            report = sim.bench(rs, chosen, args.arms.split(","), worlds=args.worlds, parallel=args.parallel,
                               sink=(lambda row: (out.write(json.dumps(row) + "\n"), out.flush())) if out else None,
                               progress=lambda arm, i, n, r: print(
                                   f"  {arm} {i}/{n} {r['encounter']} " + (r.get("error") or
                                   f"{'won' if r['won'] else 'LOST'} hp {r['hp_start']}->{r['hp_end']} in {r['seconds']:.0f}s"), flush=True))
        finally:
            if out:
                out.close()
        print(_table(report))
    else:
        table = engine.encounters(rs)
        knobs = args.knobs.split(",") if args.knobs else None
        if args.whole_tier:
            if not args.tier:
                print("--whole-tier needs --tier", file=sys.stderr)
                return 2
            for tier in args.tier.split(","):
                pool = [s for s in scenarios if s.tier == tier]
                if not pool:
                    print(f"{character} {tier}: no fight start; skipped", flush=True)
                    continue
                step = max(1, len(pool) // args.snapshots)
                pool = pool[::step][:args.snapshots]
                print(f"tune {character} {tier} as a whole: {len(pool)} fight starts", flush=True)
                result = sim.tune(rs, {"id": None, "tier": tier, "act": None}, pool, fights=args.fights, rounds=args.rounds,
                                  folder=args.tuned_dir, knobs=knobs, character=character,
                                  progress=lambda rnd, name, i, n, _row: i == n and print(
                                      f"  {time.strftime('%H:%M:%S')} round {rnd + 1} {name}: {n} fights played", flush=True))
                for h in result["history"]:
                    print(f"  round {h['round']} ({h['fights']} fights): " + ", ".join(
                        f"{r['name']} {r.get('value')}" for r in h["ranked"]), flush=True)
                print(f"  chosen: {result['chosen']}  versus default: {result['versus_default']}  "
                      f"versus Mentor: {result['versus_mentor']}", flush=True)
            print(f"done in {time.time() - started:.0f}s")
            return 0
        wanted = [e for e in table
                  if (args.all or (args.encounter and e["id"] in args.encounter.split(","))
                      or (args.tier and e["tier"] in args.tier.split(",") and not args.encounter))]
        if not wanted:
            print("name encounters with --encounter, --tier or --all", file=sys.stderr)
            return 2
        tuned_dir = args.tuned_dir or TUNED_DIR
        live = sys.stdout.isatty()
        if args.reverse:
            wanted.reverse()

        def tick(rnd, name, i, n, _row):        # a terminal shows every fight; a log file one line per candidate
            if live:
                print(f"  round {rnd + 1} {name} {i}/{n}", end="\r", flush=True)
            elif i == n:
                print(f"  {time.strftime('%H:%M:%S')} round {rnd + 1} {name}: {n} fights played", flush=True)
        for e in wanted:
            if args.skip_tuned and e["id"] in load_tuned(tuned_dir, character):      # read each time: results arrive as batches run
                print(f"{e['id']}: already tuned; skipped", flush=True)
                continue
            # Decks as the runs really had them near this fight: a boss meets the act's last decks,
            # an elite the upper half, a hallway fight any deck of the act.
            pool = sorted((s for s in scenarios if s.act == e["act"]), key=lambda s: s.floor or 0)
            if e["tier"] == "Boss":
                pool = pool[-args.snapshots:]
            elif e["tier"] == "Elite":
                pool = pool[len(pool) // 2:]
            step = max(1, len(pool) // args.snapshots)
            pool = pool[::step][:args.snapshots]
            if not pool:
                print(f"{e['id']}: no logged run snapshot from act {e['act']}; skipped", flush=True)
                continue
            print(f"tune {e['id']} ({e['tier']}): {len(pool)} snapshots", flush=True)
            try:
                result = sim.tune(rs, e, pool, fights=args.fights, rounds=args.rounds, folder=args.tuned_dir, progress=tick,
                                  knobs=knobs, character=character)
            except ValueError as exc:
                print(f"  skipped: {exc}", flush=True)
                continue
            for h in result["history"]:
                print(f"  round {h['round']} ({h['fights']} fights): " + ", ".join(
                    f"{r['name']} {r.get('value')}" for r in h["ranked"]), flush=True)
            print(f"  chosen: {result['chosen']}  versus default: {result['versus_default']}  "
                  f"versus Mentor: {result['versus_mentor']}", flush=True)
    print(f"done in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
