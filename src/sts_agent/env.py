"""Load only the supported model-provider settings from a local .env file: the three
DeepSeek ones, the Claude Console key and, for a key not tied to one workspace, its workspace ID.

No execution, interpolation, or export to traces; real environment values win.
Loading again is safe: a value added to the file since is picked up, nothing set is replaced.
"""
import os
from pathlib import Path

ENV_NAMES = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"}


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    values = {}
    for number, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or name not in ENV_NAMES:
            raise ValueError(f"{p}: invalid .env setting on line {number}")
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"{p}: unmatched quote on line {number}")
            value = value[1:-1]
        if value:
            values[name] = value
    for name, value in values.items():
        os.environ.setdefault(name, value)
