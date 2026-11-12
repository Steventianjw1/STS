"""Bounded public-run ingestion and SQLite decision context, never game actions."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse

from .expert_stats import StatsUnavailable, _character, _count, _id, _top_contributors, _withhold

NORMALIZER_VERSION = 4  # 2: per-floor route rows; 3: ancient offers; 4: floor-scoped identity issues


def _act_id(value):
    if isinstance(value, str) and value.strip().upper().startswith("ACT."):
        value = value.strip()[4:]
    return _id(value)


def _num(value):
    return value if type(value) in (int, float) and value >= 0 else None


def _ancient_relic(offer):
    """Relic id of one ``ancient_choice`` entry (``TextKey``, else the ``title.key`` after its namespace)."""
    if not isinstance(offer, dict):
        return None
    rid = offer.get("TextKey")
    if not isinstance(rid, str) or not rid:
        key = (offer.get("title") or {}).get("key") if isinstance(offer.get("title"), dict) else None
        rid = key.split(".", 1)[1] if isinstance(key, str) and "." in key else key
    if isinstance(rid, str) and "." in rid:
        rid = rid.split(".", 1)[0]
    rid = _id(rid)
    return None if rid is None or rid.startswith("NONE") else rid


def normalize_route(raw: dict) -> tuple[dict, list[dict], list[str]]:
    """Per-floor route facts for one solo run; unknown values stay None.

    ``hp``/``gold`` are the recorded values after the floor; consumers derive
    arrival values from the previous floor. ``act_floor`` is the 1-based
    position within that act's recorded history.
    """
    acts, issues, floors = raw.get("acts"), set(), []
    acts = acts if isinstance(acts, list) else []
    if len(acts) < len(raw.get("map_point_history") or []):
        issues.add("act_ids_incomplete")
    floor = 0
    for act_no, act in enumerate(raw.get("map_point_history") or [], 1):
        act_id = _act_id(acts[act_no - 1]) if act_no <= len(acts) else None
        for act_floor, point in enumerate(act if isinstance(act, list) else [], 1):
            floor += 1
            point = point if isinstance(point, dict) else {}
            rooms = point.get("rooms") if isinstance(point.get("rooms"), list) else []
            room = rooms[0] if rooms and isinstance(rooms[0], dict) else {}
            seats = point.get("player_stats") if isinstance(point.get("player_stats"), list) else []
            stats = seats[0] if len(seats) == 1 and isinstance(seats[0], dict) else {}
            model = room.get("model_id")
            model = model[10:] if isinstance(model, str) and model.startswith("ENCOUNTER.") else model
            choices = stats.get("rest_site_choices")
            choices = [c.upper() for c in choices if isinstance(c, str)] if isinstance(choices, list) else []
            max_hp = _num(stats.get("max_hp"))
            point_type, room_type = point.get("map_point_type"), room.get("room_type")
            offers = stats.get("ancient_choice") if isinstance(stats.get("ancient_choice"), list) else []
            ancient = [(_ancient_relic(o), o.get("was_chosen") is True) for o in offers]
            if any(r is None for r, _ in ancient):
                issues.add("unmatched_ancient_offer")
            ancient = [(r, c) for r, c in ancient if r]
            floors.append({
                "act": act_no, "act_id": act_id, "act_floor": act_floor, "floor": floor,
                "map_point_type": point_type.lower() if isinstance(point_type, str) and point_type else None,
                "room_type": room_type.lower() if isinstance(room_type, str) and room_type else None,
                "encounter_id": _id(model), "hp": _num(stats.get("current_hp")),
                "max_hp": max_hp if max_hp else None, "damage_taken": _num(stats.get("damage_taken")),
                "gold": _num(stats.get("current_gold")), "rest_choices": choices,
                "ancient_id": _id(model) if ancient else None,  # e.g. EVENT.NEOW -> NEOW
                "ancient_offers": ancient})
            if not stats:
                issues.add("missing_floor_player_stats")
    win = raw.get("win")
    run = {"win": int(win) if type(win) is bool else None,
           "abandoned": int(raw.get("was_abandoned") is True),
           "acts": [_act_id(a) for a in acts]}
    return run, floors, sorted(issues)


def _cards(value):
    if not isinstance(value, list) or any(not isinstance(c, dict) or not _id(c.get("id")) for c in value):
        raise StatsUnavailable("incomplete_card_records")
    return value


def normalize_run(raw: dict, *, character: str, snapshots_out: dict | None = None) -> tuple[list[dict], list[str]]:
    """Reconstruct pre-floor card lifetimes, not final-deck co-occurrence.

    Final survivors plus explicit removals describe lifetimes. Gains validate
    their acquisition counts; upgrades are replayed forward. Unknown metadata and
    transforms without lifetimes are run-wide issues. A gained card that has no
    lifetime left the deck at an unknown later floor, so it only spoils the deck
    contexts after that gain. A card whose own record gives its acquisition floor
    but which has no gain record (event, relic or other source) keeps a known
    lifetime and is only noted. Only complete card-identity contexts enter similarity.
    """
    players, acts = raw.get("players"), raw.get("map_point_history")
    if (not isinstance(players, list) or len(players) != 1 or not isinstance(players[0], dict)
            or _character(players[0].get("character")) != character or not isinstance(acts, list)):
        raise StatsUnavailable("unsupported_run_shape")
    floors, number = [], 0
    for act_no, act in enumerate(acts, 1):
        if not isinstance(act, list):
            raise StatsUnavailable("incomplete_floor_history")
        for point in act:
            number += 1
            if not isinstance(point, dict):
                raise StatsUnavailable("incomplete_floor_history")
            seats = point.get("player_stats")
            if not isinstance(seats, list) or len(seats) != 1 or not isinstance(seats[0], dict):
                raise StatsUnavailable("incomplete_seat_history")
            rooms = point.get("rooms")
            if not isinstance(rooms, list) or any(not isinstance(r, dict) for r in rooms):
                raise StatsUnavailable("incomplete_room_history")
            floors.append((number, act_no, seats[0], rooms))
    if not floors:
        raise StatsUnavailable("empty_floor_history")
    issues, lives = set(), []

    def lifetime(card, end):
        cid = _id(card.get("id"))
        born = _count(card.get("floor_added_to_deck"))
        if born is None or born < 1 or born > end:
            issues.add("unknown_card_acquisition_floor")
        lives.append({"id": cid, "born": born, "end": end, "upgrade_level": 0})

    for card in _cards(players[0].get("deck")):
        lifetime(card, number + 1)
    for floor, _, stats, _ in floors:
        for card in _cards(stats.get("cards_removed", [])):
            lifetime(card, floor)
        if any(stats.get(k) for k in ("cards_transformed", "transformed_cards")):
            issues.add("transformation_lifetimes_unverified")
        gained = _cards(stats.get("cards_gained", []))
        for card in gained:
            if _count(card.get("current_upgrade_level", 0)) is None:
                issues.add("unknown_acquired_upgrade_level")
    transforms = "transformation_lifetimes_unverified" in issues
    notes, lost_after = set(), None   # lost_after: first floor whose gain has no lifetime
    for floor, _, stats, _ in floors:
        if floor > 1:
            actual = Counter(_id(c.get("id")) for c in stats.get("cards_gained", []))
            expected = Counter(c["id"] for c in lives if c["born"] == floor)
            if actual - expected:
                lost_after = floor if lost_after is None else min(lost_after, floor)
            if expected - actual:
                if transforms:
                    issues.add("gains_do_not_reconcile_with_lifetimes")
                else:
                    notes.add("card_added_without_gain_record_lifetime_known")
    snapshots = {}
    upgrade_issues = set()
    for floor, _, stats, _ in floors:
        present = [c for c in lives if c["born"] is not None and c["born"] < floor <= c["end"]]
        context_issues = sorted(issues | ({"initial_room_deck_unknown"} if floor == 1 else set())
                                | ({"gained_card_without_lifetime_earlier"}
                                   if lost_after is not None and floor > lost_after else set()))
        snapshots[floor] = {
            "cards": [{"id": c["id"], "upgrade_level": c["upgrade_level"]} for c in present],
            "identity_complete": not context_issues,
            "issues": sorted(set(context_issues) | upgrade_issues), "basis": "before_floor",
            "upgrades_complete": not upgrade_issues,
        }
        # Acquired upgrade levels apply after this floor, never before its choice.
        gained = stats.get("cards_gained", [])
        for cid in {_id(c.get("id")) for c in gained}:
            acquisitions = [c for c in lives if c["born"] == floor and c["id"] == cid]
            levels = [c.get("current_upgrade_level", 0) for c in gained if _id(c.get("id")) == cid]
            if len(acquisitions) == len(levels) and all(_count(x) is not None for x in levels):
                for card, level in zip(acquisitions, sorted(levels)):
                    card["upgrade_level"] = level
        upgraded = stats.get("upgraded_cards", [])
        if not isinstance(upgraded, list):
            upgrade_issues.add("invalid_upgrade_history")
            upgraded = []
        for item in upgraded:
            cid = _id(item.get("id") if isinstance(item, dict) else item)
            eligible = [c for c in lives if c["id"] == cid and c["born"] is not None
                        and c["born"] <= floor < c["end"]]
            if not eligible:
                upgrade_issues.add("ambiguous_upgrade_identity")
                continue
            # Copies of one card are interchangeable while they share a lifetime: upgrade the
            # least-upgraded one. Only copies that leave the deck at different floors are ambiguous.
            if len({c["end"] for c in eligible}) > 1:
                upgrade_issues.add("ambiguous_upgrade_identity")
            min(eligible, key=lambda c: (c["upgrade_level"], -c["end"]))["upgrade_level"] += 1
    if snapshots_out is not None:
        snapshots_out.update(snapshots)
    decisions = []
    for floor, act, stats, rooms in floors:
        room_types = {str(r.get("room_type", "")).lower() for r in rooms}
        shop = "shop" in room_types
        common = {"floor": floor, "act": act, "deck": snapshots[floor]}
        for field, entity_type, kind in (("card_choices", "cards", "shop" if shop else "card_reward"),
                                          ("relic_choices", "relics", "shop"),
                                          ("potion_choices", "potions", "shop")):
            if field != "card_choices" and not shop:
                continue
            offers = stats.get(field, [])
            if not isinstance(offers, list):
                issues.add("invalid_offer_records")
                continue
            seen = {}
            for offer in offers:
                if not isinstance(offer, dict) or type(offer.get("was_picked")) is not bool:
                    issues.add("incomplete_offer_record")
                    continue
                item = offer.get("card") if entity_type == "cards" else offer.get("choice")
                eid = _id(item.get("id") if isinstance(item, dict) else item)
                if not eid:
                    issues.add("unmatched_offer_id")
                    continue
                # Shop rows count one shelf appearance per item per visit.
                if shop:
                    seen[eid] = max(seen.get(eid, 0), int(offer["was_picked"]))
                else:
                    decisions.append({**common, "kind": kind, "entity_type": entity_type,
                                      "entity_id": eid, "event_id": "", "chosen": int(offer["was_picked"])})
            if shop:
                bought = stats.get("bought_" + entity_type, [])
                if isinstance(bought, list):
                    for item in bought:
                        eid = _id(item.get("id") if isinstance(item, dict) else item)
                        if eid:
                            seen[eid] = 1
                        else:
                            issues.add("unmatched_purchased_item")
                else:
                    issues.add("invalid_purchase_records")
                if entity_type == "cards" and stats.get("cards_gained") and "bought_cards" not in stats:
                    # No purchase list: a card gained on a shop floor that was on the shelf was bought.
                    gained_ids = {_id(c.get("id")) for c in stats["cards_gained"] if isinstance(c, dict)}
                    for eid in gained_ids & set(seen):
                        seen[eid] = 1
                    if gained_ids - set(seen):
                        issues.add("shop_card_gain_purchase_attribution_unknown")
            for eid, chosen in seen.items():
                decisions.append({**common, "kind": "shop", "entity_type": entity_type,
                                  "entity_id": eid, "event_id": "", "chosen": chosen})
        choices = stats.get("event_choices", [])
        if not isinstance(choices, list):
            issues.add("invalid_event_records")
            continue
        for choice in choices:
            title = choice.get("title") if isinstance(choice, dict) else None
            if not isinstance(title, dict) or title.get("table") != "events":
                continue  # Ancient relic titles aren't ordinary event options.
            match = re.fullmatch(r"([A-Z0-9_]+)(?:\.pages\.[A-Z0-9_]+)*\.options\.([A-Z0-9_]+)\.title",
                                 str(title.get("key", "")), re.I)
            if match:
                decisions.append({**common, "kind": "event", "entity_type": "events",
                                  "event_id": match[1].upper(), "entity_id": match[2].upper(), "chosen": 1})
            else:
                issues.add("unmatched_event_option")
    if lost_after is not None:
        notes.add("gained_card_without_lifetime")
    return decisions, sorted(issues | upgrade_issues | notes)


def _similarity(cards, target):
    left, right = Counter(c["id"] for c in cards), Counter(target)
    union = sum((left | right).values())
    return sum((left & right).values()) / union if union else 1.0


class RunHistoryIndex:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.cache_dir) / "run_history.sqlite3"
        self.rotation = 0

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=0.2)
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
              hash TEXT PRIMARY KEY, username TEXT, character TEXT, ascension INTEGER,
              version TEXT, mode TEXT, normalizer INTEGER, issues TEXT, fetched_at REAL);
            CREATE TABLE IF NOT EXISTS decisions (
              run_hash TEXT, seq INTEGER, kind TEXT, entity_type TEXT, entity_id TEXT,
              event_id TEXT, floor INTEGER, act INTEGER, chosen INTEGER, deck TEXT,
              PRIMARY KEY (run_hash, seq));
            CREATE INDEX IF NOT EXISTS decision_lookup ON decisions(kind, entity_type, entity_id, event_id);
            CREATE TABLE IF NOT EXISTS sync (
              username TEXT, filters TEXT, page INTEGER, refresh_after REAL,
              PRIMARY KEY(username, filters));
            CREATE TABLE IF NOT EXISTS rejected (
              hash TEXT PRIMARY KEY, normalizer INTEGER, reason TEXT);
            CREATE TABLE IF NOT EXISTS skipped (
              hash TEXT, filters TEXT, normalizer INTEGER, reason TEXT,
              PRIMARY KEY(hash, filters));
            CREATE TABLE IF NOT EXISTS route_runs (
              run_hash TEXT PRIMARY KEY, win INTEGER, abandoned INTEGER, acts TEXT, issues TEXT);
            CREATE TABLE IF NOT EXISTS route_floors (
              run_hash TEXT, act INTEGER, act_id TEXT, act_floor INTEGER, floor INTEGER,
              map_point_type TEXT, room_type TEXT, encounter_id TEXT, hp REAL, max_hp REAL,
              damage_taken REAL, gold REAL, rest_choices TEXT, PRIMARY KEY (run_hash, floor));
            CREATE INDEX IF NOT EXISTS route_act ON route_floors(act_id);
            CREATE TABLE IF NOT EXISTS ancient_offers (
              run_hash TEXT, floor INTEGER, act INTEGER, act_id TEXT, ancient_id TEXT, relic_id TEXT,
              chosen INTEGER, deck TEXT, PRIMARY KEY (run_hash, floor, relic_id));
        """)
        return closing(con)

    def ingest(self, con, username, run_hash, raw, scope):
        if (raw.get("ascension") != scope["ascension"] or raw.get("game_mode") != "standard"
                or (scope["version"] is not None and raw.get("build_id") != scope["version"])):
            raise StatsUnavailable("run_filter_mismatch")
        snapshots = {}
        try:
            decisions, issues = normalize_run(raw, character=scope["character"], snapshots_out=snapshots)
        except StatsUnavailable as exc:
            if str(exc) == "unsupported_run_shape":
                raise
            # Route facts do not depend on card records; keep them.
            decisions, issues = [], ["decision_history_unavailable:" + str(exc)]
        route, floors, route_issues = normalize_route(raw)
        with con:
            con.execute("DELETE FROM route_floors WHERE run_hash=?", (run_hash,))
            con.execute("DELETE FROM ancient_offers WHERE run_hash=?", (run_hash,))
            con.executemany("INSERT OR REPLACE INTO ancient_offers VALUES(?,?,?,?,?,?,?,?)", [
                (run_hash, f["floor"], f["act"], f["act_id"], f["ancient_id"], rid, int(chosen),
                 json.dumps(snapshots[f["floor"]]) if f["floor"] in snapshots else None)
                for f in floors for rid, chosen in f["ancient_offers"]])
            con.execute("INSERT OR REPLACE INTO route_runs VALUES(?,?,?,?,?)",
                        (run_hash, route["win"], route["abandoned"], json.dumps(route["acts"]),
                         json.dumps(route_issues)))
            con.executemany("INSERT INTO route_floors VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (run_hash, f["act"], f["act_id"], f["act_floor"], f["floor"], f["map_point_type"],
                 f["room_type"], f["encounter_id"], f["hp"], f["max_hp"], f["damage_taken"], f["gold"],
                 json.dumps(f["rest_choices"])) for f in floors])
            con.execute("DELETE FROM decisions WHERE run_hash=?", (run_hash,))
            con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_hash, username, scope["character"], scope["ascension"], raw.get("build_id"),
                         "standard", NORMALIZER_VERSION, json.dumps(issues), time.time()))
            con.executemany("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)", [
                (run_hash, i, d["kind"], d["entity_type"], d["entity_id"], d["event_id"], d["floor"],
                 d["act"], d["chosen"], json.dumps(d["deck"])) for i, d in enumerate(decisions)])
        return issues

    def sync(self, client, cohort, scope, *, deadline, clock):
        result = {"status": "available", "new_runs": 0, "fetched_runs": 0, "players": [], "game_mode": "standard",
                  "run_limit_per_player": self.config.history_runs_per_player,
                  "coverage": "bounded submitted runs; not an exhaustive or unbiased sample"}
        filters = json.dumps(scope, sort_keys=True)
        with self._connect() as con:
            ordered = cohort[self.rotation:] + cohort[:self.rotation]
            self.rotation = (self.rotation + 1) % max(1, len(cohort))
            for member in ordered:
                username = member["username"]
                state = {"username": username, "status": "available"}
                result["players"].append(state)
                indexed = con.execute("SELECT count(*) FROM runs WHERE username=? AND character=? AND ascension=? "
                                      "AND (? IS NULL OR version=?) AND normalizer=?",
                                      (username, scope["character"], scope["ascension"], scope["version"],
                                       scope["version"], NORMALIZER_VERSION)).fetchone()[0]
                state["indexed_runs"] = indexed
                if indexed >= self.config.history_runs_per_player:
                    state["status"] = "run_limit_reached"
                    continue
                if result["fetched_runs"] >= self.config.history_new_runs_per_decision or clock() >= deadline:
                    state["status"] = "deferred_budget"
                    result["status"] = "partial"
                    continue
                cursor = con.execute("SELECT page,refresh_after FROM sync WHERE username=? AND filters=?",
                                     (username, filters)).fetchone()
                page = cursor["page"] if cursor else 1
                if cursor and time.time() < cursor["refresh_after"]:
                    continue
                try:
                    listing = client.get("/api/runs/list", {"username": username, "character": scope["character"],
                                         "ascension": scope["ascension"], "build_id": scope["version"], "players": 1,
                                         "game_mode": "standard", "sort": "newest", "page": page, "limit": 20},
                                         deadline=deadline)
                    rows = listing.data.get("runs")
                    if not isinstance(rows, list):
                        raise StatsUnavailable("invalid_run_listing")
                    all_indexed = True
                    for row in rows:
                        run_hash = row.get("run_hash") if isinstance(row, dict) else None
                        if not isinstance(run_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{16,64}", run_hash):
                            raise StatsUnavailable("invalid_run_hash")
                        existing = con.execute("SELECT normalizer FROM runs WHERE hash=?", (run_hash,)).fetchone()
                        if existing and existing[0] == NORMALIZER_VERSION:
                            continue
                        rejected = con.execute("SELECT reason FROM rejected WHERE hash=? AND normalizer=?",
                                               (run_hash, NORMALIZER_VERSION)).fetchone()
                        if rejected:
                            state["rejected_run_reason"] = rejected[0]
                            continue
                        skipped = con.execute("SELECT reason FROM skipped WHERE hash=? AND filters=? AND normalizer=?",
                                              (run_hash, filters, NORMALIZER_VERSION)).fetchone()
                        if skipped:
                            state["skipped_run_reason"] = skipped[0]
                            continue
                        all_indexed = False
                        owner = row.get("username")
                        if not isinstance(owner, str) or owner.casefold() != username.casefold():
                            raise StatsUnavailable("run_owner_mismatch")
                        result["fetched_runs"] += 1
                        shared = client.get("/api/runs/shared/" + run_hash, {}, deadline=deadline)
                        try:
                            state["normalization_issues"] = self.ingest(con, username, run_hash, shared.data, scope)
                        except StatsUnavailable as exc:
                            # Filter mismatches can be valid in another character/
                            # patch scope: skip them for this scope only, so the
                            # same hash cannot block later runs in this listing.
                            if str(exc) in ("run_filter_mismatch", "unsupported_run_shape"):
                                with con:
                                    con.execute("INSERT OR REPLACE INTO skipped VALUES(?,?,?,?)",
                                                (run_hash, filters, NORMALIZER_VERSION, str(exc)))
                                state["skipped_run_reason"] = str(exc)
                                break  # still at most one fetched run per player per decision
                            with con:
                                con.execute("INSERT OR REPLACE INTO rejected VALUES(?,?,?)",
                                            (run_hash, NORMALIZER_VERSION, str(exc)))
                            raise
                        result["new_runs"] += 1
                        state["indexed_runs"] += 1
                        break  # round-robin: at most one new run per player per decision
                    if all_indexed:
                        total_pages = listing.data.get("total_pages")
                        more = type(total_pages) is int and page < total_pages
                        with con:
                            con.execute("INSERT OR REPLACE INTO sync VALUES(?,?,?,?)",
                                        (username, filters, page + 1 if more else 1,
                                         0 if more else time.time() + self.config.cache_ttl_s))
                        state["status"] = "next_page_pending" if more else "current_listing_indexed"
                except StatsUnavailable as exc:
                    state.update({"status": "unavailable", "reason": str(exc)})
                    result["status"] = "partial"
        return result

    def route_rows(self, cohort, scope, act_id):
        """Every indexed in-scope run that entered ``act_id``, with its full floor list."""
        names = [m["username"] for m in cohort]
        if not names:
            return []
        sql = ("SELECT r.hash, r.username, rr.win, rr.abandoned, rr.issues FROM runs r "
               "JOIN route_runs rr ON rr.run_hash=r.hash WHERE r.username IN (" + ",".join("?" for _ in names)
               + ") AND r.character=? AND r.ascension=? AND r.mode='standard' AND r.normalizer=? "
               "AND (? IS NULL OR r.version=?) AND EXISTS (SELECT 1 FROM route_floors f "
               "WHERE f.run_hash=r.hash AND f.act_id=?)")
        params = names + [scope["character"], scope["ascension"], NORMALIZER_VERSION,
                          scope["version"], scope["version"], act_id]
        with self._connect() as con:
            runs = [dict(r) for r in con.execute(sql, params)]
            for run in runs:
                run["floors"] = [dict(f) for f in con.execute(
                    "SELECT * FROM route_floors WHERE run_hash=? ORDER BY floor", (run["hash"],))]
                for f in run["floors"]:
                    f["rest_choices"] = json.loads(f["rest_choices"])
        return runs

    def ancient_rows(self, cohort, scope):
        """Every indexed in-scope ancient offer row, with run outcome and arrival HP%."""
        names = [m["username"] for m in cohort]
        if not names:
            return []
        sql = ("SELECT a.*, r.username, rr.win, rr.abandoned, p.hp AS prev_hp, p.max_hp AS prev_max_hp "
               "FROM ancient_offers a JOIN runs r ON r.hash=a.run_hash JOIN route_runs rr ON rr.run_hash=a.run_hash "
               "LEFT JOIN route_floors p ON p.run_hash=a.run_hash AND p.floor=a.floor-1 WHERE r.username IN ("
               + ",".join("?" for _ in names) + ") AND r.character=? AND r.ascension=? AND r.mode='standard' "
               "AND r.normalizer=? AND (? IS NULL OR r.version=?)")
        params = names + [scope["character"], scope["ascension"], NORMALIZER_VERSION, scope["version"], scope["version"]]
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(sql, params)]
        for r in rows:
            r["deck"] = json.loads(r["deck"]) if r["deck"] else None
            r["arrival_hp_pct"] = (r["prev_hp"] / r["prev_max_hp"] * 100 if r["prev_hp"] is not None
                                   and r["prev_max_hp"] else None)
        return rows

    def compare(self, cohort, scope, option, *, floor, act, deck):
        kind = {"pick_rate": "card_reward", "buy_rate": "shop", "choice_share": "event"}.get(option.get("metric"))
        if kind is None:
            return {"status": "unavailable", "reason": "unmatched_option"}
        names = [m["username"] for m in cohort]
        if not names:
            return {"status": "unavailable", "reason": "empty_cohort"}
        sql = ("SELECT d.*,r.username,r.issues AS run_issues FROM decisions d JOIN runs r ON r.hash=d.run_hash WHERE r.username IN ("
               + ",".join("?" for _ in names) + ") AND r.character=? AND r.ascension=? AND r.mode='standard' "
               "AND r.normalizer=? AND (? IS NULL OR r.version=?) AND d.kind=?")
        params = names + [scope["character"], scope["ascension"], NORMALIZER_VERSION, scope["version"], scope["version"], kind]
        if kind == "event":
            sql += " AND d.event_id=?"
            params.append(option["event_id"])
        else:
            sql += " AND d.entity_type=? AND d.entity_id=?"
            params.extend([option.get("entity_type", "cards"), option["id"]])
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(sql, params)]
        complete = 0
        for row in rows:
            context = json.loads(row["deck"])
            row["context"] = context
            row["similarity"] = (_similarity(context["cards"], deck)
                                 if deck is not None and context["identity_complete"] else None)
            complete += int(context["identity_complete"])
        attempts, selected, chosen_scope = [], [], None
        for match_deck in ([True, False] if deck is not None else [False]):
            for level in ("nearby_floor", "act", "cohort"):
                if level == "nearby_floor" and type(floor) is not int:
                    continue
                if level == "act" and type(act) is not int:
                    continue
                subset = [r for r in rows if
                          (not match_deck or (r["similarity"] is not None and r["similarity"] >= self.config.deck_similarity_min))
                          and (level != "nearby_floor" or (abs(r["floor"] - floor) <= self.config.floor_window
                               and (type(act) is not int or r["act"] == act)))
                          and (level != "act" or r["act"] == act)]
                used = {"level": level, "deck_conditioned": match_deck,
                        "floor_min": max(1, floor - self.config.floor_window) if level == "nearby_floor" else None,
                        "floor_max": floor + self.config.floor_window if level == "nearby_floor" else None,
                        "act": act if level in ("act", "nearby_floor") else None}
                attempts.append({**used, "samples": len(subset)})
                if subset:
                    selected, chosen_scope = subset, used
                if len(subset) >= self.config.min_context_samples:
                    break
            if selected and len(selected) >= self.config.min_context_samples:
                break
        if not selected:
            return {"status": "unavailable", "reason": "no_indexed_observations",
                    "attempts": [a["level"] + ("+deck" if a["deck_conditioned"] else "") + ":0" for a in attempts]}
        contributions = {}
        for row in selected:
            n = int(row["entity_id"] == option["option_id"]) if kind == "event" else row["chosen"]
            counts = contributions.setdefault(row["username"], [0, 0])
            counts[0] += n
            counts[1] += 1
        numerator, denominator = sum(c[0] for c in contributions.values()), len(selected)
        top, other = _top_contributors([{"username": u, "numerator": c[0], "denominator": c[1]}
                                        for u, c in contributions.items()])
        rung = chosen_scope["level"] + ("+deck" if chosen_scope["deck_conditioned"] else "")
        return _withhold({"status": "available", "metric": option["metric"], "numerator": numerator,
                "denominator": denominator, "rate_pct": round(numerator / denominator * 100, 2),
                "selected_rung": rung, "rung_qualifies": denominator >= self.config.min_context_samples,
                "largest_contributor_share_pct": round(max(c[1] for c in contributions.values()) / denominator * 100, 1),
                "scope": {**chosen_scope, "game_mode": "standard", "context_basis": "before_floor",
                          "similarity": "base_card_multiset_jaccard", "similarity_min": self.config.deck_similarity_min},
                "sparse": denominator < self.config.min_context_samples,
                # Compact widening trail: level[+deck]:samples, in the order tried.
                "attempts": [a["level"] + ("+deck" if a["deck_conditioned"] else "") + ":" + str(a["samples"])
                             for a in attempts],
                "contributing_runs": len({r["run_hash"] for r in selected}),
                "contributing_players": len(contributions),
                "contributors": top, **({"other_contributors": other} if other else {}),
                "candidate_contexts": len(rows), "complete_identity_contexts": complete,
                "incomplete_identity_contexts": len(rows) - complete,
                "normalization_issues": sorted({i for r in selected for i in json.loads(r["run_issues"])}),
                "selected_context_issues": sorted({i for r in selected for i in r["context"]["issues"]})},
                self.config.min_context_samples)
