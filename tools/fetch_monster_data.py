"""Download the enemy reference used by the prompts: monster move patterns and encounter members.

Usage: python tools/fetch_monster_data.py [OUTPUT]   (default data/monster_reference/monsters.json)
Writes OUTPUT and, beside it, encounters.json (which monsters make up each encounter; a boss on
the map is an encounter and can be several monsters) and powers.json (the game's description of
each power, for the ones monsters start with or apply).
Source: spire-mentor (MIT) extraction of STS2 v0.107.1 data, pinned to one commit.
The game data is (c) Mega Crit; the output folder is git-ignored and must stay local.
"""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

COMMIT = "d3115ef"
BASE = f"https://raw.githubusercontent.com/tfriedel/spire-mentor/{COMMIT}/sts2-wiki/data/v0.107.1/"
# file -> the key every record must carry, as a shape check before anything is written
FILES = {"monsters.json": "moves", "encounters.json": "monsters", "powers.json": "description"}


def fetch(name: str, key: str) -> tuple[bytes, int]:
    with urllib.request.urlopen(BASE + name, timeout=30) as resp:
        body = resp.read()
    records = json.loads(body)
    if not isinstance(records, list) or not all(isinstance(r, dict) and key in r for r in records):
        raise SystemExit(f"Unexpected {name} shape; nothing written.")
    return body, len(records)


def main(out: Path) -> None:
    fetched = {name: fetch(name, key) for name, key in FILES.items()}   # all validated before any write
    out.parent.mkdir(parents=True, exist_ok=True)
    for name, (body, count) in fetched.items():
        path = out if name == "monsters.json" else out.with_name(name)
        path.write_bytes(body)
        print(f"Wrote {count} records to {path} (commit {COMMIT}, sha256 {hashlib.sha256(body).hexdigest()[:16]}).")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/monster_reference/monsters.json"))
