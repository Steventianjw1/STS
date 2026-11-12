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
LIST_CATALOGS = {"/api/acts", "/api/encounters", "/api/cards", "/api/monsters", "/api/powers", "/api/relics"}  # wrapped as {"items": [...]}


class StatsUnavailable(Exception):
    pass


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _deck_ids(deck):
    if not isinstance(deck, list):
        return None
    ids = []
    for card in deck:
        if not isinstance(card, dict):
            return None
        card_id = _id(card.get("id"))
        count = _count(card.get("count", 1))
        if not card_id or not count:
            return None
        ids.extend([card_id] * count)
    return ids


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


MAX_CONTRIBUTORS = 3


def _bounded(value, depth=3):
    """Size-bound untrusted external JSON: scalars kept, strings/lists/keys capped."""
    if isinstance(value, str):
        return value[:160]
    if value is None or type(value) in (bool, int) or (type(value) is float and math.isfinite(value)):
        return value
    if depth <= 0 or not isinstance(value, (list, dict)):
        return None
    if isinstance(value, list):
        return [_bounded(v, depth - 1) for v in value[:5]]
    return {str(k)[:40]: _bounded(v, depth - 1) for k, v in list(value.items())[:12]}


def _top_contributors(rows):
    """Largest samples by denominator; the remainder is summarized, not listed."""
    rows = sorted(rows, key=lambda r: (-r["denominator"], r["username"].casefold()))
    rest = rows[MAX_CONTRIBUTORS:]
    return rows[:MAX_CONTRIBUTORS], ({"players": len(rest), "numerator": sum(r["numerator"] for r in rest),
                                      "denominator": sum(r["denominator"] for r in rest)} if rest else None)


def evidence_strength(denominator, players, min_samples):
    """Whether a pooled count supports quoting a rate at all."""
    if not denominator:
        return "none"
    if denominator < min_samples:
        return "insufficient_sample"
    return "single_contributor" if players < 2 else "adequate"


def _withhold(row, min_samples):
    """Keep the counts; quote ``rate_pct`` only for an adequate, multi-player sample."""
    strength = evidence_strength(row.get("denominator"), row.get("contributing_players") or 0, min_samples)
    row["evidence_strength"] = strength
    if strength != "adequate" and row.get("rate_pct") is not None:
        row["rate_pct"] = None
        row["rate_withheld"] = ("too few observations or a single contributing player: read the counts as "
                                f"anecdote, not as a rate (needs >= {min_samples} observations from >= 2 players)")
    return row


def _loose_title(value):
    """Title without numbers, punctuation and bracketed variables, for tolerant option matching."""
    return " ".join(re.sub(r"[^a-z ]+", " ", _title(value)).split())


def match_event_option(details, exact, loose_index):
    """(option_id, how) for one observed event option; (None, None) when nothing matches uniquely."""
    option_id = _id(details.get("option_id"))
    if option_id:
        return option_id, "mod_option_id"
    title = details.get("title")
    matches = exact.get(_title(title), set()) - {None}
    if len(matches) == 1:
        return next(iter(matches)), "exact_title"
    loose = _loose_title(title)
    if len(loose) >= 4:
        matches = loose_index.get(loose, set()) - {None}
        if len(matches) == 1:
            return next(iter(matches)), "title_without_numbers"
        partial = set()
        for other, ids in loose_index.items():
            if len(other) >= 4 and (other in loose or loose in other):
                partial |= ids - {None}
        if len(partial) == 1:
            return next(iter(partial)), "unique_partial_title"
    return None, None


def coverage_summary(options, min_samples):
    """How far each option's evidence got: identifier matched, ladder rung reached, rate quotable."""
    out = {"options": len(options), "identifier_matched": 0, "profile_rate_quoted": 0, "history_available": 0,
           "history_rate_quoted": 0, "qualifying_any_rung": 0, "qualifying_nearby_floor": 0,
           "qualifying_deck_matched": 0, "single_contributor": 0, "selected_rung": {},
           "min_samples": min_samples}
    for o in options:
        out["identifier_matched"] += o.get("reason") != "unmatched_identifier_or_unsupported_action"
        out["profile_rate_quoted"] += o.get("rate_pct") is not None
        h = o.get("history_context") or {}
        if h.get("status") != "available":
            out["selected_rung"]["none"] = out["selected_rung"].get("none", 0) + 1
            continue
        out["history_available"] += 1
        out["history_rate_quoted"] += h.get("rate_pct") is not None
        rung = h.get("selected_rung") or "unknown"
        out["selected_rung"][rung] = out["selected_rung"].get(rung, 0) + 1
        out["single_contributor"] += h.get("contributing_players") == 1
        if h.get("rung_qualifies"):
            out["qualifying_any_rung"] += 1
            out["qualifying_nearby_floor"] += rung.startswith("nearby_floor")
            out["qualifying_deck_matched"] += rung.endswith("+deck")
    return out


