"""Strategic planner contracts: snapshot (input), assessment (output), result.

Everything here is explicit, hand-written validation over plain JSON-compatible
Python values. ``None`` always means *unknown*; an empty list inside a supplied
object means *known to be empty*.

Fact shapes (version 0). Each top-level key is required; its value is either
``None`` (unknown) or an object with exactly the keys shown. ``?`` = may be null.

    run:             {character?: str, difficulty?: str, act?: int>=1, floor?: int>=0,
                      encounter_type?: str}
    resources:       {hp?: int>=0, max_hp?: int>=1, gold?: int>=0, max_energy?: int>=0}
    deck:            {cards: [card]}            # COMPLETE permanent deck, else deck = None
        card:        {id: str, name: str, count: int>=1, upgraded?: bool, cost?: str,
                      type?: str, description?: str}
                      ``id`` is a caller-supplied entry id, unique in the list
                      (e.g. distinguish upgraded copies: "STRIKE_IRONCLAD+").
    relics:          {items: [{id: str, name: str, description?: str, counter?: int}]}
    potions:         {slots?: int>=0, items: [{slot: int>=0, id: str, name: str,
                      description?: str}]}      # ``slot`` unique; len(items) <= slots
    opportunities:   {items: [{id: str, kind: str, description: str, details?: str}]}
                      kind in OPPORTUNITY_KINDS; only currently player-visible choices
    recent_outcomes: {items: [{id: str, encounter_type?: str, result?: str, hp_lost?: int>=0,
                      turns?: int>=0, notes?: str}]}   # result in OUTCOME_RESULTS

Optional top-level keys (version 1), omitted or ``None`` when unknown:

    route:           {act?: int>=1, boss?: str, plan?: str, change_course_if?: str,
                      plan_set_at_floor?: int>=0, remaining_nodes?: [str], next_rest_in?: int>=1,
                      next_elite_in?: int>=1, next_shop_in?: int>=1, boss_in?: int>=1}
                      boss observed on the act map; the rest from the stored route plan
    glossary:        {items: [{term: str, definition: str}]}   # reference rules text, not state
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

SNAPSHOT_SCHEMA_VERSION = "sts2-strategic-snapshot-1"
ASSESSMENT_SCHEMA_VERSION = "sts2-strategic-assessment-1"

MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_MODEL_TEXT_BYTES = 16 * 1024
MAX_DIAGNOSTIC_CHARS = 500

DATA_SOURCES = ("synthetic", "manual", "observed")
CHANGE_REASONS = (
    "initial", "combat_finished", "deck_changed", "relic_changed", "potion_changed",
    "resources_changed", "opportunities_changed", "act_changed", "corrected_facts",
)
MAX_CHANGE_REASONS = 9
OPPORTUNITY_KINDS = ("card_reward", "relic_reward", "potion_reward", "gold_reward", "shop_item",
                     "card_removal", "rest_site", "route", "event", "encounter", "other")
OUTCOME_RESULTS = ("victory", "defeat", "fled", "unknown")
FACT_KEYS = ("run", "resources", "deck", "relics", "potions", "opportunities", "recent_outcomes")
OPTIONAL_FACT_KEYS = ("route", "glossary")
ROUTE_KEYS = ("act", "boss", "plan", "change_course_if", "plan_set_at_floor", "remaining_nodes",
              "next_rest_in", "next_elite_in", "next_shop_in", "boss_in")
PLAN_LISTS = {"deck_needs": 160, "avoid": 160, "removal_targets": 80, "upgrade_targets": 80}   # key -> item chars
PLAN_TEXTS = {"skip_rule": 240, "boss_prep": 240}
MAX_PLAN_ITEMS = 4

DIMENSIONS = ("immediate_threats", "damage", "defense", "scaling", "draw_consistency",
              "potion_needs", "upcoming_opportunities")
CONFIDENCE = ("low", "medium", "high")

_SHORT, _NAME, _TEXT = 80, 120, 1000


class InputError(ValueError):
    """Snapshot facts or identity violate the input contract."""


class OutputError(ValueError):
    """Model output violates the assessment contract."""


# ---------------------------------------------------------------------------
# small validators
# ---------------------------------------------------------------------------

def _fail(err, path: str, msg: str):
    raise err(f"{path or '/'}: {msg}")


def _obj(v, path, keys, err=InputError) -> dict:
    if not isinstance(v, dict):
        _fail(err, path, f"expected object, got {type(v).__name__}")
    for k in v:
        if not isinstance(k, str):
            _fail(err, path, "object keys must be strings")
    extra = set(v) - set(keys)
    missing = set(keys) - set(v)
    if extra:
        _fail(err, path, f"unknown keys {sorted(extra)}")
    if missing:
        _fail(err, path, f"missing keys {sorted(missing)}")
    return v


def _int(v, path, *, lo=None, nullable=True, err=InputError):
    if v is None and nullable:
        return
    if isinstance(v, bool) or not isinstance(v, int):
        _fail(err, path, f"expected integer{' or null' if nullable else ''}, got {type(v).__name__}")
    if lo is not None and v < lo:
        _fail(err, path, f"must be >= {lo}")


def _str(v, path, *, maxlen, nullable=True, nonempty=False, err=InputError):
    if v is None and nullable:
        return
    if not isinstance(v, str):
        _fail(err, path, f"expected string{' or null' if nullable else ''}, got {type(v).__name__}")
    if len(v) > maxlen:
        _fail(err, path, f"longer than {maxlen} characters")
    if nonempty and not v.strip():
        _fail(err, path, "must not be empty")


def _bool(v, path, *, nullable=True):
    if v is None and nullable:
        return
    if not isinstance(v, bool):
        _fail(InputError, path, f"expected boolean, got {type(v).__name__}")


def _enum(v, path, allowed, *, nullable=True, err=InputError):
    if v is None and nullable:
        return
    if v not in allowed:
        _fail(err, path, f"must be one of {list(allowed)}")


def _list(v, path, *, maxlen, err=InputError) -> list:
    if not isinstance(v, list):
        _fail(err, path, f"expected array, got {type(v).__name__}")
    if len(v) > maxlen:
        _fail(err, path, f"more than {maxlen} items")
    return v


def _unique(items, key, path):
    seen = set()
    for i, it in enumerate(items):
        if it[key] in seen:
            _fail(InputError, f"{path}/{i}/{key}", f"duplicate {key} {it[key]!r}")
        seen.add(it[key])


def _check_json_values(v, path):
    """Reject non-JSON types and nonfinite numbers anywhere in the facts."""
    if v is None or isinstance(v, (bool, str, int)):
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            _fail(InputError, path, "nonfinite number")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                _fail(InputError, path, "object keys must be strings")
            _check_json_values(x, f"{path}/{escape_pointer_token(k)}")
        return
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check_json_values(x, f"{path}/{i}")
        return
    _fail(InputError, path, f"unsupported type {type(v).__name__}")


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------

def validate_facts(facts: Any) -> None:
    _check_json_values(facts, "")
    _obj(facts, "", FACT_KEYS + tuple(k for k in OPTIONAL_FACT_KEYS if isinstance(facts, dict) and k in facts))

    route = facts.get("route")
    if route is not None:
        p = "/route"
        _obj(route, p, ROUTE_KEYS)
        _int(route["act"], p + "/act", lo=1)
        _str(route["boss"], p + "/boss", maxlen=240)
        _str(route["plan"], p + "/plan", maxlen=300)
        _str(route["change_course_if"], p + "/change_course_if", maxlen=300)
        _int(route["plan_set_at_floor"], p + "/plan_set_at_floor", lo=0)
        if route["remaining_nodes"] is not None:
            for i, node in enumerate(_list(route["remaining_nodes"], p + "/remaining_nodes", maxlen=40)):
                _str(node, f"{p}/remaining_nodes/{i}", maxlen=40, nullable=False, nonempty=True)
        for key in ("next_rest_in", "next_elite_in", "next_shop_in", "boss_in"):
            _int(route[key], f"{p}/{key}", lo=1)

    glossary = facts.get("glossary")
    if glossary is not None:
        p = "/glossary"
        _obj(glossary, p, ("items",))
        for i, x in enumerate(_list(glossary["items"], p + "/items", maxlen=40)):
            q = f"{p}/items/{i}"
            _obj(x, q, ("term", "definition"))
            _str(x["term"], q + "/term", maxlen=_SHORT, nullable=False, nonempty=True)
            _str(x["definition"], q + "/definition", maxlen=400, nullable=False, nonempty=True)

    run = facts["run"]
    if run is not None:
        p = "/run"
        _obj(run, p, ("character", "difficulty", "act", "floor", "encounter_type"))
        _str(run["character"], p + "/character", maxlen=_SHORT)
        _str(run["difficulty"], p + "/difficulty", maxlen=_SHORT)
        _int(run["act"], p + "/act", lo=1)
        _int(run["floor"], p + "/floor", lo=0)
        _str(run["encounter_type"], p + "/encounter_type", maxlen=_SHORT)

    res = facts["resources"]
    if res is not None:
        p = "/resources"
        _obj(res, p, ("hp", "max_hp", "gold", "max_energy") +
             (("last_observed_max_energy",) if isinstance(res, dict) and "last_observed_max_energy" in res else ()))
        _int(res["hp"], p + "/hp", lo=0)
        _int(res["max_hp"], p + "/max_hp", lo=1)
        _int(res["gold"], p + "/gold", lo=0)
        _int(res["max_energy"], p + "/max_energy", lo=0)
        if "last_observed_max_energy" in res:
            _int(res["last_observed_max_energy"], p + "/last_observed_max_energy", lo=0)
        if res["hp"] is not None and res["max_hp"] is not None and res["hp"] > res["max_hp"]:
            _fail(InputError, p + "/hp", "exceeds max_hp")

    deck = facts["deck"]
    if deck is not None:
        p = "/deck"
        _obj(deck, p, ("cards",))
        cards = _list(deck["cards"], p + "/cards", maxlen=200)
        for i, c in enumerate(cards):
            q = f"{p}/cards/{i}"
            _obj(c, q, ("id", "name", "count", "upgraded", "cost", "type", "description"))
            _str(c["id"], q + "/id", maxlen=_SHORT, nullable=False, nonempty=True)
            _str(c["name"], q + "/name", maxlen=_NAME, nullable=False, nonempty=True)
            _int(c["count"], q + "/count", lo=1, nullable=False)
            _bool(c["upgraded"], q + "/upgraded")
            _str(c["cost"], q + "/cost", maxlen=8)
            _str(c["type"], q + "/type", maxlen=_SHORT)
            _str(c["description"], q + "/description", maxlen=_TEXT)
        _unique(cards, "id", p + "/cards")

    rel = facts["relics"]
    if rel is not None:
        p = "/relics"
        _obj(rel, p, ("items",))
        items = _list(rel["items"], p + "/items", maxlen=100)
        for i, r in enumerate(items):
            q = f"{p}/items/{i}"
            _obj(r, q, ("id", "name", "description", "counter"))
            _str(r["id"], q + "/id", maxlen=_SHORT, nullable=False, nonempty=True)
            _str(r["name"], q + "/name", maxlen=_NAME, nullable=False, nonempty=True)
            _str(r["description"], q + "/description", maxlen=_TEXT)
            _int(r["counter"], q + "/counter")
        _unique(items, "id", p + "/items")

    pot = facts["potions"]
    if pot is not None:
        p = "/potions"
        _obj(pot, p, ("slots", "items"))
        _int(pot["slots"], p + "/slots", lo=0)
        items = _list(pot["items"], p + "/items", maxlen=20)
        for i, x in enumerate(items):
            q = f"{p}/items/{i}"
            _obj(x, q, ("slot", "id", "name", "description"))
            _int(x["slot"], q + "/slot", lo=0, nullable=False)
            _str(x["id"], q + "/id", maxlen=_SHORT, nullable=False, nonempty=True)
            _str(x["name"], q + "/name", maxlen=_NAME, nullable=False, nonempty=True)
            _str(x["description"], q + "/description", maxlen=_TEXT)
        _unique(items, "slot", p + "/items")
        if pot["slots"] is not None:
            if len(items) > pot["slots"]:
                _fail(InputError, p + "/items", "more potions than slots")
            for i, x in enumerate(items):
                if x["slot"] >= pot["slots"]:
                    _fail(InputError, f"{p}/items/{i}/slot", "slot index outside capacity")

    opp = facts["opportunities"]
    if opp is not None:
        p = "/opportunities"
        _obj(opp, p, ("items",))
        items = _list(opp["items"], p + "/items", maxlen=40)
        for i, x in enumerate(items):
            q = f"{p}/items/{i}"
            _obj(x, q, ("id", "kind", "description", "details"))
            _str(x["id"], q + "/id", maxlen=_SHORT, nullable=False, nonempty=True)
            _enum(x["kind"], q + "/kind", OPPORTUNITY_KINDS, nullable=False)
            _str(x["description"], q + "/description", maxlen=_TEXT, nullable=False, nonempty=True)
            _str(x["details"], q + "/details", maxlen=_TEXT)
        _unique(items, "id", p + "/items")

    out = facts["recent_outcomes"]
    if out is not None:
        p = "/recent_outcomes"
        _obj(out, p, ("items",))
        items = _list(out["items"], p + "/items", maxlen=10)
        for i, x in enumerate(items):
            q = f"{p}/items/{i}"
            _obj(x, q, ("id", "encounter_type", "result", "hp_lost", "turns", "notes"))
            _str(x["id"], q + "/id", maxlen=_SHORT, nullable=False, nonempty=True)
            _str(x["encounter_type"], q + "/encounter_type", maxlen=_SHORT)
            _enum(x["result"], q + "/result", OUTCOME_RESULTS)
            _int(x["hp_lost"], q + "/hp_lost", lo=0)
            _int(x["turns"], q + "/turns", lo=0)
            _str(x["notes"], q + "/notes", maxlen=500)
        _unique(items, "id", p + "/items")


# ---------------------------------------------------------------------------
# hashing / JSON pointer
# ---------------------------------------------------------------------------

def canonical_json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(v: Any) -> str:
    return hashlib.sha256(canonical_json(v).encode("utf-8")).hexdigest()


def escape_pointer_token(tok: str) -> str:
    return tok.replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc: Any, pointer: str) -> tuple[bool, Any]:
    """RFC 6901 resolution. Returns (found, value). Raises ValueError if malformed."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("pointer must start with '/'")
    cur = doc
    for raw in pointer[1:].split("/"):
        i = 0
        while (i := raw.find("~", i)) != -1:
            if i + 1 >= len(raw) or raw[i + 1] not in "01":
                raise ValueError(f"invalid escape in token {raw!r}")
            i += 2
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if tok not in cur:
                return False, None
            cur = cur[tok]
        elif isinstance(cur, list):
            if not tok.isdigit() or (len(tok) > 1 and tok[0] == "0"):
                raise ValueError(f"invalid array index {tok!r}")
            idx = int(tok)
            if idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategicSnapshot:
    """Caller-supplied, player-visible strategic facts at one revision of a run.

    Construction validates everything and freezes a private copy of ``facts``.
    ``snapshot_id`` and ``facts_hash`` are computed in code, never supplied.
    """

    run_id: str
    revision: int
    data_source: str
    facts: dict
    change_reasons: tuple = ()
    live_validated: bool = False
    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: str = field(init=False)
    facts_hash: str = field(init=False)
    _facts_json: str = field(init=False, repr=False)

    def __post_init__(self):
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise InputError(f"schema_version must be {SNAPSHOT_SCHEMA_VERSION!r}")
        _str(self.run_id, "run_id", maxlen=128, nullable=False, nonempty=True)
        _int(self.revision, "revision", lo=0, nullable=False)
        _enum(self.data_source, "data_source", DATA_SOURCES, nullable=False)
        if not isinstance(self.live_validated, bool):
            raise InputError("live_validated must be a boolean")
        if self.live_validated and self.data_source == "synthetic":
            raise InputError("synthetic data cannot be marked live_validated")
        reasons = self.change_reasons
        if isinstance(reasons, (str, bytes)) or not isinstance(reasons, (list, tuple)):
            raise InputError("change_reasons must be a list")
        if len(reasons) > MAX_CHANGE_REASONS:
            raise InputError(f"change_reasons: more than {MAX_CHANGE_REASONS} items")
        for r in reasons:
            _enum(r, "change_reasons", CHANGE_REASONS, nullable=False)
        if len(set(reasons)) != len(reasons):
            raise InputError("change_reasons: duplicates")
        validate_facts(self.facts)

        facts_json = canonical_json(self.facts)
        identity = {"schema_version": self.schema_version, "run_id": self.run_id,
                    "data_source": self.data_source, "live_validated": self.live_validated}
        full = {**identity, "revision": self.revision, "facts": self.facts, "change_reasons": list(reasons)}
        size = len(canonical_json(full).encode("utf-8"))
        if size > MAX_SNAPSHOT_BYTES:
            raise InputError(f"snapshot is {size} bytes; limit is {MAX_SNAPSHOT_BYTES} (nothing was dropped)")
        set_ = object.__setattr__
        set_(self, "change_reasons", tuple(reasons))
        set_(self, "_facts_json", facts_json)
        set_(self, "facts", json.loads(facts_json))  # private deep copy
        set_(self, "snapshot_id", "snap-" + _sha(full)[:24])
        set_(self, "facts_hash", "facts-" + _sha({**identity, "facts": self.facts})[:24])

    def facts_copy(self) -> dict:
        return json.loads(self._facts_json)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "run_id": self.run_id, "revision": self.revision,
                "data_source": self.data_source, "live_validated": self.live_validated,
                "change_reasons": list(self.change_reasons), "facts": self.facts_copy(),
                "snapshot_id": self.snapshot_id, "facts_hash": self.facts_hash}


