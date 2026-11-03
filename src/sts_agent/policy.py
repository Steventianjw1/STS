"""Deterministic, explainable baseline policy.

The policy is a pure function of (observation, candidates). It makes no HTTP
calls and keeps no state between decisions, so it can be replayed from traces
and later swapped for search or a learned model.

Rules, in priority order (starter heuristics, not optimal play):

1. ``lethal``        - a verified single action that defeats every remaining enemy.
2. ``prevent_damage``- a verified kill of an enemy whose visible intent is an attack.
3. ``block``         - block when visible incoming attack damage exceeds current block.
4. ``damage``        - spend energy on the most energy-efficient damage.
5. ``end_turn``      - nothing supported and useful remains.

"Verified" damage means: the card's observed description was fully parsed,
the target's HP and block are known, the target carries only statuses that do
not reduce damage taken (by name: Vulnerable, Weak, Strength), and the player
has no debuffs (a debuff such as Weak might not be reflected in card text).
Vulnerable on the target is ignored, so verified damage is a lower bound.

Tie-breaking is explicit in every sort key and ends with hand index and target
combat_id, so identical inputs always give identical outputs.
"""
from __future__ import annotations

from .actions import ACTION_END_TURN, ACTION_PLAY_CARD

POLICY_NAME = "baseline-heuristic"
POLICY_VERSION = "1.0.0"

NON_REDUCING_TARGET_STATUSES = {"vulnerable", "weak", "strength"}


def _decision(cand: dict, rule: str, reason: str, scores: dict | None = None) -> dict:
    return {
        "action_id": cand["action_id"],
        "action": cand,
        "policy": {"name": POLICY_NAME, "version": POLICY_VERSION},
        "rule": rule,
        "reason": reason,
        "scores": scores or {},
    }


def _ehp(enemy: dict) -> int | None:
    if enemy.get("hp") is None or enemy.get("block") is None:
        return None
    return enemy["hp"] + enemy["block"]


def _damage_verification(obs: dict, enemy: dict) -> str | None:
    """Return None if damage against ``enemy`` is verifiable, else why not."""
    if _ehp(enemy) is None:
        return "target HP/block unknown"
    if enemy.get("statuses") is None:
        return "target statuses unknown"
    for s in enemy["statuses"]:
        if (s.get("name") or "").strip().lower() not in NON_REDUCING_TARGET_STATUSES:
            return f"target status {s.get('name')!r} not modelled"
    pst = obs["player"].get("statuses")
    if pst is None:
        return "player statuses unknown"
    for s in pst:
        if s.get("type") == "Debuff":
            return f"player debuff {s.get('name')!r} may change damage"
    return None


