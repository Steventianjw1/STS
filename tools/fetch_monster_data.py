"""Download the enemy move-pattern and keyword references used by the prompts.

Usage: python tools/fetch_monster_data.py [MONSTERS_OUTPUT [KEYWORD_DIR]]
       (defaults data/monster_reference/monsters.json and data/keyword_reference/, which receives
       powers.json and curse_cards.json: the curse and status cards cut out of cards.json)
Source: spire-mentor (MIT) extraction of STS2 v0.107.1 data, pinned to one commit.
The game data is (c) Mega Crit; the output paths are git-ignored and must stay local.
"""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

COMMIT = "d3115ef"
BASE = f"https://raw.githubusercontent.com/tfriedel/spire-mentor/{COMMIT}/sts2-wiki/data/v0.107.1/"


def fetch(name: str, out: Path, required_key: str, keep=None) -> None:
    with urllib.request.urlopen(BASE + name, timeout=60) as resp:
        body = resp.read()
    records = json.loads(body)
    if not isinstance(records, list) or not all(isinstance(r, dict) and required_key in r for r in records):
        raise SystemExit(f"Unexpected {name} shape; nothing written.")
    if keep is not None:
        records = [{k: r.get(k) for k in ("title", "type", "keywords", "description_plain")}
                   for r in records if keep(r)]
        body = json.dumps(records, ensure_ascii=False, indent=1).encode("utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    print(f"Wrote {len(records)} records to {out} (commit {COMMIT}, sha256 {hashlib.sha256(body).hexdigest()[:16]}).")


if __name__ == "__main__":
    args = sys.argv[1:]
    fetch("monsters.json", Path(args[0] if args else "data/monster_reference/monsters.json"), "moves")
    keyword_dir = Path(args[1] if len(args) > 1 else "data/keyword_reference")
    fetch("powers.json", keyword_dir / "powers.json", "title")
    fetch("cards.json", keyword_dir / "curse_cards.json", "title", keep=lambda r: r.get("type") in ("Curse", "Status"))
