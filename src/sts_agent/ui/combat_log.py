"""Fight log rebuilt from consecutive observations of the game.

The mod reports states, not events, and a delegated fight is played by Autobattle
inside the game. So the log is a diff: each new combat observation is compared
with the previous one, and what moved (cards leaving the hand, HP, Block, statuses,
potions) becomes log entries. It is a reconstruction at the polling rate: two
cards played between two polls arrive as one entry, and which card caused which
effect inside that entry is not known.

Entries are plain dicts for the panel:

    {"k": "fight",  "fight": n, "title": ..., "enemies": [...], "hp": ..., "max_hp": ...}
    {"k": "turn",   "fight": n, "round": r, "energy": e, "hand": [...], "intents": [...]}
    {"k": "play",   "fight": n, "round": r, "cards": [{"name", "to"}], "effects": [...]}
    {"k": "potion", "fight": n, "round": r, "name": ..., "effects": [...]}
    {"k": "end",    "fight": n, "round": r, "left": cards still in hand}
    {"k": "start",  "fight": n, "round": r, "effects": [...]}        # before the turn's first play (relics, powers)
    {"k": "enemy",  "fight": n, "round": r, "effects": [...]}        # changes seen outside the player's turn
    {"k": "result", "fight": n, "result": ..., "hp_lost": ..., "turns": ..., "damage": ..., "healed": ...}
"""
from __future__ import annotations

from collections import Counter


def _card_name(card: dict) -> str:
    name = str(card.get("name") or card.get("id") or "?")
    return name + "+" if card.get("is_upgraded") and not name.endswith("+") else name


def _pile(piles: dict, key: str) -> Counter:
    out: Counter = Counter()
    for card in (piles or {}).get(key) or []:
        if isinstance(card, dict):
            out[str(card.get("name") or "?")] += card.get("count") if type(card.get("count")) is int else 1
    return out


def _statuses(owner: dict) -> dict:
    return {str(s.get("name") or s.get("id")): s.get("amount") for s in (owner or {}).get("statuses") or []
            if isinstance(s, dict)}


def _intent(enemy: dict) -> str:
    parts = []
    for it in enemy.get("intents") or []:
        if it.get("is_attack") and it.get("damage_per_hit") is not None:
            hits = it.get("hits") or 1
            parts.append(f"Attack {it['damage_per_hit']}" + (f"×{hits}" if hits > 1 else ""))
        else:
            parts.append(str(it.get("title") or it.get("type") or "?"))
    return " + ".join(parts) or "unknown"


class _Board:
    """The parts of one combat observation the log compares."""

    def __init__(self, facts: dict):
        combat, player = facts.get("combat") or {}, facts.get("player") or {}
        self.round = combat.get("round")
        self.side = combat.get("turn_side")
        self.play_phase = combat.get("is_play_phase") is True
        self.hp, self.max_hp = player.get("hp"), player.get("max_hp")
        self.block, self.energy = player.get("block"), player.get("energy")
        self.statuses = _statuses(player)
        self.hand = Counter(_card_name(c) for c in facts.get("hand") or [] if isinstance(c, dict))
        self.hand_order = [_card_name(c) for c in facts.get("hand") or [] if isinstance(c, dict)]
        piles = facts.get("piles") or {}
        self.discard, self.exhaust = _pile(piles, "discard"), _pile(piles, "exhaust")
        self.potions = Counter(str(p.get("name") or p.get("id") or "?") for p in facts.get("potions") or []
                               if isinstance(p, dict))
        self.enemies = {}
        for e in facts.get("enemies") or []:
            if isinstance(e, dict) and e.get("combat_id") is not None:
                self.enemies[e["combat_id"]] = {"name": str(e.get("name") or "?"), "hp": e.get("hp"),
                                                "max_hp": e.get("max_hp"), "block": e.get("block"),
                                                "statuses": _statuses(e), "intent": _intent(e)}
        names = Counter(e["name"] for e in self.enemies.values())
        for cid, e in self.enemies.items():   # tell duplicates apart
            e["label"] = f"{e['name']} #{cid}" if names[e["name"]] > 1 else e["name"]

    def alive(self) -> list:
        return [e for e in self.enemies.values() if e["hp"] is None or e["hp"] > 0]


def _status_gains(label: str, old: dict, new: dict) -> list:
    out = []
    for name, amount in new.items():
        before = old.get(name)
        if name not in old:
            out.append(f"{label} {'gain' if label == 'You' else 'gains'} {name}"
                       + (f" {amount}" if amount is not None else ""))
        elif type(amount) is int and type(before) is int and amount > before:
            out.append(f"{label} {name} {before}→{amount}")
    return out


