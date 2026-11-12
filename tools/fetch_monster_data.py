"""Download the enemy move-pattern reference used by the combat prompt.

Usage: python tools/fetch_monster_data.py [OUTPUT]   (default data/monster_reference/monsters.json)
Source: spire-mentor (MIT) extraction of STS2 v0.107.1 monster data, pinned to one commit.
The game data is (c) Mega Crit; the output path is git-ignored and must stay local.
"""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

COMMIT = "d3115ef"
URL = f"https://raw.githubusercontent.com/tfriedel/spire-mentor/{COMMIT}/sts2-wiki/data/v0.107.1/monsters.json"


def main(out: Path) -> None:
    with urllib.request.urlopen(URL, timeout=30) as resp:
        body = resp.read()
    records = json.loads(body)
    if not isinstance(records, list) or not all(isinstance(r, dict) and "moves" in r for r in records):
        raise SystemExit("Unexpected monsters.json shape; nothing written.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    print(f"Wrote {len(records)} monsters to {out} (commit {COMMIT}, sha256 {hashlib.sha256(body).hexdigest()[:16]}).")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/monster_reference/monsters.json"))
