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

from .expert_stats import StatsUnavailable, _character, _count, _id, _top_contributors

NORMALIZER_VERSION = 1


def _cards(value):
    if not isinstance(value, list) or any(not isinstance(c, dict) or not _id(c.get("id")) for c in value):
        raise StatsUnavailable("incomplete_card_records")
    return value


def normalize_run(raw: dict, *, character: str) -> tuple[list[dict], list[str]]:
    """Reconstruct pre-floor card lifetimes, not final-deck co-occurrence.

    Final survivors plus explicit removals describe lifetimes. Gains validate
    their acquisition counts; upgrades are replayed forward. Unknown metadata,
    unmatched gains, transforms without lifetimes, and ambiguous upgrades are
    retained as issues. Only complete card-identity contexts enter similarity.
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
    for floor, _, stats, _ in floors:
        if floor > 1:
            actual = Counter(_id(c.get("id")) for c in stats.get("cards_gained", []))
            expected = Counter(c["id"] for c in lives if c["born"] == floor)
            if actual != expected:
                issues.add("gains_do_not_reconcile_with_lifetimes")
    snapshots = {}
    upgrade_issues = set()
    for floor, _, stats, _ in floors:
        present = [c for c in lives if c["born"] is not None and c["born"] < floor <= c["end"]]
        context_issues = sorted(issues | ({"initial_room_deck_unknown"} if floor == 1 else set()))
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
            if len(eligible) == 1:
                eligible[0]["upgrade_level"] += 1
            else:
                upgrade_issues.add("ambiguous_upgrade_identity")
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
            match = re.fullmatch(r"([A-Z0-9_]+)(?:\.pages\.[A-Z0-9_]+)?\.options\.([A-Z0-9_]+)\.title",
                                 str(title.get("key", "")), re.I)
            if match:
                decisions.append({**common, "kind": "event", "entity_type": "events",
                                  "event_id": match[1].upper(), "entity_id": match[2].upper(), "chosen": 1})
            else:
                issues.add("unmatched_event_option")
    return decisions, sorted(issues | upgrade_issues)


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
        """)
        return closing(con)

    def ingest(self, con, username, run_hash, raw, scope):
        if (raw.get("ascension") != scope["ascension"] or raw.get("game_mode") != "standard"
                or (scope["version"] is not None and raw.get("build_id") != scope["version"])):
            raise StatsUnavailable("run_filter_mismatch")
        decisions, issues = normalize_run(raw, character=scope["character"])
        with con:
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
        return {"status": "available", "metric": option["metric"], "numerator": numerator,
                "denominator": denominator, "rate_pct": round(numerator / denominator * 100, 2),
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
                "selected_context_issues": sorted({i for r in selected for i in r["context"]["issues"]})}
