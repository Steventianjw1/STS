"""Read-only Spire Codex evidence for rewards, events and merchants.

Rates pool public-profile counts and locally indexed historical decisions.
Deck advice is separate community evidence, never an expert pick probability.
Missing/failed profiles and unmatched options stay unknown. No game actions.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import ExpertStatsConfig

API_ROOT = "https://spire-codex.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class StatsUnavailable(Exception):
    pass


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _id(value):
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    value = re.sub(r"^(?:CARD|RELIC|POTION|EVENT|CHARACTER)\.", "", value)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", value) else None


def _title(value):
    return " ".join(re.sub(r"\[[^\]]*\]|<[^>]*>", "", value or "").split()).casefold()


def _character(value):
    # STS2MCP exposes English display names (e.g. "The Ironclad") on some builds.
    if isinstance(value, str) and value.strip().upper().startswith("THE "):
        value = value.strip()[4:]
    return _id(value)


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@dataclass
class CachedResponse:
    data: dict
    fetched_at: float
    cache: str

    def provenance(self):
        return {"fetched_at": _iso(self.fetched_at), "cache": self.cache}


class SpireCodexClient:
    """Bounded GETs with memory/disk caching; errors never become zero counts.

    Expired data is not served. Negative results are briefly cached in memory;
    a 429 delays all further requests without sleeping during a game decision.
    Credentials are read from the environment and never stored in the cache.
    """

    def __init__(self, config: ExpertStatsConfig, *, opener=urllib.request.urlopen,
                 clock=time.monotonic, wall_clock=time.time):
        self.config, self.opener = config, opener
        self.clock, self.wall_clock = clock, wall_clock
        self.memory: dict[str, CachedResponse] = {}
        self.failures: dict[str, tuple[float, str]] = {}
        self.backoff_until = 0.0

    def get(self, path: str, params: dict, *, deadline: float) -> CachedResponse:
        return self._request(path, params, deadline=deadline)

    def post(self, path: str, body: dict, *, deadline: float) -> CachedResponse:
        # draft-advice is a read-only calculation despite its HTTP method.
        if path != "/api/draft-advice":
            raise StatsUnavailable("unsupported_statistics_post")
        return self._request(path, {}, body=body, deadline=deadline)

    def _request(self, path: str, params: dict, *, deadline: float, body: dict | None = None) -> CachedResponse:
        query = urllib.parse.urlencode(sorted((k, v) for k, v in params.items() if v is not None))
        url = API_ROOT + path + ("?" + query if query else "")
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode() if body is not None else None
        key = hashlib.sha256(url.encode() + (b"\nPOST\n" + payload if payload is not None else b"")).hexdigest()
        cache_path = Path(self.config.cache_dir) / (key + ".json")
        now = self.wall_clock()
        failure = self.failures.get(key)
        if failure and now < failure[0]:
            raise StatsUnavailable(failure[1])
        cached = self.memory.get(key)
        if cached is None:
            try:
                if cache_path.stat().st_size <= MAX_RESPONSE_BYTES:
                    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
                    if not isinstance(envelope, dict):
                        raise ValueError("invalid cache envelope")
                    stamp = envelope["fetched_at"]
                    if (envelope.get("url") == url and isinstance(envelope["data"], dict)
                            and type(stamp) in (int, float) and math.isfinite(stamp)):
                        cached = CachedResponse(envelope["data"], stamp, "disk")
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if cached and 0 <= now - cached.fetched_at < self.config.cache_ttl_s:
            self.memory[key] = cached
            return CachedResponse(cached.data, cached.fetched_at, "memory" if cached.cache != "disk" else "disk")
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise StatsUnavailable("fetch_budget_exhausted")
        if now < self.backoff_until:
            raise StatsUnavailable("rate_limited")
        headers = {"User-Agent": "STS-Agent/0.1 (Spire Codex public statistics)", "Accept": "application/json"}
        api_key = os.environ.get("SPIRE_CODEX_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        try:
            if payload is not None:
                headers["Content-Type"] = "application/json"
            with self.opener(urllib.request.Request(url, data=payload, headers=headers),
                             timeout=min(self.config.request_timeout_s, remaining)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("oversize response")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected object")
            result = CachedResponse(data, self.wall_clock(), "network")
        except urllib.error.HTTPError as exc:
            reason = f"http_{exc.code}"
            delay = 60.0
            if exc.code == 429:
                retry = exc.headers.get("Retry-After", "60") if exc.headers else "60"
                try:
                    delay = max(1.0, float(retry))
                    if not math.isfinite(delay):
                        delay = 60.0
                except ValueError:
                    try:
                        delay = max(1.0, parsedate_to_datetime(retry).timestamp() - now)
                    except (ValueError, TypeError, OverflowError):
                        delay = 60.0
                self.backoff_until = now + delay
            self.failures[key] = (now + delay, reason)
            raise StatsUnavailable(reason) from None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            self.failures[key] = (now + 60.0, "request_failed")
            raise StatsUnavailable("request_failed") from None
        self.memory[key] = result
        # Cache failure must not discard a usable public API response.
        temporary = None
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"url": url, "fetched_at": result.fetched_at, "data": result.data}, stream,
                          ensure_ascii=False, allow_nan=False)
            os.replace(temporary, cache_path)
        except (OSError, ValueError):
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return result


def select_cohort(board: dict, character: str, *, size: int, min_runs: int) -> list[dict]:
    """Rank eligible returned accounts by this character's Elo, not overall Elo."""
    if not isinstance(board.get("players"), list):
        raise StatsUnavailable("invalid_leaderboard")
    eligible = {}
    for player in board["players"]:
        if not isinstance(player, dict):
            continue
        username = player.get("username")
        splits = player.get("by_character")
        if not isinstance(username, str) or not username.strip() or not isinstance(splits, dict):
            continue
        stats = next((v for k, v in splits.items() if _id(k) == character), None)
        if not isinstance(stats, dict):
            continue
        runs, elo = _count(stats.get("runs")), stats.get("elo")
        if runs is None or runs < min_runs or type(elo) not in (int, float) or not math.isfinite(elo):
            continue
        eligible[username.casefold()] = {"username": username, "character_elo": elo,
                                         "character_rated_runs": runs}
    return sorted(eligible.values(), key=lambda p: (-p["character_elo"], -p["character_rated_runs"],
                                                   p["username"].casefold()))[:size]


