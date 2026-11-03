"""Load only the three supported DeepSeek settings from a local .env file.

No execution, interpolation, or export to traces; real environment values win.
"""
import os
from pathlib import Path

ENV_NAMES = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"}


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
