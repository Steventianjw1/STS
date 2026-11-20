"""Bind observed native previews to individual LLM combat candidates.

These are current-state UI previews, not rollouts. A multi-hit attack can change
statuses between hits. No status multiplier is applied here.
"""
from __future__ import annotations


def action_preview(card: dict, enemies: list[dict], target: dict | None = None) -> dict:
    affected = [target] if target is not None else (
        enemies if card["target_type"] == "AllEnemies" else [None])
    previews = card.get("combat_previews")
    results = []
    for enemy in affected:
        target_id = enemy["combat_id"] if enemy is not None else None
        native = next((p for p in previews or [] if p["target_combat_id"] == target_id), None)
        variables = native["variables"] if native else None
        values = {v["name"]: v["preview_value"] for v in variables or []}
        effects = card["effects"]
        # Summarize only fully parsed descriptions using the native variable,
        # never a number scraped from an untargeted description.
        simple = effects["fully_supported"]
        damage = values.get("Damage") if simple and effects["damage_per_hit"] is not None else None
        block = values.get("Block") if simple and effects["block"] is not None else None
        hits = effects["hits"] if damage is not None else None
        total = damage * hits if damage is not None and hits is not None else None
        target_block = enemy.get("block") if enemy else None
        hp = enemy.get("hp") if enemy else None
        hp_loss = min(hp, max(0, total - target_block)) if (
            total is not None and target_block is not None and hp is not None) else None
        results.append({
            "target_combat_id": target_id,
            "source": "game_native_preview_v1" if variables is not None else "unavailable",
            "variables": variables,
            "damage_per_hit": damage,
            "hits_from_text": hits,
            "total_damage_if_unchanged": total,
            "target_block": target_block,
            "hp_loss_if_unchanged": hp_loss,
            "block_gain": block,
        })
    return {"targets": results}
