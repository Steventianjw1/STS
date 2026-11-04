"""Ancient offers: each option's relic effects compared with the current deck and state.

Evidence only; the model chooses. Effects are text-derived from the Spire Codex relic
catalog with explicit patterns. Expert behaviour comes from indexed Spire Codex runs of
the selected cohort (offer-conditioned: take rates, head-to-head, similar situations).
"""
from __future__ import annotations

import re
from collections import defaultdict

from .route_profile import HP_BANDS
from .spire_history import _similarity

_NUM = r"(\d+)"
EFFECT_PATTERNS = (  # (tag, regex on the rendered, tag-stripped description, lower case)
    ("removes_cards", r"remove " + _NUM + r" cards? from your deck"),
    ("removes_cards", r"remove (a) card from your deck"),
    ("hp_loss", r"lose " + _NUM + r" hp"),
    ("max_hp_gain", r"gain " + _NUM + r" max hp"),
    ("max_hp_gain", r"raise your max hp by " + _NUM),
    ("max_hp_loss", r"lose " + _NUM + r" max hp"),
    ("gold_gain", r"gain " + _NUM + r" gold"),
    ("gold_loss", r"lose " + _NUM + r" gold"),
    ("upgrades", r"upgrade " + _NUM + r" (?:random )?cards?"),
    ("transforms", r"transform " + _NUM + r" cards?"),
    ("adds_cards", r"add " + _NUM + r" .{0,60}?to your deck"),
    ("adds_curse", r"(curse)"),
    ("potions", r"(potion)"),
    ("enchants", r"(enchant)"),
    ("heal", r"heal " + _NUM),
)


def _plain(text):
    return " ".join(re.sub(r"\[[^\]]*\]|<[^>]*>", "", text or "").split()).lower()


def relic_effects(relic):
    """Text-derived tags with amounts where the pattern captures a number."""
    text = _plain(relic.get("description"))
    tags = {}
    for tag, pattern in EFFECT_PATTERNS:
        m = re.search(pattern, text)
        if m and tag not in tags:
            value = m.group(1)
            tags[tag] = int(value) if value.isdigit() else (1 if value == "a" else True)
    tags["upon_pickup"] = "upon pickup" in text
    return tags


def deck_facts(deck, cards):
    """Counts the advice compares against; None when the permanent deck is not observed."""
    if not isinstance(deck, list):
        return None
    out = {"size": len(deck), "starter_cards": 0, "curses": 0, "upgradable": 0, "attacks": 0, "skills": 0,
           "powers": 0, "unknown_card_ids": []}
    for entry in deck:
        cid = re.sub(r"^CARD\.", "", str(entry.get("id", "")).upper())
        card = cards.get(cid)
        if card is None:
            out["unknown_card_ids"].append(entry.get("id"))
            continue
        out["starter_cards"] += card.get("rarity") == "Basic"
        out["curses"] += card.get("type") == "Curse" or card.get("rarity") == "Curse"
        out["upgradable"] += entry.get("is_upgraded") is not True and bool(card.get("upgrade"))
        for kind, key in (("Attack", "attacks"), ("Skill", "skills"), ("Power", "powers")):
            out[key] += card.get("type") == kind
    return out


def state_comparison(effects, deck, *, hp, max_hp, gold, expert_marks):
    """Concrete before/after numbers for each effect against the current state."""
    out = {}
    if "removes_cards" in effects and deck:
        n = effects["removes_cards"] if isinstance(effects["removes_cards"], int) else 1
        out["removal"] = {"cards_removed": n, "starter_or_curse_cards_in_deck": deck["starter_cards"] + deck["curses"],
                          "deck_size_after": deck["size"] - n}
    if max_hp and hp is not None:
        loss = effects.get("hp_loss") if isinstance(effects.get("hp_loss"), int) else 0
        gain = effects.get("max_hp_gain") if isinstance(effects.get("max_hp_gain"), int) else 0
        drop = effects.get("max_hp_loss") if isinstance(effects.get("max_hp_loss"), int) else 0
        if loss or gain or drop:
            new_max = max_hp + gain - drop
            new_hp = max(0, min(new_max, hp - loss + gain))
            out["hp"] = {"hp_before_pct": round(hp / max_hp * 100, 1), "hp_after": new_hp, "max_hp_after": new_max,
                         "hp_after_pct": round(new_hp / new_max * 100, 1) if new_max else None,
                         "expert_marks": expert_marks.get("hp")}
    if gold is not None and ("gold_gain" in effects or "gold_loss" in effects):
        delta = (effects.get("gold_gain") or 0) - (effects.get("gold_loss") or 0)
        out["gold"] = {"gold_after": gold + (delta if isinstance(delta, int) else 0),
                       "expert_marks": expert_marks.get("gold")}
    if "upgrades" in effects and deck:
        out["upgrades"] = {"upgrades": effects["upgrades"], "upgradable_cards_in_deck": deck["upgradable"]}
    if "transforms" in effects and deck:
        out["transforms"] = {"transforms": effects["transforms"], "starter_cards_in_deck": deck["starter_cards"]}
    return out


