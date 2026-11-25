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
    players --character C     the decks and card picks of the highest-rated Spire Codex players, read by the
                              deck plans' own rules, and how a plan's verdicts match them (`pilot.players`; network)

bench, tune, scenarios and show take --character (IRONCLAD by default; the others fight from the
fight starts `fight-starts --save` kept).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from ..game.characters import CHARACTERS, character_key
from . import engine, players, runs, sim
from .search import SAFE_WIN
from ..game import builds
from .profiles import WIN_ODDS, character_tuning, load_tuned, profile_for, save_character_tuning, tuned_root


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


def _players_table(character, rows, catalog, words, arms, cards=False, read_builds=()) -> str:
    found = players.decks(rows, character, catalog, words)
    lines = [f"{character}: {found['runs']} solo A10 standard runs of builds {', '.join(read_builds)} by the "
             f"{found['players']} highest-rated Spire Codex players who have them, {found['won']} won (public data, not this agent)"]
    for act in (1, 2, 3):
        row = found.get(f"act{act}")
        if not row:
            continue
        deck = row["deck"]
        lines.append(f"  act {act}: deck entering the boss in won runs (n={row['won_decks']}) {deck['size']:.1f} cards, "
                     f"quartiles {'/'.join(map(str, row['size_quartiles']))}: "
                     + ", ".join(f"{key.replace('track_', '').replace('names_', 'naming ')} {value:.1f}"
                                 for key, value in deck.items() if key != "size"))
        lines.append(f"         during the act (n={row['runs']}): card rewards taken {row['rewards_taken_pct']}% "
                     f"({row['rewards_taken']:.1f} a run), removals {row['removals']:.2f} ("
                     + ", ".join(f"{kind} {pct}%" for kind, pct in row["removed_pct"].items())
                     + f"), elites {row['elites']:.2f} at {row['hp_lost_per_elite_pct']}% of max HP each, "
                     f"HP entering the boss {row['hp_entering_boss_pct']}%")
    decisions = list(players.rewards(rows, catalog))
    lines.append(f"  pick rate when offered, at {len(decisions)} card rewards, by the number of that kind the deck "
                 "already held (taken % (offers), a dash under 25 offers), then by act:")
    for kind, row in sorted(players.pick_rates(decisions, words).items()):
        held = "  ".join(f"{n if n < 6 else '6+'}: " + (f"{100 * row['held'][n][0] / row['held'][n][1]:3.0f}% ({row['held'][n][1]})"
                                                     if row["held"].get(n, (0, 0))[1] >= 25 else "  -")
                         for n in range(7))
        acts = "  ".join(f"act {a}: {100 * row['act'][a][0] / row['act'][a][1]:.0f}%" for a in (1, 2, 3)
                         if row["act"].get(a, (0, 0))[1] >= 25)
        lines.append(f"    {kind:<16} {held}   | {acts}")
    lines.append("  a card reward was taken (not skipped), by act and the deck's size at the reward:")
    for act, row in players.size_rates(decisions).items():
        lines.append(f"    act {act}: " + "  ".join(f"{size}-{size + 3} cards {100 * a / n:.0f}% ({n})"
                                                   for size, (a, n) in row.items() if n >= 25))
    lines.append("  how each plan labels those offers, and how often the players took a card under each label:")
    for arm in arms:
        fit = players.plan_fit(decisions, character, arm)
        lines.append(f"    {arm[:60]:<60} " + "  ".join(f"{label} {row['taken_pct']:.0f}% ({row['offered']})"
                                                         for label, row in fit["labels"].items())
                     + f"  | of the cards taken, {fit['takes_labelled_worsens_pct']:.0f}% are labelled worsens")
    if cards:
        lines.append("  every card offered 15 times or more: taken %, offers, card, kinds")
        lines += [f"    {pct:3d}% {n:4d}  {cid:<24} {tags}" for pct, n, cid, tags in players.card_rates(decisions)]
    return "\n".join(lines)


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
    ap.add_argument("--build", default=None, help=f"the game build to work on: {', '.join(builds.BUILDS)} "
                                                   f"(default: {builds.ENV} or {builds.DEFAULT})")
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
    c.add_argument("file", nargs="+", help="files `bench --out` wrote")
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
    pl = sub.add_parser("players")
    pl.add_argument("--players", type=int, default=players.PLAYERS, help="how many of the character's highest-rated accounts")
    pl.add_argument("--runs", type=int, default=players.RUNS_EACH, help="solo A10 runs of the game build read per account, newest first")
    pl.add_argument("--arms", default="shared,character", help="plans whose verdicts are compared with the picks (as for `runs`)")
    pl.add_argument("--words", default="", help="words a card's text names, counted as kinds of their own (Shiv,Poison)")
    pl.add_argument("--cards", action="store_true", help="also list every card's pick rate")
    pl.add_argument("--offline", action="store_true", help="read only what earlier calls kept in the Spire Codex cache")
    pl.add_argument("--builds", default=None, help="game builds whose runs are read, newest first, comma separated "
                                                   "(default: the Spire Codex builds of the game build in use)")
    pl.add_argument("--fetch-only", action="store_true", help="fetch and keep the runs, print only how many (needs no simulator)")
    pl.add_argument("--source", default=None, help="a full checkout of Spire Mentor's source (default: data/mentor_build)")
    for p in (s, sh, b, t, f, c, pl):
        p.add_argument("--character", type=_character, default=None if p is f else "IRONCLAD",
                       help=("whose players" if p is pl else "whose fight starts")
                       + (" (default: every character in the file)" if p is f else " (default IRONCLAD)"))
    for p in (b, t):
        p.add_argument("--low-priority", action="store_true", help="run below normal CPU priority, so a game being played keeps the processor")
        p.add_argument("--tier", default=None, help="Weak,Normal,Elite,Boss")
        p.add_argument("--encounter", default=None, help="encounter ids, comma separated")
        p.add_argument("--limit", type=int, default=0, help="at most this many scenarios")
    args = ap.parse_args(argv)
    if args.build and builds.select(args.build) is None:
        ap.error(f"no version of the agent for game build {args.build!r}; it has {', '.join(builds.BUILDS)}")

    if args.cmd == "report":
        fights = sim.read_fights(args.file)
        names = args.arms.split(",") if args.arms else list(fights)
        print(_table(sim.report({name: fights[name] for name in names})))
        return 0
    if args.cmd == "calibrate":
        fights = [r for path in args.file for r in sim.read_fights(path).get(args.arm, [])
                  if "error" not in r and r.get("character", "IRONCLAD") == args.character]
        fitted, lines = {}, {}
        for group, rows in (("Boss", [r for r in fights if r["tier"] == "Boss"]), ("other", [r for r in fights if r["tier"] != "Boss"])):
            fit = sim.fit_win_odds(rows)
            won = sum(r["won"] for r in rows)
            print(f"{args.character} {group}: {len(rows)} fights, {won} won: "
                  + (f"intercept {fit[0]}, slope {fit[1]}" if fit else "no fit (it needs at least 3 won and 3 lost fights)"))
            if fit:         # how the fit reads the shares it was made from, against what happened in those fights
                bands = ((0.0, 0.02), (0.02, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 1.01))
                for lo, hi in bands:
                    inside = [(sum(lo <= p < hi for p in r["shares"]) / len(r["shares"]), r["won"]) for r in rows if r.get("shares")]
                    weight = sum(w for w, _ in inside)
                    if weight > 0:
                        mid = (lo + min(hi, 1.0)) / 2
                        odds = fit[0] + fit[1] * math.log(max(mid, 1e-3) / max(1 - mid, 1e-3))
                        print(f"    share {lo:.2f}-{min(hi, 1.0):.2f}: {weight:5.1f} fights' worth of decisions, "
                              f"{sum(w for w, won in inside if won) / weight:.0%} of them in won fights; "
                              f"the fit reads {mid:.2f} as {1 / (1 + math.exp(-odds)):.0%}")
            if fit:
                # Ironclad's reading stays unless these fights show its potion hold line to be unsafe for this
                # character: decisions read as safe that sat in lost fights, well beyond the 0.3% the line allows.
                # Then the character's own fit is kept, and only if it holds potions less readily. A fit is never
                # used to hold them more readily: a hundred fights do not place that line.
                shared = sim.hold_line(WIN_ODDS.get("Boss" if group == "Boss" else None))
                line = sim.hold_line(fit)
                worth, lost_worth = sim.above_line(rows, shared)
                allowed = (1 - SAFE_WIN) * worth
                unsafe = lost_worth > max(1.0, 3 * allowed)
                keep = unsafe and line > shared
                print(f"    this fit would hold potions above a share of {line:.3f}; Ironclad's reading holds them above {shared:.3f}")
                print(f"    decisions at or above Ironclad's line: {worth:.1f} fights' worth, {lost_worth:.2f} of it in lost fights "
                      f"(the line allows {allowed:.2f}): " + ("unsafe here, " + ("the fit is kept" if keep else "but the fit is no "
                      "more cautious, so nothing is kept") if unsafe else "safe here, Ironclad's reading stays"))
                lines[group] = {"fit": fit, "hold_line": round(line, 3), "ironclad_line": round(shared, 3), "kept": keep,
                                "fights": len(rows), "lost": len(rows) - won,
                                "above_ironclad_line": {"fights_worth": round(worth, 1), "in_lost_fights": round(lost_worth, 2),
                                                        "allowed": round(allowed, 2)}}
                if keep:
                    fitted[group] = fit
        # The same files usually hold Mentor's search on the same fights: how the arm compares, kept as evidence.
        mentor = [r for path in args.file for r in sim.read_fights(path).get("mentor", [])
                  if "error" not in r and r.get("character", "IRONCLAD") == args.character]
        versus = {}
        for tier in ("Weak", "Normal", "Elite", "Boss"):
            row = sim.paired([r for r in mentor if r["tier"] == tier], [r for r in fights if r["tier"] == tier])
            if row.get("fights", 0) >= 2:
                versus[tier] = {**row, "arm": sim.summarize([r for r in fights if r["tier"] == tier and "@hp" not in r["scenario"]]),
                                "mentor": sim.summarize([r for r in mentor if r["tier"] == tier])}
                print(f"{args.character} {tier}: {args.arm} - mentor over {row['fights']} paired fights: value {row['value']:+.4f} "
                      f"±{row['value_se']:.4f}, hp {row['hp_end']:+.2f} ±{row['hp_end_se']:.2f}, wins {row['won']:+.3f} ±{row['won_se']:.3f}")
        if args.save and (lines or versus):
            save_character_tuning(args.character, win_odds=fitted, evidence={
                **({"win_odds": {"arm": args.arm, "by_group": lines,
                                 "kind": "simulator; every decision's share of won playouts against the fight's outcome, "
                                         "each fight weighing the same, fight starts entered at full and at half HP; a fit "
                                         "is kept only when these fights show Ironclad's potion hold line to be unsafe for "
                                         "the character and the fit is the more cautious"}}
                   if lines else {}),
                **({"versus_mentor": {"kind": f"simulator; the {args.arm} arm minus Mentor's search on the same fights",
                                      "by_tier": versus}} if versus else {})})
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
    if args.cmd == "players":
        read_builds = tuple(b for b in (args.builds or "").split(",") if b) or players.codex_builds()
        raw = players.fetch(args.character, players=args.players, runs_each=args.runs, builds=read_builds, offline=args.offline,
                            progress=lambda i, n, kept: print(f"  player {i}/{n}: {kept} runs", flush=True))
        rows = players.read(raw, args.character)
        if args.fetch_only:
            print(f"{args.character}: {len(raw)} runs of builds {', '.join(read_builds)} kept, {len(rows)} readable, "
                  f"{sum(r['win'] for r in rows)} won")
            return 0
        rs = engine.load(args.game_dir)
        catalog, words = runs.Catalog(rs, runs.source_dir(args.source)), [w for w in args.words.split(",") if w]
        print(_players_table(args.character, rows, catalog, words, args.arms.split(","), args.cards, read_builds))
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
            if args.save and usable and character == "IRONCLAD" and builds.active().key == sim.LOGGED_BUILD:
                print("  not kept: on this build Ironclad's kept fight starts are the logged ones (scenarios --save)")
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
            sim.save_snapshots(scenarios, sim.snapshot_path(character))
            print(f"kept in {sim.snapshot_path(character)}")
        for (tier, enc), n in sorted(Counter((s.tier, s.encounter) for s in scenarios).items()):
            print(f"  {tier:<7} {enc:<34} {n}")
        return 0
    if args.cmd == "show":
        tuned, whole = load_tuned(character=character), character_tuning(character)
        if character != "IRONCLAD":
            knobs = {tier: row for tier, row in whole.knobs.items() if row}
            print(f"{character}: tier knobs {knobs or 'the defaults'}; win chance read by "
                  f"{whole.win_odds or 'Ironclad\'s fit'}; {len(tuned)} encounters tuned")
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
        tuned_dir = args.tuned_dir or tuned_root()
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
