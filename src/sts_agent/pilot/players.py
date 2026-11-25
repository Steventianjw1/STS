"""What the highest-rated players' decks hold and what they take, counted the way the deck plans count.

A character's deck plan (``evidence.deck_balance.plan_for``) says which roles and tracks come first and
when another card of a kind stops helping. This module measures those things on real runs: the
solo A10 standard runs that the highest-rated Spire Codex players of a character submitted on the
pinned game build, read with the same role rules the plan uses.

    fetch       the runs: the Elo board's best players of the character, their runs on the pinned builds
    decks       what the deck entering each act boss holds, the rewards taken and skipped, removals, elites
    size_rates  how often a reward was taken at each deck size: whether the players hold the deck to a size
    pick_rates  how often a card of a kind was taken when offered, by how many of that kind the deck held
    plan_fit    how a plan labels the offers (priority, improves, neutral, worsens) and how often the
                players took the cards under each label: a plan that reads decks the way these players
                build them has them take what it calls worsening rarely and what it puts first often

Card text comes from the simulator's catalog (``runs.Catalog``), which is the pinned build's, so runs
of other builds are left out. The runs are fetched through the agent's own Spire Codex client and
kept in its cache (``traces/spire_codex_cache``), so a second reading asks the network for nothing.

These are observations of very strong players, not instructions and not causes: what they take
depends on what they were offered, on their relics and on how well they play the deck.
"""
from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict

from ..config import ExpertStatsConfig
from ..evidence import deck_balance
from ..game import builds as game_builds
from ..evidence.expert_stats import SpireCodexClient, StatsUnavailable
from ..evidence.spire_history import normalize_run
from . import runs

PLAYERS, RUNS_EACH = 12, 30
PAUSE_S = 0.35                          # between requests: far below the public rate limit
KEEP_S = 365 * 86400.0                  # the fetched runs are a fixed sample, not a live statistic
LABELS = ("priority", "improves", "mixed", "neutral", "neutral_thin", "worsens")
MARKS = ("weak", "summon", "frost", "osty_dies")


def client(cache_dir=None) -> SpireCodexClient:
    config = ExpertStatsConfig(request_timeout_s=60.0, cache_ttl_s=KEEP_S)
    if cache_dir:
        config.cache_dir = str(cache_dir)
    return SpireCodexClient(config)


def _get(api, path, params, offline=False):
    """One answer of the API, from the cache when it is there; None when there is none to be had."""
    for attempt in range(4):
        try:
            got = api.get(path, params, deadline=api.clock() + (0.0 if offline else 90.0))
            if got.cache == "network":
                time.sleep(PAUSE_S)
            return got.data
        except StatsUnavailable as exc:
            if offline or str(exc) not in ("rate_limited", "request_failed", "http_429"):
                return None
            api.failures.clear()            # the client refuses a failed request for a minute; wait instead
            time.sleep(max(5.0 * (attempt + 1), api.backoff_until - api.wall_clock()))
    return None


def codex_builds(build=None) -> tuple:
    """The Spire Codex build ids whose runs describe a game build (the one in use when None): it and the
    builds with the same cards, the ones read first first."""
    return game_builds.get(build).codex_builds


def fetch(character, *, players=PLAYERS, runs_each=RUNS_EACH, builds=None, api=None, offline=False,
          progress=None) -> list[dict]:
    """The raw runs of the character's ``players`` highest-rated accounts that have any: up to
    ``runs_each`` solo A10 standard runs each, newest first, of ``builds`` in that order. An account
    is rated on the Elo board of the top 100 (30 rated runs or more with the character). Network
    unless cached."""
    api, builds = api or client(), builds or codex_builds()
    board = (_get(api, "/api/leaderboards/elo", {"limit": 100, "min_runs": 30}, offline) or {}).get("players") or []
    key = character.lower()
    ranked = sorted((p for p in board if (p.get("by_character") or {}).get(key, {}).get("runs", 0) >= 30),
                    key=lambda p: -p["by_character"][key]["elo"])
    out, found = [], 0
    for rank, player in enumerate(ranked, 1):
        if found >= players:            # the highest-rated ``players`` accounts that have such runs
            break
        kept = 0
        for build in builds:
            listing = _get(api, "/api/runs/list", {"username": player["username"], "character": character, "ascension": 10,
                                                   "build_id": build, "players": 1, "game_mode": "standard",
                                                   "sort": "newest", "page": 1, "limit": 50}, offline) or {}
            for row in listing.get("runs") or []:
                if kept >= runs_each:
                    break
                if str(row.get("username", "")).casefold() != player["username"].casefold():
                    continue
                raw = _get(api, "/api/runs/shared/" + str(row.get("run_hash")), {}, offline)
                if isinstance(raw, dict) and raw.get("players"):
                    out.append({**raw, "_player": rank})
                    kept += 1
        found += kept > 0
        if progress and kept:
            progress(found, players, kept)
    return out


