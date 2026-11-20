"""Minimal effect extraction from *observed* card descriptions.

This is deliberately not a general card-text interpreter. It recognises a
handful of sentence shapes used by the Ironclad starter deck and records
everything else as an unsupported effect. All numbers come from the live
description that the mod renders for the card in hand; nothing is imported
from the original Slay the Spire.

A card is ``fully_supported`` only when every sentence of its description was
recognised. The policy only plays fully supported cards.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Sentence patterns. Each must match a whole sentence (after trimming).
_DEAL = re.compile(r"^deal (\d+) damage(?: to (all enemies|all enemy|each enemy|an enemy|target enemy))?$", re.I)
_DEAL_TIMES = re.compile(r"^deal (\d+) damage (\d+) times(?: to (all enemies|each enemy))?$", re.I)
_BLOCK = re.compile(r"^gain (\d+) block$", re.I)
_APPLY = re.compile(r"^apply (\d+) (vulnerable|weak)(?: to (all enemies|each enemy))?$", re.I)

SUPPORTED_TARGET_TYPES = {"AnyEnemy", "AllEnemies", "None", "Self"}


@dataclass
class CardEffects:
    damage_per_hit: int | None = None
    hits: int = 0
    damage_all_enemies: bool = False
    block: int | None = None
    applies: dict[str, int] = field(default_factory=dict)  # e.g. {"vulnerable": 2}
    unsupported: list[str] = field(default_factory=list)

    @property
    def total_damage(self) -> int:
        if self.damage_per_hit is None:
            return 0
        return self.damage_per_hit * max(self.hits, 1)

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported and (
            self.damage_per_hit is not None or self.block is not None or bool(self.applies)
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_damage"] = self.total_damage
        d["fully_supported"] = self.fully_supported
        return d


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!])\s+|\.$", text)
    return [p.strip().rstrip(".").strip() for p in parts if p and p.strip().rstrip(".").strip()]


def parse_effects(description: str | None, card_type: str | None = None) -> CardEffects:
    eff = CardEffects()
    if not description:
        eff.unsupported.append("missing description")
        return eff
    for s in _sentences(description):
        m = _DEAL_TIMES.match(s)
        if m:
            if eff.damage_per_hit is not None:
                eff.unsupported.append(f"second damage clause: {s}")
                continue
            eff.damage_per_hit, eff.hits = int(m.group(1)), int(m.group(2))
            eff.damage_all_enemies = bool(m.group(3))
            continue
        m = _DEAL.match(s)
        if m:
            if eff.damage_per_hit is not None:
                eff.unsupported.append(f"second damage clause: {s}")
                continue
            eff.damage_per_hit, eff.hits = int(m.group(1)), 1
            eff.damage_all_enemies = bool(m.group(2)) and m.group(2).lower() in ("all enemies", "all enemy", "each enemy")
            continue
        m = _BLOCK.match(s)
        if m:
            if eff.block is not None:
                eff.unsupported.append(f"second block clause: {s}")
                continue
            eff.block = int(m.group(1))
            continue
        m = _APPLY.match(s)
        if m and not m.group(3):
            eff.applies[m.group(2).lower()] = int(m.group(1))
            continue
        eff.unsupported.append(s)
    # Keyword hover-tips (Vulnerable, Block, Exhaust, ...) are informational and
    # are not used to infer effects; only the description sentences are.
    if card_type in ("Status", "Curse"):
        eff.unsupported.append(f"card type {card_type}")
    return eff