def _band(hp_pct):
    return next((n for n, lo, hi in HP_BANDS if hp_pct is not None and lo <= hp_pct < hi), None)


def _situation(floor, hp_pct):
    # The run's first floor has no earlier HP record; every run starts there, so it is one situation.
    return "run_start" if floor is not None and floor <= 1 else _band(hp_pct)


def expert_offer_stats(rows, offered, *, hp_pct, act, floor, deck_ids, similarity_min, min_samples):
    """Offer-conditioned behaviour of the cohort: take rates, head-to-head, similar situations, outcomes."""
    groups = defaultdict(dict)
    meta = {}
    for r in rows:
        if r["abandoned"]:
            continue
        key = (r["run_hash"], r["floor"])
        groups[key][r["relic_id"]] = r["chosen"]
        meta[key] = r
    band = _situation(floor if act in (None, 1) else None, hp_pct)
    out = {}
    for a in offered:
        with_a = [k for k, g in groups.items() if a in g]
        taken = [k for k in with_a if groups[k][a]]
        res = {"offered": len(with_a), "taken": len(taken),
               "take_rate_pct": round(len(taken) / len(with_a) * 100, 1) if with_a else None,
               "sparse": len(with_a) < min_samples, "head_to_head": []}
        for b in offered:
            if b == a:
                continue
            both = [k for k in with_a if b in groups[k]]
            if both:
                res["head_to_head"].append({"vs": b, "co_offered": len(both),
                                            "took_this": sum(groups[k][a] for k in both),
                                            "took_other": sum(groups[k][b] for k in both)})
        similar = [k for k in with_a if band is not None
                   and _situation(meta[k]["floor"], meta[k]["arrival_hp_pct"]) == band]
        basis = "run start" if band == "run_start" else f"arrival HP band {band}"
        if type(act) is int and act > 1 and deck_ids is not None:
            similar = [k for k in similar if (meta[k]["deck"] or {}).get("identity_complete")
                       and _similarity(meta[k]["deck"]["cards"], deck_ids) >= similarity_min]
            basis += f" and base-card deck similarity >= {similarity_min:g}"
        res["similar_situations"] = {"basis": basis, "offered": len(similar),
                                     "take_rate_pct": round(sum(groups[k][a] for k in similar) / len(similar) * 100, 1)
                                     if similar else None, "sparse": len(similar) < min_samples}

        def win_rate(keys):
            known = [meta[k]["win"] for k in keys if meta[k]["win"] is not None]
            return {"runs": len(known), "win_rate_pct": round(sum(known) / len(known) * 100, 1) if known else None}
        res["outcomes"] = {"took_it": win_rate(taken), "offered_not_taken": win_rate([k for k in with_a if k not in taken]),
                           "note": "correlation within the cohort; stronger runs choose differently"}
        out[a] = res
    return out


def archetype_fit(relic_id, archetypes, deck_ids):
    """Community archetypes that define this relic, and how much of each one's core the deck holds."""
    fits = []
    for a in archetypes or []:
        relics = {str(r.get("id", "")).upper() for r in a.get("defining_relics") or [] if isinstance(r, dict)}
        if relic_id not in relics:
            continue
        core = [str(c.get("id", "")).upper() for c in a.get("defining_cards") or [] if isinstance(c, dict)]
        held = sorted(set(core) & deck_ids) if deck_ids is not None else None
        fits.append({"archetype": a.get("name"), "win_rate_pct": a.get("win_rate"), "share_pct": a.get("share"),
                     "core_cards": core, "deck_holds": held,
                     "deck_core_share": round(len(held) / len(core), 2) if held is not None and core else None})
    return sorted(fits, key=lambda f: -(f["deck_core_share"] or 0))[:3]


def labels(options):
    """Descriptive labels (what an option is most of); never a ranking."""
    def best(label, key, positive=True):
        scored = [(key(o), o) for o in options if key(o) is not None]
        scored = [(v, o) for v, o in scored if (v > 0 if positive else True)]
        if scored:
            top = max(v for v, _ in scored)
            for v, o in scored:
                if v == top:
                    o.setdefault("labels", []).append(label)
    best("largest_hp_cost", lambda o: o["effects"].get("hp_loss") if isinstance(o["effects"].get("hp_loss"), int) else None)
    best("removes_most_cards", lambda o: o["effects"].get("removes_cards") if isinstance(o["effects"].get("removes_cards"), int) else None)
    best("best_deck_archetype_fit", lambda o: max([f["deck_core_share"] or 0 for f in o["archetype_fit"]], default=None))
    best("highest_community_lift", lambda o: (o.get("community") or {}).get("lift"), positive=False)
    best("most_taken_by_experts_when_offered",
         lambda o: (o.get("experts") or {}).get("take_rate_pct") if not (o.get("experts") or {}).get("sparse") else None)
