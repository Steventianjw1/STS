"""``python -m sts_agent.pilot``: simulator comparisons and per-encounter tuning.

    scenarios [--save]        the fight starts found in Spire Mentor's logs and kept in the repository
    bench  [--arms a,b,...]   play them with each arm; the first arm is the baseline
    report FILE               the comparison table of a file `bench --out` wrote
    tune   --encounter ID     search the profile space for one encounter (or --tier / --all)
    show                      the profile each encounter would be played with
    engine                    where the simulator was loaded from, and a fingerprint to compare machines by
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from . import engine, sim
from .profiles import TUNED_DIR, load_tuned, profile_for


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
    sub.add_parser("show")
    sub.add_parser("engine")
    b = sub.add_parser("bench")
    b.add_argument("--arms", default="mentor,pilot")
    b.add_argument("--worlds", type=int, default=2)
    b.add_argument("--parallel", type=int, default=None, help="fights played at once (default: cores / search threads)")
    b.add_argument("--out", default=None, help="append every finished fight to this JSON-lines file")
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
    rs = engine.load(args.game_dir)
    if args.cmd == "engine":
        print(f"simulator: {Path(rs.__file__).parent}")
        print(f"encounters: {len(rs.list_encounters())}")
        print(f"fingerprint: {engine.fingerprint(rs)}")
        return 0
    scenarios, skipped = sim.all_scenarios(rs, args.logs)
    if args.cmd == "scenarios":
        print(f"{len(scenarios)} fight starts usable; skipped: {dict(skipped)}")
        if args.save:
            sim.save_snapshots(scenarios)
            print(f"kept in {sim.SNAPSHOT_PATH}")
        for (tier, enc), n in sorted(Counter((s.tier, s.encounter) for s in scenarios).items()):
            print(f"  {tier:<7} {enc:<34} {n}")
        return 0
    if args.cmd == "show":
        tuned = load_tuned()
        for e in engine.encounters(rs):
            p = profile_for(e["id"], e["tier"], e["act"], tuned)
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
            if args.skip_tuned and e["id"] in load_tuned(tuned_dir):      # read each time: results arrive as batches run
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
                result = sim.tune(rs, e, pool, fights=args.fights, rounds=args.rounds, folder=args.tuned_dir, progress=tick)
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