def read(raw_runs, character) -> list[dict]:
    """The runs that can be read: not abandoned, A10, and with a deck that can be rebuilt floor by floor."""
    rows = []
    for raw in raw_runs:
        if raw.get("was_abandoned") or raw.get("ascension") != 10:
            continue
        snaps = {}
        try:
            normalize_run(raw, character=character, snapshots_out=snaps)
        except StatsUnavailable:
            continue
        floors, number, bosses = [], 0, {}
        for act, points in enumerate(raw["map_point_history"], 1):
            for point in points:
                number += 1
                rooms = {str(r.get("room_type", "")).lower() for r in point.get("rooms") or []}
                floors.append((number, act, rooms, point["player_stats"][0]))
                if point.get("map_point_type") == "boss":
                    bosses.setdefault(act, number)
        rows.append({"player": raw.get("_player"), "win": bool(raw.get("win")), "build": raw.get("build_id"),
                     "floors": floors, "bosses": bosses, "snaps": snaps, "final": raw["players"][0]["deck"]})
    return rows


def _mean(values):
    values = list(values)
    return round(statistics.fmean(values), 2) if values else None


def _quartiles(values):
    values = sorted(values)
    return [values[min(len(values) - 1, int(q * len(values)))] for q in (0.25, 0.5, 0.75)] if values else None


def _words(words):
    return {w: re.compile(rf"\b{re.escape(w)}", re.I) for w in words or ()}


def _shape(cards, act, character, catalog, words) -> dict:
    deck = [catalog.card(c["id"]) for c in cards]
    state = deck_balance.balance(deck, act, character)
    kinds = Counter(str(c.get("type") or "?").lower() for c in deck)
    ids = Counter(catalog.card_id(c["id"]) for c in cards)
    return {"size": state["deck_size"], "added": state["cards_added"],
            "strikes": sum(n for cid, n in ids.items() if cid.startswith("STRIKE_")),
            "defends": sum(n for cid, n in ids.items() if cid.startswith("DEFEND_")),
            "attacks": kinds["attack"], "skills": kinds["skill"], "powers": kinds["power"],
            **{role: row["count"] for role, row in state["added"].items()},
            **{f"track_{name}": row["count"] for name, row in state["tracks"].items()},
            **{f"names_{w}": sum(bool(rx.search(c.get("description") or "")) for c in deck) for w, rx in words.items()}}


def decks(rows, character, catalog, words=()) -> dict:
    """Per act: the deck the won runs carried into the boss, and what every run did during the act."""
    words, out = _words(words), {"runs": len(rows), "won": sum(r["win"] for r in rows),
                                  "players": len({r["player"] for r in rows})}
    for act in (1, 2, 3):
        there = [r for r in rows if act in r["bosses"] and r["snaps"].get(r["bosses"][act])]
        shapes = [_shape(r["snaps"][r["bosses"][act]]["cards"], act, character, catalog, words) for r in there if r["win"]]
        if len(shapes) < 8:
            continue
        taken = skipped = 0
        removed, removals, elites, elite_hp, boss_hp = Counter(), [], [], [], []
        for r in there:
            n_removed = n_elites = 0
            for number, at, rooms, stats in r["floors"]:
                if at != act:
                    continue
                for card in stats.get("cards_removed") or []:
                    cid = catalog.card_id(str(card.get("id", "")))
                    kind = str(catalog.card(cid).get("type") or "").lower()
                    removed["strike" if cid.startswith("STRIKE_") else "defend" if cid.startswith("DEFEND_") else
                            "curse" if kind in ("curse", "status") else "other"] += 1
                    n_removed += 1
                if "elite" in rooms:
                    n_elites += 1
                    if stats.get("max_hp"):
                        elite_hp.append(100.0 * (stats.get("damage_taken") or 0) / stats["max_hp"])
                choices = stats.get("card_choices") or []
                if choices and "shop" not in rooms:
                    picked = any(c.get("was_picked") for c in choices)
                    taken += picked
                    skipped += not picked
            removals.append(n_removed)
            elites.append(n_elites)
            before = r["floors"][r["bosses"][act] - 2][3] if r["bosses"][act] >= 2 else {}
            if before.get("max_hp"):
                boss_hp.append(100.0 * (before.get("current_hp") or 0) / before["max_hp"])
        total = sum(removed.values()) or 1
        out[f"act{act}"] = {
            "won_decks": len(shapes), "size_quartiles": _quartiles([s["size"] for s in shapes]),
            "deck": {key: _mean(s[key] for s in shapes) for key in shapes[0]},
            "runs": len(there), "rewards_taken_pct": round(100.0 * taken / max(1, taken + skipped)),
            "rewards_taken": round(taken / len(there), 2), "removals": _mean(removals),
            "removed_pct": {kind: round(100.0 * n / total) for kind, n in removed.most_common()},
            "elites": _mean(elites), "hp_lost_per_elite_pct": round(statistics.fmean(elite_hp)) if elite_hp else None,
            "hp_entering_boss_pct": round(statistics.fmean(boss_hp)) if boss_hp else None}
    return out