def _profile_counts(data):
    if data.get("available") is not True:
        raise StatsUnavailable("profile_unavailable")
    if _count(data.get("runs")) is None or not isinstance(data.get("tables"), dict):
        raise StatsUnavailable("invalid_profile")
    tables = data["tables"]
    if not all(isinstance(tables.get(k), list) for k in ("cards", "shops", "events")):
        raise StatsUnavailable("invalid_profile")
    if not data["runs"]:
        raise StatsUnavailable("no_runs_for_filters")
    result = {"cards": {}, "shops": {}, "events": {}}
    for table in result:
        for row in tables[table]:
            if not isinstance(row, dict):
                raise StatsUnavailable("invalid_profile")
            if table == "events":
                key = (_id(row.get("event")), _id(row.get("option")))
                num, den = _count(row.get("chosen")), None
            else:
                key = _id(row.get("id")) if table == "cards" else (row.get("entity_type"), _id(row.get("id")))
                num = _count(row.get("taken" if table == "cards" else "bought"))
                den = _count(row.get("offered" if table == "cards" else "seen"))
            if num is None or (table != "events" and (den is None or num > den)):
                raise StatsUnavailable("invalid_profile_counts")
            if key is None or (isinstance(key, tuple) and None in key) or key in result[table]:
                raise StatsUnavailable("invalid_profile_ids")
            result[table][key] = (num, den)
    return result


