"""Keyword glossary pushed into prompts: definitions for game terms that appear in visible text.

Two sources, both static reference text and never observed game state:

* Card keywords (Exhaust, Ethereal, Eternal, ...): a small built-in table, worded here from
  the v0.107.1 rules as re-implemented in tfriedel/spire-mentor's engine (source inspection,
  not live validation).
* Powers / statuses (Plating, Vulnerable, Disintegration, ...) and curse / status cards
  (Greed, Wound, ...): the game's own text from local copies of spire-mentor's extracted
  ``powers.json`` and a reduced ``curse_cards.json`` (fetch both with
  ``tools/fetch_monster_data.py``; the files are git-ignored). Missing files = card keywords only.

Only terms found in the supplied text are returned, so the model reads a definition exactly
when the word is on screen. A term without an entry stays unknown.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

DATA_VERSION = "v0.107.1"
DEFAULT_DIR = "data/keyword_reference"
MAX_ITEMS = 24
_MAX_DEFINITION = 300

CARD_KEYWORDS = {
    "Exhaust": "When this card is played it goes to the exhaust pile and cannot be drawn again this combat. "
               "It stays in the permanent deck for later combats.",
    "Ethereal": "If this card is still in your hand at the end of your turn, it is exhausted for this combat.",
    "Eternal": "This card cannot be removed from your deck by any card-removal effect.",
    "Innate": "This card starts each combat in your opening hand.",
    "Retain": "This card is not discarded at the end of your turn; it stays in your hand.",
    "Unplayable": "This card cannot be played from your hand. It still takes a hand slot and a draw.",
    "Sly": "If this card is discarded from your hand before the end of your turn, it is played for free.",
}

_powers: dict = {}          # title -> definition
_cards: dict = {}           # curse/status card title -> (definition, [keywords])
_pattern: re.Pattern | None = None
_path: str | None = None


def _plain(text) -> str:
    text = re.sub(r"\[/?[a-zA-Z_]+\]", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:_MAX_DEFINITION]


def _compile() -> None:
    global _pattern
    terms = sorted(set(CARD_KEYWORDS) | set(_powers) | set(_cards), key=len, reverse=True)
    # Case-sensitive whole words: game text capitalizes keywords ("Gain 4 Plating."), prose does not.
    _pattern = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z])")


def _read(directory, name):
    try:
        records = json.loads((Path(directory) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return [r for r in records if isinstance(r, dict) and isinstance(r.get("title"), str)] \
        if isinstance(records, list) else None


def configure(directory: str | None) -> bool:
    """Load reference definitions. Returns True when power text was read; card keywords always work."""
    global _powers, _cards, _path
    _powers, _cards, _path = {}, {}, None
    powers = _read(directory, "powers.json") if directory else None
    for rec in powers or []:
        title, text = rec["title"].strip(), _plain(rec.get("description"))
        # Short and lower-case titles would match ordinary prose.
        if len(title) >= 3 and title[0].isupper() and text and title not in CARD_KEYWORDS:
            kind = str(rec.get("type") or "").lower()
            _powers.setdefault(title, (f"{kind.capitalize()}. " if kind in ("buff", "debuff") else "") + text)
    for rec in (_read(directory, "curse_cards.json") if directory else None) or []:
        title, kind = rec["title"].strip(), str(rec.get("type") or "card")
        keywords = [k for k in rec.get("keywords") or [] if k in CARD_KEYWORDS]
        if len(title) >= 3 and title not in CARD_KEYWORDS and title not in _powers:
            text = _plain(rec.get("description_plain"))
            _cards[title] = (f"{kind} card." + (f" Keywords: {', '.join(keywords)}." if keywords else "")
                             + (f" {text}" if text else ""), keywords)
    if powers is not None:
        _path = str(directory)
    _compile()
    return _path is not None


def status() -> dict:
    return {"card_keywords": len(CARD_KEYWORDS), "powers": len(_powers), "curse_cards": len(_cards), "path": _path,
            "data_version": DATA_VERSION}


_TEXT_KEY = re.compile(r"description|details|keywords|body|prompt", re.I)


def _texts(value, wanted=True):
    """Rules text only: strings under description-like keys. Names are skipped, so a card that
    shares its name with a power does not pull that power's entry in by itself."""
    if isinstance(value, str):
        if wanted:
            yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _texts(v, bool(_TEXT_KEY.search(str(k))) if isinstance(v, (str, list, tuple)) else False)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _texts(v, wanted if isinstance(v, str) else True)


def lookup(*sources, limit: int = MAX_ITEMS) -> list[dict]:
    """Definitions for known terms found in ``sources`` (strings or nested JSON-like values).

    Earlier sources win when the limit is reached, so pass what is being decided first.
    """
    if _pattern is None:
        _compile()
    found: dict = {}
    for source in sources:
        for text in _texts(source):
            for term in _pattern.findall(text):
                # A named curse/status card brings its own keywords along (Greed -> Eternal).
                for t in [term] + (_cards[term][1] if term in _cards else []):
                    if t not in found and len(found) < limit:
                        found[t] = None
    return [{"term": t, "kind": "card_keyword" if t in CARD_KEYWORDS else "power" if t in _powers else "card",
             "definition": CARD_KEYWORDS.get(t) or _powers.get(t) or _cards[t][0]} for t in found]


def for_facts(decided, background) -> dict | None:
    """Prompt block: terms in what is being decided first, then in the rest of the visible text."""
    items = lookup(decided, background)
    if not items:
        return None
    return {"label": "REFERENCE DEFINITIONS for keywords in the visible text; static rules text, not observed state",
            "source": f"built-in card keywords; power and curse/status card text from {DATA_VERSION} reference data"
                      if _powers else "built-in card keywords only (reference data not installed: "
                                      "tools/fetch_monster_data.py)",
            "items": items}


_compile()
