"""What did the work in one fight, rebuilt from consecutive observations of it.

The mod reports states, not events. Each new combat observation is compared with the previous one: the
cards that left the hand during the player's turn, the enemy HP removed and the Block gained in that
step. Inside one step the cards share what happened; a step with a single card is credited to it. The
agent's own pilot observes after every action, so its steps are single cards; a fight Autobattle plays is
seen at the polling rate and more of it is shared. Enemy HP lost across a turn boundary or with no card
played (Poison, Doom, Orbs, Thorns, relics and Powers at the start or end of a turn) is counted apart.

A card that left the hand is counted as played, so a card another card discards or exhausts is counted
too. "Seen in hand" means present in at least one observation of a player turn.
"""
from __future__ import annotations

from collections import Counter

_UNPLAYED_TYPES = ("status", "curse")
_MAX_CHARS = 700


def _name(card: dict) -> str:
    name = str(card.get("name") or card.get("id") or "?")
    return name + "+" if card.get("is_upgraded") and not name.endswith("+") else name


class _Board:
    def __init__(self, facts: dict):
        combat, player = facts.get("combat") or {}, facts.get("player") or {}
        self.round = combat.get("round")
        self.player_side = combat.get("turn_side") == "player"
        self.block = player.get("block")
        hand = [c for c in facts.get("hand") or [] if isinstance(c, dict)]
        self.hand = Counter(_name(c) for c in hand)
        self.types = {_name(c): str(c.get("type") or "").strip().lower() for c in hand}
        self.potions = Counter(str(p.get("name") or p.get("id") or "?") for p in facts.get("potions") or []
                               if isinstance(p, dict))
        self.enemies = {e["combat_id"]: e.get("hp") for e in facts.get("enemies") or []
                        if isinstance(e, dict) and e.get("combat_id") is not None}


def _hp_removed(prev: _Board, cur: _Board) -> int:
    total = 0
    for cid, before in prev.enemies.items():
        if type(before) is not int or before <= 0:
            continue
        after = cur.enemies.get(cid)
        if after is None:
            total += before          # it left the board between two observations: defeated
        elif type(after) is int and after < before:
            total += before - max(after, 0)
    return total


class FightTally:
    """Feed it every observation of one fight with ``observe``; ``summary`` is the text for the fight's record."""

    def __init__(self, partial: bool = False):
        self.partial = partial          # True when the start of the fight was not observed by this tally
        self.prev: _Board | None = None
        self.damage: Counter = Counter()
        self.block: Counter = Counter()
        self.played: Counter = Counter()
        self.first_round: dict = {}
        self.types: dict = {}
        self.seen: set = set()
        self.shared_damage = self.shared_block = 0
        self.outside_damage = 0
        self.potion_damage = self.potion_block = 0
        self.final_damage = 0           # enemy HP still on the board at the last observation of a won fight

    def observe(self, facts: dict):
        if not isinstance(facts, dict):
            return
        cur = _Board(facts)
        self.types.update(cur.types)
        if cur.player_side:
            self.seen.update(cur.hand)
        prev, self.prev = self.prev, cur
        if prev is None:
            return
        removed = _hp_removed(prev, cur)
        same_turn = prev.player_side and cur.player_side and prev.round == cur.round
        if not same_turn:
            self.outside_damage += removed     # end and start of turn, the enemy's turn
            return
        gained = cur.block - prev.block if type(cur.block) is int and type(prev.block) is int else 0
        gained = max(0, gained)
        left = list((prev.hand - cur.hand).elements())
        potions = bool(prev.potions - cur.potions)
        for name in left:
            self.played[name] += 1
            self.first_round.setdefault(name, prev.round)
        if left and not potions and len(left) == 1:
            self.damage[left[0]] += removed
            self.block[left[0]] += gained
        elif left:
            self.shared_damage += removed
            self.shared_block += gained
        elif potions:
            self.potion_damage += removed
            self.potion_block += gained
        else:
            self.outside_damage += removed     # no card left the hand: a Power or relic at the start of the turn

    def finish(self, result: str):
        """Close the fight: in a victory, the HP still on the board fell to plays after the last observation."""
        if result == "victory" and self.prev is not None:
            self.final_damage = sum(hp for hp in self.prev.enemies.values() if type(hp) is int and hp > 0)

    def summary(self) -> str | None:
        """Compact text: who dealt the damage and gained the Block, when the Powers came, what sat unplayed."""
        if not self.played and not self.outside_damage and not self.shared_damage:
            return None

        def top(counter, n):
            return ", ".join(f"{k} {v}" for k, v in [kv for kv in counter.most_common() if kv[1] > 0][:n])

        total = (sum(self.damage.values()) + self.shared_damage + self.outside_damage + self.potion_damage
                 + self.final_damage)
        parts = ["PLAY (rebuilt from observations; cards that left the hand in one observed step share its effects"
                 + ("; the start of this fight was not observed" if self.partial else "") + "):",
                 f"enemy HP removed {total}"
                 + (f", by single cards: {top(self.damage, 5)}" if any(self.damage.values()) else "")
                 + (f", by cards played together {self.shared_damage}" if self.shared_damage else "")
                 + (f", outside card plays (turn start or end, enemy turns: Poison, Doom, Orbs, Thorns, relics) "
                    f"{self.outside_damage}" if self.outside_damage else "")
                 + (f", by potions {self.potion_damage}" if self.potion_damage else "")
                 + (f", by the last plays after the last observation {self.final_damage}" if self.final_damage else "")
                 + "."]
        if any(self.block.values()) or self.shared_block or self.potion_block:
            parts.append("Block gained"
                         + (f" by single cards: {top(self.block, 4)}" if any(self.block.values()) else "")
                         + (f", by cards played together {self.shared_block}" if self.shared_block else "")
                         + (f", by potions {self.potion_block}" if self.potion_block else "") + ".")
        powers = sorted((r if type(r) is int else 99, n) for n, r in self.first_round.items()
                        if self.types.get(n) == "power")
        if powers:
            parts.append("Powers first played: " + ", ".join(f"{n} round {r}" if r != 99 else n for r, n in powers) + ".")
        idle = sorted(n for n in self.seen if n not in self.played and self.types.get(n) not in _UNPLAYED_TYPES)
        if idle:
            parts.append("Seen in hand, never played: " + ", ".join(idle[:8]) + ".")
        parts.append(f"Played most: {top(self.played, 5)}.")
        text = " ".join(parts)
        return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS - 14] + " …[truncated]"