class ExpertDecisionStats:
    def __init__(self, config: ExpertStatsConfig, *, game_build: str | None = None,
                 client=None, clock=time.monotonic):
        for name, low, high in (("cohort_size", 1, 20), ("min_character_runs", 1, 1000)):
            value = getattr(config, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"expert_stats.{name} must be an integer in {low}..{high}")
        for name in ("cache_ttl_s", "request_timeout_s", "max_fetch_s"):
            value = getattr(config, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"expert_stats.{name} must be positive and finite")
        if config.ascension is not None and (type(config.ascension) is not int or not 0 <= config.ascension <= 10):
            raise ValueError("expert_stats.ascension must be 0..10 or omitted for the observed difficulty")
        for name, low, high in (("history_runs_per_player", 1, 10000), ("history_new_runs_per_decision", 1, 20),
                               ("floor_window", 0, 20), ("min_context_samples", 1, 10000)):
            value = getattr(config, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"expert_stats.{name} must be an integer in {low}..{high}")
        if (type(config.deck_similarity_min) not in (int, float)
                or not 0 <= config.deck_similarity_min <= 1):
            raise ValueError("expert_stats.deck_similarity_min must be in 0..1")
        self.config, self.game_build, self.clock = config, game_build, clock
        self.client = client or SpireCodexClient(config, clock=clock)
        from .spire_history import RunHistoryIndex
        self.history = RunHistoryIndex(config)

    def for_view(self, view, *, budget_s: float | None = None) -> dict | None:
        budget = min(self.config.max_fetch_s, max(0, budget_s) if budget_s is not None else self.config.max_fetch_s)
        deadline = self.clock() + budget
        support = self._profile_view(view, budget_s=budget)
        if support is None:
            return None
        support["schema_version"] = 2
        raw_deck = view.facts.get("deck")
        deck = ([_id(c.get("id")) for c in raw_deck] if isinstance(raw_deck, list)
                and all(isinstance(c, dict) and _id(c.get("id")) for c in raw_deck) else None)
        if self.config.history_enabled and support["cohort"]["members"]:
            try:
                cohort, scope = support["cohort"]["members"], support["scope"]
                support["history_index"] = self.history.sync(self.client, cohort, scope,
                                                            deadline=deadline, clock=self.clock)
                run = view.facts.get("run") or {}
                for option in support["options"]:
                    option["history_context"] = self.history.compare(cohort, scope, option,
                                                                     floor=run.get("floor"), act=run.get("act"), deck=deck)
            except (OSError, sqlite3.Error) as exc:
                support["history_index"] = {"status": "unavailable", "reason": type(exc).__name__}
        if self.config.deck_advice_enabled:
            support["supplementary_deck_advice"] = self._deck_advice(view, deck, deadline=deadline)
        if support["status"] == "unavailable" and any(
                o.get("history_context", {}).get("status") == "available" for o in support["options"]):
            support["status"] = "partial"
        return support

    def _deck_advice(self, view, deck, *, deadline):
        result = {"source": API_ROOT, "label": "COMMUNITY DECK ADVICE; separate from expert rates",
                  "cohort_restricted": False, "floor_conditioned": False, "version_filtered": False,
                  "scores_are_calibrated_probabilities": False,
                  "limitations": ["Base card IDs only; upgrades and precise event/shop utility are not modeled.",
                                   "Pick-coach archetypes are suggestions, not a required deck plan."]}
        relics = view.facts.get("relics")
        character = _character((view.facts.get("run") or {}).get("character"))
        if (deck is None or not character or not isinstance(relics, list)
                or any(not isinstance(r, dict) or not _id(r.get("id")) for r in relics)):
            return {**result, "status": "unavailable", "reason": "observed_permanent_deck_or_relics_unknown"}
        offer = [_id(c["details"].get("id") if c["kind"] == "select_card_reward" else c["details"].get("card_id"))
                 for c in view.candidates if c["kind"] in ("select_card_reward", "shop_purchase")]
        offer = list(dict.fromkeys(c for c in offer if c))
        relic_ids = [_id(r["id"]) for r in relics]
        held = ["cards:" + c for c in deck] + ["relics:" + r for r in relic_ids]
        if offer:
            try:
                response = self.client.post("/api/draft-advice", {"deck": held, "offered": offer, "lang": self.config.lang},
                                            deadline=deadline)
                ranked = response.data.get("ranked")
                if not isinstance(ranked, list):
                    raise StatsUnavailable("invalid_draft_advice")
                # Restrict results to offered IDs; scores remain raw scores.
                rows = [{k: r[k] for k in ("id", "score", "base", "reasons") if k in r}
                        for r in ranked if isinstance(r, dict) and r.get("id") in offer]
                result["draft_advice"] = {"status": "available" if rows else "unavailable", "ranked": rows,
                                          **response.provenance()}
            except StatsUnavailable as exc:
                result["draft_advice"] = {"status": "unavailable", "reason": str(exc)}
        else:
            result["draft_advice"] = {"status": "not_applicable", "reason": "no_offered_cards"}
        try:
            response = self.client.get("/api/runs/pick-coach", {"character": character, "cards": ",".join(deck),
                                       "relics": ",".join(relic_ids), "offer": ",".join(offer[:5]),
                                       "lang": self.config.lang}, deadline=deadline)
            if response.data.get("available") is not True:
                raise StatsUnavailable("pick_coach_unavailable")
            candidates, offers = response.data.get("candidates"), response.data.get("offers")
            if not isinstance(candidates, list) or not isinstance(offers, list):
                raise StatsUnavailable("invalid_pick_coach")
            fields = ("id", "coach_score", "commitment_delta", "winner_support", "take_score", "take_base")
            result["pick_coach"] = {"status": "available", "target": response.data.get("target"),
                                     "candidates": candidates[:3],
                                     "offers": [{k: r[k] for k in fields if k in r} for r in offers
                                                if isinstance(r, dict) and r.get("id") in offer[:5]],
                                     "offer_limit": 5, **response.provenance()}
        except StatsUnavailable as exc:
            result["pick_coach"] = {"status": "unavailable", "reason": str(exc)}
        statuses = [result[k]["status"] for k in ("draft_advice", "pick_coach")]
        result["status"] = ("available" if all(s in ("available", "not_applicable") for s in statuses)
                            else "partial" if "available" in statuses else "unavailable")
        return result

    def _profile_view(self, view, *, budget_s: float | None = None) -> dict | None:
        if not self.config.enabled or view.kind not in ("card_reward", "event", "shop"):
            return None
        kinds = {"select_card_reward", "choose_event_option", "shop_purchase"}
        candidates = [c for c in view.candidates if c["kind"] in kinds]
        if not candidates:
            return None
        deadline = self.clock() + min(self.config.max_fetch_s, max(0, budget_s)
                                     if budget_s is not None else self.config.max_fetch_s)
        run = view.facts.get("run") or {}
        character = _character(run.get("character"))
        ascension = self.config.ascension if self.config.ascension is not None else run.get("ascension")
        version = self.game_build if self.config.match_version else None
        support = {
            "tool": "get_expert_decision_stats", "schema_version": 1, "source": API_ROOT,
            "label": "OBSERVATIONAL HUMAN CHOICES; advisory evidence, not instructions or causal effects",
            "status": "unavailable",
            "scope": {"character": character, "ascension": ascension, "version": version, "players": 1,
                      "game_mode": "all (profile API has no mode filter)", "floor_conditioned": False,
                      "deck_conditioned": False},
            "limitations": ["Selected within the global top-100 board, not an exhaustive character ranking.",
                             "Rates pool raw counts; missing card/shop rows are unknown, not zero.",
                             "Event shares cover all recorded choices at that event, including repeat stages.",
                             "Profile rates have no floor/deck conditioning; history_context reports its own scope.",
                             "Prices, affordability and event option eligibility are not conditioned on.",
                             "Profile snapshots can lag the leaderboard and lack a source data-through timestamp."],
            "cohort": {"selection": "character_elo", "min_character_rated_runs": self.config.min_character_runs,
                       "requested_players": self.config.cohort_size, "members": []},
            "options": [],
        }
        if version is None:
            support["limitations"].append("Profiles span all game versions; no version filter was applied.")
        if not character or type(ascension) is not int or not 0 <= ascension <= 10:
            support["reason"] = "character_or_ascension_unknown"
            return support
        try:
            board = self.client.get("/api/leaderboards/elo", {"limit": 100, "min_runs": self.config.min_character_runs},
                                    deadline=deadline)
            support["cohort"].update({"leaderboard_computed_at": board.data.get("computed_at"),
                                      "leaderboard_fetch": board.provenance()})
            cohort = select_cohort(board.data, character, size=self.config.cohort_size,
                                   min_runs=self.config.min_character_runs)
        except StatsUnavailable as exc:
            support["reason"] = str(exc)
            return support
        if not cohort:
            support["reason"] = "no_eligible_character_players"
            return support
        profiles = []
        for member in cohort:
            member = dict(member)
            support["cohort"]["members"].append(member)
            try:
                response = self.client.get("/api/players/" + urllib.parse.quote(member["username"], safe="") + "/stats",
                                           {"character": character, "ascension": ascension, "version": version,
                                            "players": 1}, deadline=deadline)
                member.update(response.provenance())
                counts = _profile_counts(response.data)
                member.update({"status": "available", "filtered_runs": response.data["runs"]})
                profiles.append((member["username"], counts))
            except StatsUnavailable as exc:
                member.update({"status": "unavailable", "reason": str(exc)})
        support["cohort"]["available_players"] = len(profiles)
        event_id = _id((view.facts.get("screen_details") or {}).get("event_id"))
        event_options = {}
        if view.kind == "event" and event_id and profiles:
            try:
                catalog = self.client.get("/api/events/" + event_id, {"lang": self.config.lang}, deadline=deadline)
                groups = [catalog.data.get("options") or []]
                pages = catalog.data.get("pages") or []
                if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
                    raise StatsUnavailable("invalid_event_catalog")
                groups.extend(page.get("options") or [] for page in pages)
                for group in groups:
                    if not isinstance(group, list):
                        raise StatsUnavailable("invalid_event_catalog")
                    for option in group:
                        if not isinstance(option, dict) or not isinstance(option.get("title"), str):
                            raise StatsUnavailable("invalid_event_catalog")
                        event_options.setdefault(_title(option["title"]), set()).add(_id(option.get("id")))
            except StatsUnavailable as exc:
                support["event_catalog_error"] = str(exc)
                event_options = {}
        for candidate in candidates:
            details = candidate["details"]
            row = {"action_id": candidate["action_id"], "status": "unavailable"}
            if view.kind == "card_reward":
                table, key, metric = "cards", _id(details.get("id")), "pick_rate"
                row["id"] = key
            elif view.kind == "shop":
                category = next((t for t in ("card", "relic", "potion") if details.get(t + "_id")), None)
                key = (category + "s", _id(details.get(category + "_id"))) if category else None
                table, metric = "shops", "buy_rate"
                row.update({"entity_type": key[0] if key else None, "id": key[1] if key else None})
            else:
                option_id = _id(details.get("option_id"))
                matches = event_options.get(_title(details.get("title")), set()) - {None}
                if option_id is None and len(matches) == 1:
                    option_id = next(iter(matches))
                key, table, metric = (event_id, option_id), "events", "choice_share"
                row.update({"event_id": event_id, "option_id": option_id})
            invalid_key = key is None or (isinstance(key, tuple) and None in key)
            if invalid_key:
                row["reason"] = "unmatched_identifier_or_unsupported_action"
            else:
                observations = []
                for username, tables in profiles:
                    observed = tables[table].get(key)
                    if table == "events":
                        den = sum(n for (eid, _), (n, _) in tables[table].items() if eid == event_id)
                        # Event rows exhaust recorded choices. An unchosen option
                        # contributes zero when this player has other choices here.
                        num = observed[0] if observed is not None else 0
                    elif observed is not None:
                        num, den = observed
                    else:
                        continue
                    if den:
                        observations.append({"username": username, "numerator": num, "denominator": den})
                numerator = sum(o["numerator"] for o in observations)
                denominator = sum(o["denominator"] for o in observations)
                row.update({"metric": metric, "numerator": numerator if observations else None,
                            "denominator": denominator if observations else None,
                            "rate_pct": round(numerator / denominator * 100, 2) if denominator else None,
                            "contributing_players": len(observations), "contributors": observations})
                if observations:
                    row.update({"status": "available", "largest_player_sample_share_pct":
                                round(max(o["denominator"] for o in observations) / denominator * 100, 2)})
                else:
                    row["reason"] = "no_recorded_observations"
            support["options"].append(row)
        available = sum(row["status"] == "available" for row in support["options"])
        support["status"] = ("available" if available == len(candidates) and len(profiles) == len(cohort)
                             else "partial" if available else "unavailable")
        return support