def _effects(prev: _Board, cur: _Board, *, own_turn: bool) -> list:
    """What changed on the board between two observations, as short phrases."""
    out = []
    for cid, old in prev.enemies.items():
        new = cur.enemies.get(cid)
        label = old["label"]
        if new is None:
            if old["hp"] is None or old["hp"] > 0:
                out.append(f"{label} defeated")   # it left the board between two observations
            continue
        label = new["label"]
        if type(old["hp"]) is int and type(new["hp"]) is int and new["hp"] != old["hp"]:
            if new["hp"] <= 0:
                out.append(f"{label} defeated")
            elif new["hp"] < old["hp"]:
                out.append(f"{label} −{old['hp'] - new['hp']} HP ({new['hp']} left)")
            else:
                out.append(f"{label} +{new['hp'] - old['hp']} HP ({new['hp']})")
        if type(old["block"]) is int and type(new["block"]) is int and new["block"] > old["block"]:
            out.append(f"{label} +{new['block'] - old['block']} Block")
        out += _status_gains(label, old["statuses"], new["statuses"])
    for cid, new in cur.enemies.items():
        if cid not in prev.enemies:
            out.append(f"{new['label']} joins ({new['hp']} HP)")
    if type(prev.hp) is int and type(cur.hp) is int and cur.hp != prev.hp:
        out.append(f"You −{prev.hp - cur.hp} HP ({cur.hp} left)" if cur.hp < prev.hp
                   else f"You +{cur.hp - prev.hp} HP ({cur.hp})")
    if type(prev.block) is int and type(cur.block) is int:
        if cur.block > prev.block:
            out.append(f"You +{cur.block - prev.block} Block ({cur.block})")
        elif cur.block < prev.block and not own_turn and cur.round == prev.round:
            out.append(f"Block absorbs {prev.block - cur.block}")
    out += _status_gains("You", prev.statuses, cur.statuses)
    return out


class CombatLog:
    """Feed it every observed ``ScreenView``; it returns the new log entries."""

    def __init__(self):
        self.fight = 0
        self.key: str | None = None
        self.prev: _Board | None = None
        self.announced_round = None
        self.ended_round = None

    def update(self, view) -> list:
        if not getattr(view, "in_combat", False) or not isinstance(view.facts, dict):
            return []   # reward screens, pickers that open during a fight, non-combat rooms
        cur = _Board(view.facts)
        out = []
        if view.encounter_key != self.key or self.prev is None:
            self.key, self.prev = view.encounter_key, None
            self.fight += 1
            self.announced_round = self.ended_round = None
            run = view.facts.get("run") or {}
            out.append({"k": "fight", "fight": self.fight, "kind": view.state_type or "monster",
                        "act": run.get("act"), "floor": run.get("floor"),
                        "enemies": [f"{e['label']} ({e['hp']} HP)" for e in cur.alive()],
                        "hp": cur.hp, "max_hp": cur.max_hp, "round": cur.round})
        prev = self.prev
        if prev is not None:
            out += self._diff(prev, cur)
        if (cur.round is not None and cur.round != self.announced_round and cur.side == "player"
                and cur.play_phase and cur.hand_order):
            self.announced_round = cur.round
            out.append({"k": "turn", "fight": self.fight, "round": cur.round, "energy": cur.energy,
                        "hp": cur.hp, "block": cur.block, "hand": cur.hand_order,
                        "intents": [f"{e['label']}: {e['intent']}" for e in cur.alive()]})
        self.prev = cur
        return out

    def _diff(self, prev: _Board, cur: _Board) -> list:
        out = []
        same_turn = cur.round == prev.round and cur.side == "player" and prev.side == "player"
        left = prev.hand - cur.hand
        effects = _effects(prev, cur, own_turn=same_turn)
        used = list((prev.potions - cur.potions).elements())
        base = {"fight": self.fight, "round": prev.round}
        if used:
            out.append({**base, "k": "potion", "name": ", ".join(used),
                        "effects": [] if (left and same_turn) else effects})
            if not (left and same_turn):
                effects = []
        if same_turn and left:
            exhausted, discarded = cur.exhaust - prev.exhaust, cur.discard - prev.discard
            cards = []
            for name in left.elements():
                plain = name.rstrip("+")
                if exhausted.get(name) or exhausted.get(plain):
                    to = "exhaust"
                    exhausted[name if exhausted.get(name) else plain] -= 1
                elif discarded.get(name) or discarded.get(plain):
                    to = "discard"
                    discarded[name if discarded.get(name) else plain] -= 1
                else:
                    to = "play"   # a Power, or a card that left every visible pile
                cards.append({"name": name, "to": to})
            out.append({**base, "k": "play", "cards": cards, "effects": effects})
        elif not same_turn and prev.side == "player" and prev.round != self.ended_round:
            # The turn ended between the two observations: what left the hand was discarded, not played.
            self.ended_round = prev.round
            out.append({**base, "k": "end", "left": sum(left.values())})
            if effects:
                out.append({**base, "k": "enemy", "effects": effects})
        elif effects:
            # No card left the hand: the tail of the previous play, start-of-turn effects, or the enemy's turn.
            kind = "enemy" if not same_turn else "play" if cur.round == self.announced_round else "start"
            out.append({**base, "round": cur.round if kind == "start" else prev.round, "k": kind,
                        "cards": [], "effects": effects})
        return out

    def finish(self, item: dict, outcome: dict) -> list:
        """The coordinator's own fight summary closes the fight."""
        self.key, self.prev = None, None
        return [{"k": "result", "fight": self.fight, "result": item.get("result"),
                 "kind": item.get("encounter_type"), "hp_lost": item.get("hp_lost"), "turns": item.get("turns"),
                 "damage": outcome.get("hp_damage"), "healed": outcome.get("hp_healed")}]