class BaselinePolicy:
    name = POLICY_NAME
    version = POLICY_VERSION

    def decide(self, obs: dict, candidates: list[dict]) -> dict:
        if not candidates:
            raise ValueError("no candidates supplied")
        end = next((c for c in candidates if c["kind"] == ACTION_END_TURN), None)
        plays = [c for c in candidates if c["kind"] == ACTION_PLAY_CARD]
        hand = {c["index"]: c for c in obs.get("hand") or []}
        enemies = [e for e in obs.get("enemies") or [] if e.get("hp") is None or e["hp"] > 0]
        by_id = {e["combat_id"]: e for e in enemies}

        analyses = [self._analyse(obs, c, hand[c["card"]["index"]], enemies, by_id) for c in plays]

        # 1. Verified lethal on the whole fight.
        lethal = [a for a in analyses if a["verified"] and enemies and a["kills"] >= set(by_id)]
        if lethal:
            a = min(lethal, key=lambda a: (a["cost"], a["index"], a["target_id"] or -1))
            return _decision(a["cand"], "lethal",
                             f"Selected {a['name']} because its verified damage ({a['damage']}) defeats every remaining enemy.",
                             _scores(a))

        # 2. Verified kill of an attacking enemy.
        prevent = []
        for a in analyses:
            if not a["verified"]:
                continue
            killed_attackers = [by_id[k] for k in a["kills"] if by_id[k]["has_attack_intent"]]
            if not killed_attackers:
                continue
            known = [e["incoming_damage"] for e in killed_attackers if e["incoming_damage"] is not None]
            unknown = len(known) < len(killed_attackers)
            a["prevented"] = sum(known)
            a["prevented_unknown"] = unknown
            prevent.append(a)
        if prevent:
            a = min(prevent, key=lambda a: (not a["prevented_unknown"], -a["prevented"], a["cost"], a["index"], a["target_id"] or -1))
            names = ", ".join(by_id[k]["name"] or "?" for k in sorted(a["kills"]))
            amount = "an unknown amount of" if a["prevented_unknown"] else str(a["prevented"])
            return _decision(a["cand"], "prevent_damage",
                             f"Selected {a['name']} because its verified damage ({a['damage']}) defeats {names}, preventing {amount} incoming damage.",
                             _scores(a, prevented=a["prevented"], prevented_unknown=a["prevented_unknown"]))

        # 3. Block against visible incoming attacks.
        incoming, incoming_unknown = 0, False
        for e in enemies:
            if e["incoming_damage"] is None:
                if e["has_attack_intent"] or e["intents"] is None:
                    incoming_unknown = True
            else:
                incoming += e["incoming_damage"]
        block_now = obs["player"].get("block")
        any_attack = any(e["has_attack_intent"] for e in enemies)
        unblocked = None if (incoming_unknown or block_now is None) else incoming - block_now
        block_useful = (unblocked is not None and unblocked > 0) or (incoming_unknown and any_attack)
        if block_useful:
            blockers = [a for a in analyses if a["block"]]
            if blockers:
                cap = unblocked if unblocked is not None else 10**9
                a = min(blockers, key=lambda a: (-min(a["block"], cap), a["cost"], -a["damage"], a["index"], a["target_id"] or -1))
                why = (f"{unblocked} unblocked incoming damage" if unblocked is not None
                       else "an attack intent of unknown size")
                return _decision(a["cand"], "block",
                                 f"Selected {a['name']} for {a['block']} block against {why}.",
                                 _scores(a, incoming=None if incoming_unknown else incoming,
                                         current_block=block_now, unblocked=unblocked))

        # 4. Useful damage.
        attackers = [a for a in analyses if a["damage"] > 0 and a["useful_damage"] > 0]
        if attackers:
            # Verified kills first (removing an enemy removes its future damage),
            # then damage per energy, then explicit tie-breaks.
            a = min(attackers, key=lambda a: (
                -len(a["kills"]), -a["efficiency"], -a["useful_damage"], a["cost"], a["index"],
                0 if a["target_attacking"] else 1, a["target_ehp"] if a["target_ehp"] is not None else 10**9,
                a["target_id"] or -1))
            tgt = f" on {a['target_name']}" if a["target_name"] else ""
            kill = " and defeats the target" if a["kills"] else ""
            return _decision(a["cand"], "damage",
                             f"Selected {a['name']}{tgt}: {a['useful_damage']} useful damage "
                             f"({a['efficiency']:.2f} per energy){kill}; no lethal, preventable attack, or needed block.",
                             _scores(a))

        # 5. End turn.
        if end is None:
            raise ValueError("end_turn candidate missing")
        n = len(plays)
        return _decision(end, "end_turn",
                         "Ended turn: no supported card adds useful block or damage." if n else
                         "Ended turn: no supported playable cards.",
                         {"incoming": None if incoming_unknown else incoming, "current_block": block_now,
                          "supported_plays_considered": n})

    def _analyse(self, obs, cand, card, enemies, by_id) -> dict:
        eff = card["effects"]
        dmg = eff["total_damage"] if eff["damage_per_hit"] is not None else 0
        targets = []
        if cand.get("target"):
            targets = [by_id[cand["target"]["combat_id"]]]
        elif eff["damage_all_enemies"] or card["target_type"] == "AllEnemies":
            targets = list(enemies)

        verified = dmg > 0 and bool(targets) and all(_damage_verification(obs, e) is None for e in targets)
        unverified_reasons = sorted({r for e in targets if (r := _damage_verification(obs, e))})
        kills = set()
        useful = 0
        for e in targets:
            ehp = _ehp(e)
            if ehp is None:
                useful += dmg
            else:
                useful += min(dmg, ehp)
                if verified and dmg >= ehp:
                    kills.add(e["combat_id"])
        cost = card["cost"] or 0
        t = cand.get("target")
        te = by_id.get(t["combat_id"]) if t else None
        return {
            "cand": cand,
            "name": card["name"],
            "index": card["index"],
            "cost": cost,
            "damage": dmg,
            "block": eff["block"] or 0,
            "verified": verified,
            "unverified_reasons": unverified_reasons,
            "kills": kills,
            "useful_damage": useful,
            "efficiency": useful / max(cost, 1),
            "target_id": t["combat_id"] if t else None,
            "target_name": t["name"] if t else None,
            "target_ehp": _ehp(te) if te else None,
            "target_attacking": bool(te and te["has_attack_intent"]),
        }


def _scores(a: dict, **extra) -> dict:
    out = {
        "card": a["name"], "hand_index": a["index"], "cost": a["cost"], "damage": a["damage"],
        "block": a["block"], "damage_verified": a["verified"], "unverified_reasons": a["unverified_reasons"],
        "kills": sorted(a["kills"]), "useful_damage": a["useful_damage"],
        "efficiency": round(a["efficiency"], 4), "target_combat_id": a["target_id"],
    }
    out.update(extra)
    return out