def _ranked_draft(rows):
    """Offer-local rank and gap; the raw number is renamed so it is not read as a lift or a rate."""
    def num(r):
        v = r.get("score")
        return v if type(v) in (int, float) else None
    scored = sorted((r for r in rows if num(r) is not None), key=lambda r: -num(r))
    out = []
    for i, r in enumerate(scored):
        nxt = num(scored[i + 1]) if i + 1 < len(scored) else None
        out.append({"id": r.get("id"), "rank": i + 1, "of": len(scored),
                    "composite_score_uncalibrated": num(r),
                    "gap_to_next": None if nxt is None else round(num(r) - nxt, 4),
                    **{k: r[k] for k in ("base", "reasons") if k in r}})
    return out + [{"id": r.get("id"), "rank": None, "composite_score_uncalibrated": None}
                  for r in rows if num(r) is None]


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
            if path in LIST_CATALOGS and isinstance(data, list):
                data = {"items": data}
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
        self._act_ids: dict = {}   # (character, act number) -> act ID resolved on a map screen

    def for_view(self, view, *, budget_s: float | None = None, route_state: dict | None = None) -> dict | None:
        """Advisory evidence only: unexpected external data never stops a run."""
        try:
            return self._for_view(view, budget_s=budget_s, route_state=route_state)
        except Exception as exc:
            return {"tool": "get_expert_decision_stats", "schema_version": 2, "source": API_ROOT,
                    "status": "unavailable", "reason": "internal_error:" + type(exc).__name__}

    def _for_view(self, view, *, budget_s: float | None = None, route_state: dict | None = None) -> dict | None:
        budget = min(self.config.max_fetch_s, max(0, budget_s) if budget_s is not None else self.config.max_fetch_s)
        deadline = self.clock() + budget
        if view.kind == "map":
            return (self._route_view(view, deadline=deadline, route_state=route_state)
                    if self.config.enabled and self.config.history_enabled else None)
        if (view.kind == "event" and (view.facts.get("screen_details") or {}).get("is_ancient") is True
                and self.config.ancient_advisor_enabled):
            return self._ancient_view(view, deadline=deadline) if self.config.enabled else None
        support = self._profile_view(view, budget_s=budget)
        if support is None:
            return None
        support["schema_version"] = 2
        deck = _deck_ids(view.facts.get("deck"))
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
        support["evidence_coverage"] = coverage_summary(support["options"], self.config.min_context_samples)
        if self.config.deck_advice_enabled:
            support["supplementary_deck_advice"] = self._deck_advice(view, deck, deadline=deadline)
        if self.config.elite_readiness_enabled and view.kind in ("card_reward", "shop"):
            offered = [_id(c["details"].get("id") if c["kind"] == "select_card_reward" else c["details"].get("card_id"))
                       for c in view.candidates if c["kind"] in ("select_card_reward", "shop_purchase")]
            support["elite_readiness"] = self._readiness_for_view(view, None, deadline=deadline,
                                                                  offered=[o for o in offered if o])
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
                rows = [{k: _bounded(r[k]) for k in ("id", "score", "base", "reasons") if k in r}
                        for r in ranked if isinstance(r, dict) and r.get("id") in offer]
                result["draft_advice"] = {"status": "available" if rows else "unavailable",
                                          "ranked": _ranked_draft(rows),
                                          "score_meaning": "composite_score_uncalibrated is a unitless community "
                                                           "ranking score. It is not a lift, win rate, pick rate or "
                                                           "probability; use rank and gap_to_next within this offer only.",
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
            result["pick_coach"] = {"status": "available", "target": _bounded(response.data.get("target")),
                                     "candidates": _bounded(candidates[:3]),
                                     "offers": [{k: _bounded(r[k]) for k in fields if k in r} for r in offers
                                                if isinstance(r, dict) and r.get("id") in offer[:5]],
                                     "offer_limit": 5, **response.provenance()}
        except StatsUnavailable as exc:
            result["pick_coach"] = {"status": "unavailable", "reason": str(exc)}
        statuses = [result[k]["status"] for k in ("draft_advice", "pick_coach")]
        result["status"] = ("available" if all(s in ("available", "not_applicable") for s in statuses)
                            else "partial" if "available" in statuses else "unavailable")
        return result

    @staticmethod
    def _scope(character, ascension, version):
        # Also the history sync cursor key: keep identical for every caller.
        return {"character": character, "ascension": ascension, "version": version, "players": 1,
                "game_mode": "all (profile API has no mode filter)", "floor_conditioned": False,
                "deck_conditioned": False}

    def _cohort(self, character, record, *, deadline):
        board = self.client.get("/api/leaderboards/elo", {"limit": 100, "min_runs": self.config.min_character_runs},
                                deadline=deadline)
        record.update({"leaderboard_computed_at": board.data.get("computed_at"),
                       "leaderboard_fetch": board.provenance()})
        return select_cohort(board.data, character, size=self.config.cohort_size,
                             min_runs=self.config.min_character_runs)

    def _run_scope(self, run):
        character = _character(run.get("character"))
        ascension = self.config.ascension if self.config.ascension is not None else run.get("ascension")
        version = self.game_build if self.config.match_version else None
        return character, ascension, version

    def _catalogs(self, deadline):
        acts = self.client.get("/api/acts", {"lang": self.config.lang}, deadline=deadline).data.get("items")
        if not isinstance(acts, list):
            raise StatsUnavailable("invalid_act_catalog")
        encounters = self.client.get("/api/encounters", {"lang": self.config.lang}, deadline=deadline).data.get("items")
        if not isinstance(encounters, list):
            raise StatsUnavailable("invalid_encounter_catalog")
        acts = [a for a in acts if isinstance(a, dict) and _id(a.get("id")) and isinstance(a.get("bosses"), list)]
        return acts, {_id(e["id"]): e for e in encounters if isinstance(e, dict) and _id(e.get("id"))}

    def resolve_act(self, act, boss_names, *, deadline, boss_ids=()):
        """Act ID from visible boss encounter IDs, else boss names (exact, case-insensitive),
        else a unique variant for the 1-based act number. Returns (act_id, method, candidates)."""
        acts, encounters = self._catalogs(deadline)
        ids = {_id(b) for b in boss_ids if _id(b)}
        by_id = [a for a in acts if ids & {_id(b) for b in a["bosses"]}]
        if len(by_id) == 1:
            return _id(by_id[0]["id"]), "visible_boss_id", [_id(by_id[0]["id"])]
        wanted = {_title(n) for n in boss_names if isinstance(n, str) and n.strip()}
        if wanted:
            names = {}
            for eid, e in encounters.items():
                monsters = [m.get("name") for m in e.get("monsters") or [] if isinstance(m, dict)]
                names[eid] = {_title(x) for x in [e.get("name"), *monsters] if isinstance(x, str)}
            matched = [a for a in acts if any(wanted & names.get(_id(b), set()) for b in a["bosses"])]
            if len(matched) == 1:
                return _id(matched[0]["id"]), "visible_boss_name", [_id(a["id"]) for a in matched]
            if len(matched) > 1:
                return None, "ambiguous_boss_name", [_id(a["id"]) for a in matched]
        same_index = [a for a in acts if type(act) is int and a.get("index") == act - 1]
        if len(same_index) == 1:
            return _id(same_index[0]["id"]), "single_variant_for_act_number", [_id(same_index[0]["id"])]
        return None, "act_variant_unresolved", [_id(a["id"]) for a in same_index]

    def _combat_catalogs(self, deadline):
        out = []
        for path in ("/api/cards", "/api/monsters", "/api/powers"):
            items = self.client.get(path, {"lang": self.config.lang}, deadline=deadline).data.get("items")
            if not isinstance(items, list):
                raise StatsUnavailable("invalid_catalog:" + path)
            out.append({_id(x["id"]): x for x in items if isinstance(x, dict) and _id(x.get("id"))})
        return out

    def _readiness_for_view(self, view, act_id, *, deadline, offered=None):
        """Elite threats of this act and later acts vs. the observed deck (Spire Codex data only)."""
        from .elite_readiness import builds_summary, deck_coverage, gaps, offered_fit, pool_gap_share, threat_profile
        run, player = view.facts.get("run") or {}, view.facts.get("player") or {}
        character, ascension, _ = self._run_scope(run)
        result = {"tool": "get_elite_readiness", "source": API_ROOT, "status": "unavailable",
                  "label": "SPIRE CODEX ELITE FACTS VS. YOUR OBSERVED DECK; rule-based gaps, not instructions",
                  "rules": {"aoe": "multiple enemies or summons, and no AllEnemies attack in the deck",
                            "block": f"largest attack turn >= {self.config.elite_block_attack_pct:g}% of max HP or the "
                                     "elite gains Strength, and no non-starter block card in the deck",
                            "damage_or_scaling": "no non-starter attack and no Power card in the deck"},
                  "limitations": ["Card coverage uses catalog fields (target, damage, block, type), not rules text; "
                                  "relics, potions, enchantments and card synergies are not counted.",
                                  "Which elite appears behind an elite node is unknown; the act's whole pool is shown.",
                                  "Build death rates are community correlations, not proof that a card beats an elite."]}
        try:
            act_id = act_id or self._act_ids.get((character, run.get("act")))
            if act_id is None:
                act_id, method, _ = self.resolve_act(run.get("act"), [], deadline=deadline)
                if act_id is None:
                    result["reason"] = method
                    return result
            acts, encounters = self._catalogs(deadline)
            cards, monsters, powers = self._combat_catalogs(deadline)
        except StatsUnavailable as exc:
            result["reason"] = str(exc)
            return result
        current = next((a for a in acts if _id(a["id"]) == act_id), None)
        if current is None or type(ascension) is not int:
            result["reason"] = "act_or_ascension_unknown"
            return result
        deck = view.facts.get("deck")
        coverage = deck_coverage(deck, cards) if isinstance(deck, list) else None
        deck_ids = {c for c in (_id(x.get("id")) for x in deck) if c} if isinstance(deck, list) else None
        later = sorted((a for a in acts if type(a.get("index")) is int and type(current.get("index")) is int
                        and a["index"] > current["index"]), key=lambda a: a["index"])
        pools = []
        for act, relation in [(current, "current")] + [(a, "later") for a in later]:
            elites = []
            for eid in act.get("encounters") or []:
                enc = encounters.get(_id(eid))
                if not enc or str(enc.get("room_type", "")).lower() != "elite":
                    continue
                threat = threat_profile(enc, monsters, powers, ascension)
                threat["gaps"] = gaps(threat, coverage, max_hp=player.get("max_hp"),
                                      block_attack_pct=self.config.elite_block_attack_pct)
                try:
                    payload = self.client.get("/api/runs/encounter-builds", {"encounter": threat["encounter_id"],
                                              "lang": self.config.lang}, deadline=deadline).data
                    threat["builds"] = builds_summary(payload, character, deck_ids)
                except StatsUnavailable as exc:
                    threat["builds"] = {"status": "unavailable", "reason": str(exc)}
                elites.append(threat)
            pool = {"act_id": _id(act["id"]), "relation": relation, "elites": elites}
            pool["elite_gap_share"] = pool_gap_share(pool)
            pools.append(pool)
        result.update({"status": "available" if coverage is not None else "partial",
                       "deck_status": "observed" if coverage is not None else
                       "unknown: the permanent deck needs the patched mod (tools/patch_mod_preview.py); gaps not evaluated",
                       "deck_coverage": coverage, "pools": pools})
        if offered:
            result["offered_cards"] = offered_fit(offered, cards, pools)
        return result

    def _ancient_view(self, view, *, deadline):
        """Ancient offers vs. the observed deck and state, plus Spire Codex expert and community evidence."""
        from .ancient_advisor import (archetype_fit, deck_facts, expert_offer_stats, labels, relic_effects,
                                      state_comparison)
        from .route_profile import build_route_profile
        run, player = view.facts.get("run") or {}, view.facts.get("player") or {}
        details = view.facts.get("screen_details") or {}
        character, ascension, version = self._run_scope(run)
        ancient_id = _id(details.get("event_id"))
        support = {
            "tool": "get_ancient_offer_evidence", "schema_version": 1, "source": API_ROOT, "status": "unavailable",
            "label": "SPIRE CODEX ANCIENT EVIDENCE COMPARED WITH YOUR DECK AND STATE; not instructions or causal effects",
            "scope": {"character": character, "ascension": ascension, "version": version, "ancient_id": ancient_id,
                      "act": run.get("act")},
            "limitations": ["Effects are text-derived tags from the relic catalog; read each relic's full text.",
                            "Expert rows come from a bounded set of top players' runs; small cells are flagged sparse.",
                            "Outcome win rates are correlations: players in strong positions choose differently.",
                            "route_outlook re-projects the act map from each option's HP, max HP and gold; card, "
                            "relic and deck effects of the relic itself are not simulated."],
            "options": []}
        if not character or type(ascension) is not int:
            support["reason"] = "character_or_ascension_unknown"
            return support
        try:
            relics = {_id(r["id"]): r for r in self.client.get("/api/relics", {"lang": self.config.lang},
                      deadline=deadline).data.get("items") or [] if isinstance(r, dict) and _id(r.get("id"))}
            cards = self._combat_catalogs(deadline)[0]
        except StatsUnavailable as exc:
            support["reason"] = str(exc)
            return support
        by_name = {}
        for rid, r in relics.items():
            by_name.setdefault(_title(r.get("name")), set()).add(rid)
        try:
            pool = self.client.get("/api/ancient-pools/" + urllib.parse.quote(ancient_id or "", safe=""), {},
                                   deadline=deadline).data if ancient_id else {}
        except StatsUnavailable as exc:
            pool, support["ancient_pool_error"] = {}, str(exc)
        entries = {}
        for group in pool.get("pools") or []:
            for e in (group.get("relics") or []) if isinstance(group, dict) else []:
                if isinstance(e, dict) and _id(e.get("id")):
                    entries[_id(e["id"])] = {"pool": group.get("name"), "condition": e.get("condition")}
        support["ancient_pool"] = {"name": pool.get("name"), "selection": pool.get("selection"),
                                   "description": pool.get("description")}
        deck = view.facts.get("deck")
        deck_copies = _deck_ids(deck)
        deck_ids = set(deck_copies) if deck_copies is not None else None
        facts = deck_facts(deck, cards)
        support["deck_facts"] = facts if facts is not None else {
            "status": "unknown: the permanent deck needs the patched mod (tools/patch_mod_preview.py)"}
        options = []
        for c in view.candidates:
            if c["kind"] != "choose_event_option":
                continue
            d = c["details"]
            matches = by_name.get(_title(d.get("relic_name")), set()) if d.get("relic_name") else set()
            rid = next(iter(matches)) if len(matches) == 1 else None
            options.append({"action_id": c["action_id"], "title": d.get("title"), "relic_id": rid,
                            "match": "exact_name" if rid else ("no_relic" if not d.get("relic_name") else "unmatched")})
        offered = [o["relic_id"] for o in options if o["relic_id"]]
        # Expert marks for HP/gold comparisons: this act's route profile (all variants if unresolved).
        marks, runs_by_act, profiles, map_act = {}, {}, {}, None
        amap = details.get("map")
        if not amap:
            support["limitations"].append("No map on this screen (needs the patched mod's ancient-screen map); "
                                          "routes are not projected and the act variant may be unresolved.")
        cohort, scope = [], self._scope(character, ascension, version)
        try:
            cohort = self._cohort(character, {}, deadline=deadline)
            support["cohort"] = [m["username"] for m in cohort]
            support["history_index"] = self.history.sync(self.client, cohort, scope, deadline=deadline, clock=self.clock)
            rows = self.history.ancient_rows(cohort, scope)
            if amap:  # the act map is readable at the ancient: its boss names the act variant
                boss_rows = [b for b in amap.get("bosses") or [] if isinstance(b, dict)]
                map_act, method, _ = self.resolve_act(run.get("act"), [b.get("name") for b in boss_rows],
                                                      deadline=deadline, boss_ids=[b.get("id") for b in boss_rows])
                support["scope"].update({"act_id": map_act, "act_resolution": method})
                if map_act:
                    self._act_ids[(character, run.get("act"))] = map_act
            act_ids = [self._act_ids.get((character, run.get("act")))]
            if act_ids[0] is None:
                _, _, act_ids = self.resolve_act(run.get("act"), [], deadline=deadline)
            for act_id in act_ids:
                runs = self.history.route_rows(cohort, scope, act_id)
                runs_by_act[act_id] = runs
                if runs:
                    prof = build_route_profile(runs, act_id, min_samples=self.config.min_context_samples)
                    profiles[act_id] = prof
                    group = "wins" if not prof["samples"]["sparse"] else "all"
                    marks.setdefault("hp", {})[act_id] = {"group": group, "boss_arrival_hp_pct":
                                                          prof["hp_pct_checkpoints"][group]["boss_arrival"]}
                    marks.setdefault("gold", {})[act_id] = {"group": group, "shop_arrival_gold":
                                                            prof["shops"][group]["arrival_gold_per_visit"]}
        except StatsUnavailable as exc:
            rows, support["history_reason"] = [], str(exc)
        except (OSError, sqlite3.Error) as exc:
            rows, support["history_reason"] = [], "history_index_" + type(exc).__name__
        hp_pct = player.get("hp") / player["max_hp"] * 100 if player.get("hp") is not None and player.get("max_hp") else None
        experts = expert_offer_stats(rows, offered, hp_pct=hp_pct, act=run.get("act"), floor=run.get("floor"),
                                     deck_ids=deck_copies,
                                     similarity_min=self.config.deck_similarity_min,
                                     min_samples=self.config.min_context_samples) if offered else {}
        community, archetypes = {}, []
        try:
            table = self.client.get("/api/runs/metrics/relics", {"bracket": self.config.encounter_stats_bracket,
                                    "character": character}, deadline=deadline).data
            keep = ("win_rate", "win_rate_ci", "lift", "lift_n", "pick_rate", "pick_rate_by_act", "picks", "offered")
            community = {_id(r.get("id")): {k: r[k] for k in keep if k in r} for r in table.get("rows") or []
                         if isinstance(r, dict) and _id(r.get("id")) in offered}
            support["community_scope"] = {"bracket": self.config.encounter_stats_bracket, "character": character,
                                          "baseline_win_rate": table.get("baseline_win_rate"),
                                          "note": "all patches; lift = floor-adjusted win-rate gain in points"}
        except StatsUnavailable as exc:
            support["community_reason"] = str(exc)
        try:
            data = self.client.get("/api/runs/archetypes", {"lang": self.config.lang}, deadline=deadline).data
            archetypes = next((v for k, v in (data.get("characters") or {}).items() if _character(k) == character), [])
        except StatsUnavailable as exc:
            support["archetype_reason"] = str(exc)
        for o in options:
            rid = o["relic_id"]
            if rid is None:
                continue
            relic = relics[rid]
            o.update({"name": relic.get("name"), "rarity": relic.get("rarity"), "description": relic.get("description"),
                      "pool_entry": entries.get(rid), "effects": relic_effects(relic)})
            o["state_comparison"] = state_comparison(o["effects"], facts, hp=player.get("hp"), max_hp=player.get("max_hp"),
                                                     gold=player.get("gold"), expert_marks=marks)
            o["experts"] = experts.get(rid)
            o["community"] = community.get(rid)
            o["archetype_fit"] = archetype_fit(rid, archetypes, deck_ids)
        if amap and map_act:
            self._ancient_map_outlook(support, options, amap, map_act, runs_by_act.get(map_act) or [],
                                      profiles.get(map_act), view, character, version, deadline=deadline)
        labels([o for o in options if o["relic_id"]])
        support["options"] = options
        if self.config.elite_readiness_enabled:
            ready = self._readiness_for_view(view, None, deadline=deadline)
            if ready.get("reason"):
                support["elite_gaps_reason"] = ready["reason"]
            support["elite_gaps_by_act"] = {p["act_id"]: sorted({f"{g['need']} ({e['name']})" for e in p["elites"]
                                                                 for g in e.get("gaps") or []})
                                            for p in ready.get("pools") or []}
        matched = sum(o["relic_id"] is not None for o in options)
        support["status"] = "available" if offered and matched == len([o for o in options if o["match"] != "no_relic"]) \
            else "partial" if offered else "unavailable"
        if not offered:
            support["reason"] = "no_matched_relic_options"
        return support

    def _ancient_map_outlook(self, support, options, amap, act_id, runs, profile, view, character, version, *, deadline):
        """Project this act's map from the current state and from each option's resulting HP/max HP/gold."""
        from .route_paths import plan_paths
        player = view.facts.get("player") or {}
        try:
            act_entry, encounters, weak = self._act_context(act_id, deadline)
            risk = self._risk_model(runs, act_id, act_entry, encounters, weak, character, player.get("max_hp"),
                                    version, deadline=deadline)
        except StatsUnavailable as exc:
            support["map_outlook"] = {"status": "unavailable", "reason": str(exc)}
            return
        pool = None
        if self.config.elite_readiness_enabled:
            ready = self._readiness_for_view(view, act_id, deadline=deadline)
            pool = next((p for p in ready.get("pools") or [] if p["relation"] == "current"), None)

        def project(hp, max_hp, gold):
            return plan_paths(amap, hp=hp, max_hp=max_hp, gold=gold, risk=risk, profile=profile, candidates=0,
                              max_routes=self.config.route_max_routes, elite_pool=pool)
        base = project(player.get("hp"), player.get("max_hp"), player.get("gold"))
        bosses = [{"id": b.get("id"), "name": b.get("name")} for b in amap.get("bosses") or [] if isinstance(b, dict)]
        support["map_outlook"] = {
            "status": base.get("status"), "reason": base.get("reason"), "act_id": act_id, "bosses": bosses,
            "first_nodes": [{"node": o["node"], "coords": o["coords"], "distinct_routes": o["distinct_routes"],
                             "max_elites": o["max_elites"],
                             "lowest_death_risk_before_boss_pct": o["lowest_death_risk_before_boss_pct"],
                             "fewest_fights_before_next_rest": o["fewest_fights_before_next_rest"]}
                            for o in base.get("by_next_option") or []],
            "distinct_routes": base.get("distinct_routes"), "outlook": base.get("outlook"),
            "route_ranges": base.get("route_ranges"),
            "elite_gaps": base.get("current_act_elite_gaps"), "elite_gap_share": base.get("current_act_elite_gap_share"),
            "hp_risk_classes": {k: {f: c[f] for f in ("mean_pct", "p90_pct", "death_rate_pct", "n", "source")}
                                for k, c in risk["classes"].items()},
            "basis": "current HP/max HP/gold; pooled fight damage for this character and act variant"}
        for o in options:
            if not o.get("relic_id"):
                continue
            sc = o.get("state_comparison") or {}
            hp = (sc.get("hp") or {}).get("hp_after", player.get("hp"))
            max_hp = (sc.get("hp") or {}).get("max_hp_after", player.get("max_hp"))
            gold = (sc.get("gold") or {}).get("gold_after", player.get("gold"))
            if "hp" in sc or "gold" in sc:
                after = project(hp, max_hp, gold)
                o["route_outlook"] = {"start": {"hp": hp, "max_hp": max_hp, "gold": gold}, **(after.get("outlook") or {})}
            else:
                o["route_outlook"] = {"start": {"hp": hp, "max_hp": max_hp, "gold": gold}, **(base.get("outlook") or {}),
                                      "same_as_map_outlook": True,
                                      "note": "no HP/max HP/gold change detected in the relic text"}

    def _encounter_fallback(self, act_entry, encounters, character, max_hp, version, *, deadline):
        """Pooled /api/runs/encounter-stats means and death rates per node class (no percentiles)."""
        from .route_risk import CLASSES
        ids = [_id(e) for e in act_entry.get("encounters") or [] if _id(e)]
        if not ids or not max_hp or type(act_entry.get("index")) is not int:
            return None, {"status": "unavailable", "reason": "act_encounters_or_max_hp_unknown"}
        params = {"act": act_entry["index"] + 1, "room_type": "monster,elite,boss", "encounter": ",".join(ids[:50]),
                  "bracket": self.config.encounter_stats_bracket, "build_id": version, "limit": 200}
        try:
            response = self.client.get("/api/runs/encounter-stats", params, deadline=deadline)
        except StatsUnavailable as exc:
            return None, {"status": "unavailable", "reason": str(exc)}
        rows = response.data.get("encounters")
        if not isinstance(rows, list):
            return None, {"status": "unavailable", "reason": "invalid_encounter_stats"}
        acc = {c: [0, 0.0, 0] for c in CLASSES}
        for row in rows:
            eid = _id(row.get("encounter_id")) if isinstance(row, dict) else None
            room = str(row.get("room_type", "")).lower() if eid else ""
            mine = next((c for c in row.get("characters") or [] if isinstance(c, dict)
                         and _character(c.get("character")) == character), None) if eid else None
            if not mine or _count(mine.get("total")) in (None, 0) or type(mine.get("avg_damage")) not in (int, float):
                continue
            cls = {"elite": "elite", "boss": "boss"}.get(room) or (
                ("early_hallway" if (encounters.get(eid) or {}).get("is_weak") else "later_hallway")
                if room == "monster" else None)
            if cls:
                a = acc[cls]
                a[0] += mine["total"]
                a[1] += mine["avg_damage"] * mine["total"]
                a[2] += _count(mine.get("fatal")) or 0
        result = {c: {"n": n, "mean_pct": round(dmg / n / max_hp * 100, 1), "death_rate_pct": round(f / n * 100, 2)}
                  for c, (n, dmg, f) in acc.items() if n}
        return result, {"status": "available", "bracket": self.config.encounter_stats_bracket, "build_id": version,
                        "percent_basis": f"absolute average damage / current max HP {max_hp} (approximate)",
                        "split": "early = encounters flagged is_weak in the catalog", **response.provenance()}

    def _route_view(self, view, *, deadline, route_state=None):
        from .route_profile import build_route_profile
        run = view.facts.get("run") or {}
        character, ascension, version = self._run_scope(run)
        boss_rows = [b for b in (view.facts.get("screen_details") or {}).get("bosses") or [] if isinstance(b, dict)]
        bosses = [b.get("name") for b in boss_rows]
        support = {
            "tool": "get_expert_route_profile", "schema_version": 3, "source": API_ROOT,
            "label": "OBSERVATIONAL ROUTES OF SELECTED HUMAN PLAYERS; descriptive evidence, not a route "
                     "recommendation, instruction or causal effect",
            "status": "unavailable",
            "scope": {"character": character, "ascension": ascension, "version": version, "players": 1,
                      "game_mode": "standard", "act": run.get("act"), "act_id": None, "act_resolution": None},
            "cohort": {"selection": "character_elo", "min_character_rated_runs": self.config.min_character_runs,
                       "requested_players": self.config.cohort_size, "members": []},
            "definitions": {
                "wins_vs_all": "'all' = every non-abandoned indexed run in scope that entered this act; "
                               "'wins' = the subset that won the whole run.",
                "act_floor": "1-based position in this act's recorded history (may include a non-choice "
                             "opening node such as an ancient).",
                "node_types": "Map icon chosen (map_point_type); 'unknown' is a ? node, see unknown_node_resolved_as.",
                "complete_act_basis": "node_counts_per_act, first_elite and first_shop_act_floor use only runs "
                                      "that reached this act's boss node.",
                "arrival": "HP%/gold on arrival = values recorded after the previous floor.",
                "hallway_fights_before": "Rooms that resolved to a normal monster fight, including ? nodes."},
            "limitations": ["Bounded corpus of the selected players' newest submitted runs; not exhaustive or unbiased.",
                             "Run files do not include the map, so counts mix choice with what the map offered.",
                             "Winners' routes partly reflect stronger decks and HP; they are not causal advice.",
                             "No deck, relic, potion or current-HP conditioning; compare with the current state."],
        }
        if version is None:
            support["limitations"].append("Runs span all game versions; no version filter was applied.")
        if not character or type(ascension) is not int or not 0 <= ascension <= 10:
            support["reason"] = "character_or_ascension_unknown"
            return support
        try:
            act_id, method, candidates = self.resolve_act(run.get("act"), bosses, deadline=deadline,
                                                          boss_ids=[b.get("id") for b in boss_rows])
            support["scope"].update({"act_id": act_id, "act_resolution": method, "act_candidates": candidates})
            if act_id is None:
                support["reason"] = method
                return support
            self._act_ids[(character, run.get("act"))] = act_id
            cohort = self._cohort(character, support["cohort"], deadline=deadline)
        except StatsUnavailable as exc:
            support["reason"] = str(exc)
            return support
        support["cohort"]["members"] = [dict(m) for m in cohort]
        if not cohort:
            support["reason"] = "no_eligible_character_players"
            return support
        scope = self._scope(character, ascension, version)
        try:
            support["history_index"] = self.history.sync(self.client, cohort, scope, deadline=deadline, clock=self.clock)
            runs = self.history.route_rows(cohort, scope, act_id)
        except (OSError, sqlite3.Error) as exc:
            support["reason"] = "history_index_" + type(exc).__name__
            return support
        if runs:
            support["profile"] = build_route_profile(runs, act_id, min_samples=self.config.min_context_samples)
            support["status"] = "partial" if support["profile"]["samples"]["sparse"] else "available"
        else:
            support["reason"] = "no_indexed_runs_for_act"
        if self.config.route_planning_enabled:
            self._route_planning(view, support, runs, act_id, character, version,
                                 route_state=route_state, deadline=deadline)
        return support

    def _risk_model(self, runs, act_id, act_entry, encounters, weak, character, max_hp, version, *, deadline):
        from .route_risk import build_risk_model
        min_n = self.config.min_context_samples
        risk = build_risk_model(runs, act_id, weak_ids=weak, min_samples=min_n)
        if any(c["n"] < min_n for c in risk["classes"].values()):
            fallback, record = self._encounter_fallback(act_entry, encounters, character, max_hp,
                                                        version, deadline=deadline)
            risk = build_risk_model(runs, act_id, weak_ids=weak, min_samples=min_n, fallback=fallback)
            risk["encounter_stats_fallback"] = record
        risk["basis"] = ("% of max HP per node, from indexed runs of the cohort (n >= "
                         f"{min_n} for p90); encounter_stats fallback has means and death rates only")
        return risk

    def _act_context(self, act_id, deadline):
        acts, encounters = self._catalogs(deadline)
        act_entry = next((a for a in acts if _id(a["id"]) == act_id), {})
        weak = {_id(e) for e in act_entry.get("encounters") or []
                if _id(e) and (encounters.get(_id(e)) or {}).get("is_weak") is True}
        return act_entry, encounters, weak

    def _route_planning(self, view, support, runs, act_id, character, version, *, route_state, deadline):
        from .route_paths import plan_paths, plan_status
        player = view.facts.get("player") or {}
        details = view.facts.get("screen_details") or {}
        try:
            act_entry, encounters, weak = self._act_context(act_id, deadline)
        except StatsUnavailable as exc:
            support["paths"] = {"status": "unavailable", "reason": str(exc)}
            return
        risk = self._risk_model(runs, act_id, act_entry, encounters, weak, character, player.get("max_hp"),
                                version, deadline=deadline)
        from .route_risk import public_risk
        support["hp_risk"] = public_risk(risk)
        plan = (route_state or {}).get("plan")
        here = tuple(c for c in [(details.get("current_position") or {}).get("col"),
                                 (details.get("current_position") or {}).get("row")])
        coords = [tuple(c) for c in (plan or {}).get("coords") or []]
        previous = ([list(c) for c in coords[coords.index(here) + 1:]] if plan and plan["act"] == view.facts["run"].get("act")
                    and here in coords else None)
        pool = None
        if self.config.elite_readiness_enabled:
            support["elite_readiness"] = self._readiness_for_view(view, act_id, deadline=deadline)
            pool = next((p for p in support["elite_readiness"].get("pools") or [] if p["relation"] == "current"), None)
        support["paths"] = plan_paths(details, hp=player.get("hp"), max_hp=player.get("max_hp"), gold=player.get("gold"),
                                      risk=risk, profile=support.get("profile"),
                                      candidates=self.config.route_candidates, max_routes=self.config.route_max_routes,
                                      previous_nodes=previous, elite_pool=pool,
                                      outcomes=(route_state or {}).get("outcomes"))
        support["paths"]["limitations"] = [
            "Projections assume current max HP, pooled class damage and independent fights; deck, relics, potions "
            "and event effects beyond recorded HP loss are not modeled.",
            "death_risk_* is the share of replays in which sampled fight losses (times agent_loss_scale) used up "
            "the HP: a rough estimate from other players' fights, not a calibrated probability. p10_* is the 10th "
            "percentile across replays; HP below 0 is kept and means the path fell short by that much.",
            "The loss samples come from strong players whose fights usually cost little; agent_loss_scale corrects "
            "only for this run's own finished fights and starts at 1.",
            "Simulated rest choices follow the experts' HEAL share in that HP band; heal size is measured, not assumed.",
            "selected_for labels say what a path is best at; there is no combined score. Python never chooses."]
        if route_state is not None:
            support["route_plan_status"] = plan_status(
                plan, act=view.facts["run"].get("act"), details=details, hp=player.get("hp"),
                max_hp=player.get("max_hp"), gold=player.get("gold"),
                relics=[str(r.get("id") or r.get("name")) for r in view.facts.get("relics") or []],
                potions=[str(p.get("id") or p.get("name")) for p in view.facts.get("potions") or []],
                outcomes=route_state.get("outcomes") or [], risk=risk,
                hp_tolerance=self.config.replan_hp_tolerance_pct, gold_tolerance=self.config.replan_gold_tolerance,
                elite_pool=pool, elite_steps=self.config.elite_trigger_steps)
        if support["status"] == "unavailable" and support["paths"].get("status") == "available":
            support["status"] = "partial"

    def bulk_sync(self, character, ascension, *, max_new_runs, max_seconds, progress=None):
        """Fill the local run corpus before play (the per-decision sync is deliberately small)."""
        character = _character(character)
        version = self.game_build if self.config.match_version else None
        stop = self.clock() + max_seconds
        cohort = self._cohort(character, {}, deadline=stop)
        if not cohort:
            raise StatsUnavailable("no_eligible_character_players")
        scope, total, idle = self._scope(character, ascension, version), 0, 0
        while total < max_new_runs and self.clock() < stop and idle < 2:
            result = self.history.sync(self.client, cohort, scope, deadline=stop, clock=self.clock)
            total += result["new_runs"]
            paging = any(p["status"] == "next_page_pending" for p in result["players"])
            idle = 0 if result["new_runs"] or paging else idle + 1
            if progress:
                progress(total, result)
        return {"cohort": [m["username"] for m in cohort], "new_runs": total, "scope": scope}

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
        character, ascension, version = self._run_scope(run)
        support = {
            "tool": "get_expert_decision_stats", "schema_version": 1, "source": API_ROOT,
            "label": "OBSERVATIONAL HUMAN CHOICES; advisory evidence, not instructions or causal effects",
            "status": "unavailable",
            "scope": self._scope(character, ascension, version),
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
            cohort = self._cohort(character, support["cohort"], deadline=deadline)
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
        event_options, event_loose = {}, {}
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
                        event_loose.setdefault(_loose_title(option["title"]), set()).add(_id(option.get("id")))
            except StatsUnavailable as exc:
                support["event_catalog_error"] = str(exc)
                event_options, event_loose = {}, {}
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
                option_id, how = match_event_option(details, event_options, event_loose)
                key, table, metric = (event_id, option_id), "events", "choice_share"
                row.update({"event_id": event_id, "option_id": option_id, "option_match": how})
                if option_id is None:  # kept in the trace so the missing identifier can be fixed
                    row["unmatched_detail"] = {"observed_title": str(details.get("title"))[:80],
                                               "catalog_option_titles": len(event_options),
                                               "event_catalog_error": support.get("event_catalog_error")}
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
                top, other = _top_contributors(observations)
                row.update({"metric": metric, "numerator": numerator if observations else None,
                            "denominator": denominator if observations else None,
                            "rate_pct": round(numerator / denominator * 100, 2) if denominator else None,
                            "contributing_players": len(observations), "contributors": top})
                if other:
                    row["other_contributors"] = other
                if observations:
                    row.update({"status": "available", "largest_player_sample_share_pct":
                                round(max(o["denominator"] for o in observations) / denominator * 100, 2)})
                    _withhold(row, self.config.min_context_samples)
                else:
                    row["reason"] = "no_recorded_observations"
            support["options"].append(row)
        available = sum(row["status"] == "available" for row in support["options"])
        support["status"] = ("available" if available == len(candidates) and len(profiles) == len(cohort)
                             else "partial" if available else "unavailable")
        return support
