"""Combat decisions from Spire Mentor's search, with the LLM policy as fallback.

Only ready combat frames go to the search. Every other screen, and every combat
decision the search cannot answer (advisor unreachable, board mismatch, no
mapping to an offered action, timeout), is decided by the wrapped LLM policy.
The chosen action is always one of the frame's own candidates and goes through
the same choice contract as a model reply, so validation, execution and replay
are unchanged.
"""
from __future__ import annotations

import json

from ..spire_mentor import SOURCE
from .models import MAX_REASON_CHARS, ChoiceError, DecisionFrame, parse_choice
from .policy import ChoiceResult, LLMDecisionPolicy


class SearchCombatPolicy:
    def __init__(self, inner: LLMDecisionPolicy, advisor):
        self.inner, self.advisor = inner, advisor
        self.name, self.version, self.prompt_version = inner.name, inner.version, inner.prompt_version
        self.model = inner.model

    @property
    def scripted(self) -> bool:
        return self.inner.scripted

    def choose(self, frame: DecisionFrame) -> ChoiceResult:
        if (frame.screen or {}).get("kind") != "combat":
            return self.inner.choose(frame)
        rec = self.advisor.recommend(frame.facts, frame.candidates)
        if rec.action_id is not None:
            p_win = rec.advice.get("p_win")
            reason = (f"Spire Mentor search: {rec.label or rec.action_id}"
                      + (f"; est. win {p_win:.0%}" if isinstance(p_win, (int, float)) else "")
                      + (f"; {rec.advice['note']}" if rec.advice.get("note") else ""))[:MAX_REASON_CHARS]
            text = json.dumps({"frame_id": frame.frame_id, "action_id": rec.action_id, "reason": reason,
                               "turn_intent": ""}, ensure_ascii=False)
            try:
                choice = parse_choice(text, frame)
            except ChoiceError as e:
                rec.action_id, rec.miss = None, f"mapped action failed the choice contract: {e}"[:300]
            else:
                return ChoiceResult(choice, attempts=[{"attempt": 1, "response_text": text, "error": None,
                                                       "call": None, "source": SOURCE}],
                                    source=SOURCE, search=rec.record())
        res = self.inner.choose(frame)
        res.search = rec.record()
        return res
