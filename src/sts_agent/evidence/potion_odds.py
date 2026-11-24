"""Potion odds for route projections: potions held, the combat drop counter, and a potion's HP value.

The drop rule is the one the person running this agent gave: after each won fight a potion
drops with 40% chance, plus 10% for every fight since the last drop (the chance returns to
40% after a drop). Each potion is valued as HP it saves in a fight: 6-10 HP, 8 on average.
Relics that change drops or potion slots are not read. The counter comes from this run's own
rewards screens (``potion_reward`` on each recorded fight outcome); fights whose rewards were not
observed leave it unchanged and are counted separately.
"""
from __future__ import annotations

BASE_DROP_PCT, STEP_DROP_PCT = 40, 10
POTION_HP = {"low": 6, "mean": 8, "high": 10}
# Who spends the potions in the projection. Autobattle drinks every held potion in the next fight;
# the agent's own play keeps them for elites and for a fight that would otherwise end the run.
NEXT_FIGHT, RESERVE = "next_fight", "reserve_for_elites_and_lethal_fights"


def drop_chance_pct(fights_without_drop: int) -> int:
    return min(100, BASE_DROP_PCT + STEP_DROP_PCT * max(0, fights_without_drop))


def drop_state(outcomes) -> dict:
    """The drop counter from this run's recorded fights (lost fights roll nothing)."""
    without = drops = observed = unobserved = 0
    for o in outcomes or []:
        if o.get("result") == "defeat":
            continue
        seen = o.get("potion_reward")
        if seen is True:
            without, drops, observed, unobserved = 0, drops + 1, observed + 1, 0
        elif seen is False:
            without, observed = without + 1, observed + 1
        else:
            unobserved += 1
    return {"fights_without_drop": without, "next_fight_drop_chance_pct": drop_chance_pct(without),
            "drops_observed": drops, "fights_with_observed_rewards": observed,
            "fights_unobserved_since_last_drop": unobserved,
            "basis": (f"{BASE_DROP_PCT}% after each won fight, +{STEP_DROP_PCT}% per fight without a drop, back to "
                      f"{BASE_DROP_PCT}% after one; counter read from this run's rewards screens"
                      + (". Fights whose rewards were not seen are not counted, so the chance may be higher"
                         if unobserved else ""))}


def unknown_fight_share(profile) -> tuple[float | None, str | None]:
    """Share of ? nodes that the pooled runs met as a fight (a fight rolls for a potion)."""
    for group in ("all", "wins"):
        shares = ((((profile or {}).get("unknown_node_resolved_as") or {}).get(group) or {}).get("share_pct")) or {}
        if shares:
            return sum(v for k, v in shares.items() if k in ("monster", "elite")) / 100, group
    return None, None


def potion_model(potions, slots, outcomes, *, max_hp, profile, combat_delegated, value_hp=POTION_HP["mean"]):
    """Inputs for ``route_paths.simulate`` and the facts shown with them; None when potions were not observed."""
    if potions is None or not max_hp:
        return None
    held = len(potions)
    slots = slots if type(slots) is int and slots >= held else None
    state = drop_state(outcomes)
    share, basis = unknown_fight_share(profile)
    p_none, within = 1.0, {}
    for n in range(1, 4):
        p_none *= 1 - drop_chance_pct(state["fights_without_drop"] + n - 1) / 100
        within[str(n)] = round((1 - p_none) * 100, 1)
    model = {"held": held, "slots": slots, "without": state["fights_without_drop"],
             "value_pct": value_hp / max_hp * 100, "policy": NEXT_FIGHT if combat_delegated else RESERVE,
             "unknown_fight_share": share or 0.0}
    facts = {"held": potions, "slots": slots, "free_slots": None if slots is None else slots - held,
             **state, "at_least_one_drop_within_next_fights_pct": within,
             "hp_saved_per_potion": {**POTION_HP, "mean_pct_of_max_hp": round(model["value_pct"], 1),
                                     "note": "an average; read each potion's own text, some save far more"},
             "potions_held_hp_equivalent_pct": round(held * model["value_pct"], 1),
             "projected_use": (("Autobattle drinks every held potion in the next fight" if combat_delegated else
                                "one potion per elite, and as many as needed in any fight that would otherwise "
                                "end the run; the rest are kept for the boss")
                               + "; a drop with every slot full is lost but still resets the chance"),
             "unknown_nodes_rolling_for_a_potion": (None if share is None else
                                                    {"fight_share": round(share, 2), "basis": basis})}
    return model, facts
