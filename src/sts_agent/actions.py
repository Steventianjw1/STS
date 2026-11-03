"""Action builder: supported legal combat actions for one observation.

Every candidate is bound to the observation fingerprint that produced it. Hand
indices shift after each play and enemy ``entity_id`` values are renumbered
by the mod when an enemy dies, so candidates must never be reused after any
action. Targets are sent to the mod as the numeric ``combat_id`` (verified in
the mod source: ``ResolveTarget`` accepts a numeric combat id), which does not
shift when other enemies die.
"""
from __future__ import annotations

from .cards import SUPPORTED_TARGET_TYPES

ACTION_PLAY_CARD = "play_card"
ACTION_END_TURN = "end_turn"


def _target_ok(enemy: dict) -> bool:
    return enemy.get("combat_id") is not None and (enemy.get("hp") is None or enemy["hp"] > 0)


def build_candidates(obs: dict) -> tuple[list[dict], list[dict]]:
    """Return ``(candidates, excluded)``.

    ``candidates`` are supported legal actions. ``excluded`` lists hand cards
    that were not offered and why (for the trace's unsupported-effects log).
    Returns no candidates unless the observation is ready for an action.
    """
    fp = obs["fingerprint"]
    if not obs.get("ready_for_action"):
        return [], []

    candidates: list[dict] = []
    excluded: list[dict] = []
    energy = obs["player"]["energy"]
    enemies = [e for e in (obs.get("enemies") or []) if _target_ok(e)]

    for card in obs.get("hand") or []:
        why = _exclusion_reason(card, energy, enemies)
        if why:
            excluded.append({"index": card["index"], "id": card["id"], "name": card["name"], "reason": why,
                             "unsupported_effects": card["effects"]["unsupported"]})
            continue
        snapshot = {k: card[k] for k in ("index", "instance_key", "id", "name", "cost", "description", "target_type")}
        if card["target_type"] == "AnyEnemy":
            for e in sorted(enemies, key=lambda e: e["combat_id"]):
                candidates.append(_play(fp, card, snapshot, e))
        else:
            candidates.append(_play(fp, card, snapshot, None))

    candidates.append({
        "action_id": "end_turn",
        "kind": ACTION_END_TURN,
        "obs_fingerprint": fp,
        "card": None,
        "target": None,
        "payload": {"action": "end_turn"},
    })
    return candidates, excluded


def _exclusion_reason(card: dict, energy: int | None, enemies: list[dict]) -> str | None:
    if card.get("can_play") is not True:
        return f"not playable ({card.get('unplayable_reason') or 'can_play unknown'})"
    if card.get("cost_is_x"):
        return "X-cost cards not supported"
    if card.get("cost") is None:
        return "cost unknown"
    if energy is not None and card["cost"] > energy:
        return "insufficient energy"
    if card.get("target_type") not in SUPPORTED_TARGET_TYPES:
        return f"unsupported target type {card.get('target_type')!r}"
    if card["target_type"] == "AnyEnemy" and not enemies:
        return "no valid enemy target"
    if not card["effects"]["fully_supported"]:
        return "unsupported effect text"
    return None


def _play(fp: str, card: dict, snapshot: dict, enemy: dict | None) -> dict:
    payload = {"action": "play_card", "card_index": card["index"]}
    target = None
    if enemy is not None:
        payload["target"] = str(enemy["combat_id"])
        target = {"combat_id": enemy["combat_id"], "entity_id": enemy["entity_id"], "name": enemy["name"]}
    aid = f"play:{card['index']}:{card['id']}" + (f"@{enemy['combat_id']}" if enemy else "")
    return {
        "action_id": aid,
        "kind": ACTION_PLAY_CARD,
        "obs_fingerprint": fp,
        "card": snapshot,
        "target": target,
        "payload": payload,
    }


def validate_against(candidate: dict, obs: dict) -> str | None:
    """Check that ``candidate`` is still valid for a fresh observation.

    Returns ``None`` if valid, otherwise a reason. Validation is strict: the
    fresh observation must have the same fingerprint as the one the candidate
    was built from. Any change means the decision is stale and must be remade.
    """
    if not obs.get("ready_for_action"):
        return f"game not ready (phase={obs.get('phase')})"
    if candidate["obs_fingerprint"] != obs["fingerprint"]:
        return "observation changed since the decision was made"
    if candidate["kind"] == ACTION_PLAY_CARD:
        idx = candidate["card"]["index"]
        hand = obs.get("hand") or []
        if idx >= len(hand) or hand[idx]["id"] != candidate["card"]["id"]:
            return "card at index no longer matches"
        if candidate.get("target"):
            ids = {e["combat_id"] for e in obs.get("enemies") or [] if _target_ok(e)}
            if candidate["target"]["combat_id"] not in ids:
                return "target no longer present"
    return None