def rewards(rows, catalog):
    """Every card reward of the runs: (act, the deck before the floor, the cards offered, which was taken)."""
    for r in rows:
        for number, act, rooms, stats in r["floors"]:
            choices, snap = stats.get("card_choices") or [], r["snaps"].get(number)
            if not choices or "shop" in rooms or not snap or number == 1:
                continue
            offers = [(c["card"]["id"], bool(c.get("was_picked"))) for c in choices
                      if isinstance(c.get("card"), dict) and c["card"].get("id")]
            cards = [catalog.card(cid) for cid, _ in offers]
            if not 2 <= len(offers) <= 4 or sum(p for _, p in offers) > 1 or any(not c.get("type") for c in cards):
                continue
            yield act, [catalog.card(c["id"]) for c in snap["cards"]], cards, [p for _, p in offers]


def _kinds(card, words) -> set:
    """What a card is to the plans: its roles, the tracks it counts for, the ``words`` its text names."""
    roles, marks, text = deck_balance.card_roles(card), deck_balance.card_marks(card), card.get("description") or ""
    named = {f"names_{w}" for w, rx in words.items() if rx.search(text)}
    plain = roles == ("damage",) and not named and not any(marks.get(m) for m in MARKS)
    return ({*roles} or {"other"}) | {m for m in MARKS if marks.get(m)} | named | ({"damage_only"} if plain else set())


def pick_rates(decisions, words=()) -> dict:
    """kind -> {"held": {cards of the kind already added: [taken, offered]}, "act": {act: [taken, offered]}}.

    ``held`` counts the cards added beyond the starter, except for the tracks, which count the
    starter's too (as the plans do); 6 stands for six or more.
    """
    words, out = _words(words), defaultdict(lambda: {"held": defaultdict(lambda: [0, 0]), "act": defaultdict(lambda: [0, 0])})
    for act, deck, cards, picked in decisions:
        held = Counter()
        for card in deck:
            basic = str(card.get("rarity") or "").lower() == "basic"
            for kind in _kinds(card, words):
                if not basic or kind in MARKS:
                    held[kind] += 1
        for card, was in zip(cards, picked):
            for kind in _kinds(card, words):
                for cell in (out[kind]["held"][min(held[kind], 6)], out[kind]["act"][act]):
                    cell[0] += was
                    cell[1] += 1
    return {kind: {"held": dict(row["held"]), "act": dict(row["act"])} for kind, row in out.items()}


def size_rates(decisions, step=4) -> dict:
    """act -> {deck size at the reward, in steps of ``step``: [rewards with a card taken, rewards]}."""
    out = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for act, deck, _, picked in decisions:
        cell = out[act][len(deck) // step * step]
        cell[0] += any(picked)
        cell[1] += 1
    return {act: dict(sorted(row.items())) for act, row in sorted(out.items())}


def plan_fit(decisions, character, arm) -> dict:
    """How ``arm``'s plan (what ``runs.plan_of`` takes) labels the offers, and how often each label was taken."""
    plan = runs.plan_of(arm, character)
    cells, takes = {label: [0, 0] for label in LABELS}, 0
    for act, deck, cards, picked in decisions:
        state = deck_balance.balance(deck, act, character, plan=plan)
        thin = state["phase"] == deck_balance.LATE
        for row, was in zip(deck_balance.offer_effects(state, list(enumerate(cards))), picked):
            label = ("worsens" if row["effect"] == "worsens" else "priority" if row["fills_priority"] else
                     row["effect"] if row["effect"] in ("improves", "mixed") else "neutral_thin" if thin else "neutral")
            cells[label][0] += was
            cells[label][1] += 1
            takes += was
    return {"takes": takes, "labels": {label: {"offered": n, "taken_pct": round(100.0 * a / n, 1)}
                                       for label, (a, n) in cells.items() if n},
            "takes_labelled_worsens_pct": round(100.0 * cells["worsens"][0] / max(1, takes), 1),
            "takes_labelled_neutral_thin_pct": round(100.0 * cells["neutral_thin"][0] / max(1, takes), 1)}


def card_rates(decisions, least=15) -> list:
    """Per card offered at least ``least`` times: (taken %, offered, id, roles and tracks), most taken first."""
    cells, tags = defaultdict(lambda: [0, 0]), {}
    for _, _, cards, picked in decisions:
        for card, was in zip(cards, picked):
            cid = str(card["id"]).removesuffix("_CARD")
            cells[cid][0] += was
            cells[cid][1] += 1
            tags[cid] = "/".join(sorted(_kinds(card, {})))
    return sorted(((round(100.0 * a / n), n, cid, tags[cid]) for cid, (a, n) in cells.items() if n >= least), reverse=True)