# ---------------------------------------------------------------------------
# model output
# ---------------------------------------------------------------------------

def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise OutputError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_constant(name):
    raise OutputError(f"nonfinite number {name}")


def _sentences(text: str) -> int:
    t = text.strip()
    n = sum(1 for i, ch in enumerate(t) if ch in ".!?" and (i + 1 == len(t) or t[i + 1].isspace()))
    return max(n, 1 if t else 0)


def _evidence(v, path, facts) -> list:
    ev = _list(v, path, maxlen=3, err=OutputError)
    targets = []
    for i, p in enumerate(ev):
        if not isinstance(p, str):
            _fail(OutputError, f"{path}/{i}", "evidence must be a JSON Pointer string")
        try:
            found, val = resolve_pointer(facts, p)
        except ValueError as e:
            _fail(OutputError, f"{path}/{i}", f"invalid JSON Pointer {p!r}: {e}")
        if not found:
            _fail(OutputError, f"{path}/{i}", f"evidence {p!r} does not resolve inside the current facts")
        targets.append(val)
    if len(set(ev)) != len(ev):
        _fail(OutputError, path, "duplicate evidence paths")
    return targets


def parse_assessment(text: Any, facts: dict) -> dict:
    """Validate raw model text against the output contract. Returns the content dict."""
    if not isinstance(text, str):
        raise OutputError(f"model returned {type(text).__name__}, expected text")
    if len(text.encode("utf-8")) > MAX_MODEL_TEXT_BYTES:
        raise OutputError(f"model text exceeds {MAX_MODEL_TEXT_BYTES} bytes")
    body = text.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise OutputError("response must be a bare JSON object (no prose or code fences)")
    try:
        data = json.loads(body, object_pairs_hook=_no_dupes, parse_constant=_no_constant)
    except json.JSONDecodeError as e:
        raise OutputError(f"malformed JSON: {e}") from None

    E = OutputError
    _obj(data, "", ("summary", "dimensions", "priorities", "standing_plan", "uncertainties", "change_note"), err=E)
    _str(data["summary"], "/summary", maxlen=600, nullable=False, nonempty=True, err=E)
    if len(data["summary"].split()) > 60:
        _fail(E, "/summary", "more than 60 words")
    if _sentences(data["summary"]) > 2:
        _fail(E, "/summary", "more than 2 sentences")

    dims = _obj(data["dimensions"], "/dimensions", DIMENSIONS, err=E)
    for name in DIMENSIONS:
        p = f"/dimensions/{name}"
        d = _obj(dims[name], p, ("judgment", "horizon", "confidence", "evidence"), err=E)
        _str(d["judgment"], p + "/judgment", maxlen=240, nullable=False, nonempty=True, err=E)
        _str(d["horizon"], p + "/horizon", maxlen=80, nullable=False, nonempty=True, err=E)
        _enum(d["confidence"], p + "/confidence", CONFIDENCE, nullable=False, err=E)
        targets = _evidence(d["evidence"], p + "/evidence", facts)
        if d["confidence"] == "high" and not any(t is not None for t in targets):
            _fail(E, p + "/confidence", "high confidence requires at least one non-null evidence target")

    pri = _list(data["priorities"], "/priorities", maxlen=3, err=E)
    for i, it in enumerate(pri):
        p = f"/priorities/{i}"
        _obj(it, p, ("need", "horizon", "tradeoff", "reconsider_when", "evidence"), err=E)
        _str(it["need"], p + "/need", maxlen=160, nullable=False, nonempty=True, err=E)
        _str(it["horizon"], p + "/horizon", maxlen=80, nullable=False, nonempty=True, err=E)
        _str(it["tradeoff"], p + "/tradeoff", maxlen=160, nullable=False, err=E)
        _str(it["reconsider_when"], p + "/reconsider_when", maxlen=160, nullable=False, err=E)
        _evidence(it["evidence"], p + "/evidence", facts)

    plan = _obj(data["standing_plan"], "/standing_plan", tuple(PLAN_LISTS) + tuple(PLAN_TEXTS), err=E)
    for key, chars in PLAN_LISTS.items():
        for i, it in enumerate(_list(plan[key], f"/standing_plan/{key}", maxlen=MAX_PLAN_ITEMS, err=E)):
            _str(it, f"/standing_plan/{key}/{i}", maxlen=chars, nullable=False, nonempty=True, err=E)
    for key, chars in PLAN_TEXTS.items():
        _str(plan[key], f"/standing_plan/{key}", maxlen=chars, nullable=False, nonempty=True, err=E)

    unc = _list(data["uncertainties"], "/uncertainties", maxlen=5, err=E)
    for i, u in enumerate(unc):
        _str(u, f"/uncertainties/{i}", maxlen=160, nullable=False, nonempty=True, err=E)
    _str(data["change_note"], "/change_note", maxlen=240, nullable=False, nonempty=True, err=E)
    return data


