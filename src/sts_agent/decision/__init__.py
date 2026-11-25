"""LLM decision engine: frames, screen adapters, LLM selection, run coordination."""
from .coordinator import RunCoordinator
from .models import Choice, ChoiceError, DecisionFrame, parse_choice
from .policy import ChoiceResult, LLMDecisionPolicy
from . import prompts as _prompts
from .prompts import DEFAULT_OBJECTIVE
from .screens import CAPABILITIES, SOURCE_INSPECTED, SYNTHETIC_ONLY, UNSUPPORTED, build_view, capability_table

__all__ = ["RunCoordinator", "Choice", "ChoiceError", "DecisionFrame", "parse_choice", "ChoiceResult",
           "LLMDecisionPolicy", "DECISION_PROMPT_VERSION", "DEFAULT_OBJECTIVE", "SYSTEM_INSTRUCTION", "CAPABILITIES",
           "SOURCE_INSPECTED", "SYNTHETIC_ONLY", "UNSUPPORTED", "build_view", "capability_table"]


def __getattr__(name):
    if name in ("DECISION_PROMPT_VERSION", "SYSTEM_INSTRUCTION"):       # of the game build in use, read when asked
        return getattr(_prompts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
