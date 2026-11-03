"""Strategic planner (version 0): an LLM-backed, evidence-grounded run assessment.

Python validates caller-supplied facts, decides when a refresh is needed,
calls an injected text model once, and validates its JSON. It never selects
cards, routes, purchases or actions, and it is not connected to the combat
runner. See docs/STRATEGIC_PLANNER.md.
"""
from .models import (
    ASSESSMENT_SCHEMA_VERSION, CHANGE_REASONS, DATA_SOURCES, FAILED, INVALID_INPUT,
    SNAPSHOT_SCHEMA_VERSION, UNCHANGED, UPDATED, InputError, OutputError, PlannerResult,
    StrategicAssessment, StrategicSnapshot,
)
from .planner import StrategicPlanner, TextModel
from .prompts import PROMPT_VERSION, SYSTEM_INSTRUCTION

__all__ = [
    "StrategicPlanner", "TextModel", "StrategicSnapshot", "StrategicAssessment", "PlannerResult",
    "InputError", "OutputError", "UPDATED", "UNCHANGED", "FAILED", "INVALID_INPUT",
    "SNAPSHOT_SCHEMA_VERSION", "ASSESSMENT_SCHEMA_VERSION", "PROMPT_VERSION", "SYSTEM_INSTRUCTION",
    "DATA_SOURCES", "CHANGE_REASONS",
]