# ---------------------------------------------------------------------------
# envelopes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategicAssessment:
    """Code-owned envelope around a validated model assessment."""

    run_id: str
    input_revision: int
    snapshot_id: str
    facts_hash: str
    data_source: str
    live_validated: bool
    prompt_version: str
    content: dict
    schema_version: str = ASSESSMENT_SCHEMA_VERSION
    model_generated: bool = True  # produced by the injected text model (could be a scripted fake in tests)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "prompt_version": self.prompt_version,
                "run_id": self.run_id, "input_revision": self.input_revision,
                "snapshot_id": self.snapshot_id, "facts_hash": self.facts_hash,
                "provenance": {"data_source": self.data_source, "live_validated": self.live_validated},
                "content": json.loads(json.dumps(self.content))}


UPDATED, UNCHANGED, FAILED, INVALID_INPUT = "updated", "unchanged", "failed", "invalid_input"


@dataclass(frozen=True)
class PlannerResult:
    """Outcome of one ``StrategicPlanner.update`` call.

    ``assessment`` may be historical. Use ``current_assessment`` for advice that
    is valid for the facts just supplied; it is ``None`` after a failure unless
    the previous assessment still matches the current facts *and* no refresh
    was required, which never happens for ``failed``.
    """

    status: str
    assessment: StrategicAssessment | None
    applies_to_current_facts: bool
    snapshot_id: str | None             # binding of the snapshot just checked (None if input invalid)
    latest_checked_revision: int | None
    model_called: bool
    diagnostic: str | None = None

    def __post_init__(self):
        if self.diagnostic and len(self.diagnostic) > MAX_DIAGNOSTIC_CHARS:
            object.__setattr__(self, "diagnostic", self.diagnostic[:MAX_DIAGNOSTIC_CHARS - 1] + "…")

    @property
    def current_assessment(self) -> StrategicAssessment | None:
        if self.status in (UPDATED, UNCHANGED) and self.applies_to_current_facts:
            return self.assessment
        return None
