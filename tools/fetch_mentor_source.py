"""Download the source of Spire Mentor's advisor that the combat pilot runs.

Usage: python tools/fetch_mentor_source.py [OUTPUT]   (default data/mentor_source)
Source: tfriedel/spire-mentor (MIT), pinned to the commit that released the installed mod
version (0.10.6), so the source matches the engine the mod ships; only the advisor's Python
(sts2-pycli/python and the three packages it imports) is checked out. The output path
is git-ignored. `[autobattle] pilot = true` runs this code in place of Mentor's packaged
advisor, against the engine the installed mod ships (see src/sts_agent/pilot/advisor.py).
"""
from pathlib import Path
import shutil
import subprocess
import sys

REPO = "https://github.com/tfriedel/spire-mentor.git"
COMMIT = "fe9cde7f9a377d8846e14853bae1f3b3d64cb65b"
PATHS = ["/LICENSE", "/NOTICE", "/sts2-pycli/python/*.py", "/sts2-pycli/python/sts2_hint/*",
         "/sts2-pycli/python/workbench/*.py", "/sts2-pycli/python/ppo/*.py"]


def git(out: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(out), *args], check=True, capture_output=True, text=True).stdout.strip()


def main(out: Path) -> None:
    if out.exists():
        if not (out / ".git").is_dir():
            raise SystemExit(f"{out} exists and is not a checkout this script made; nothing changed.")
        shutil.rmtree(out, onerror=lambda fn, path, _: (Path(path).chmod(0o700), fn(path)))
    out.mkdir(parents=True)
    git(out, "init", "-q")
    git(out, "remote", "add", "origin", REPO)
    git(out, "sparse-checkout", "set", "--no-cone", *PATHS)
    git(out, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", COMMIT)
    git(out, "checkout", "-q", "FETCH_HEAD")
    head = git(out, "rev-parse", "HEAD")
    if head != COMMIT or not (out / "sts2-pycli" / "python" / "realgame_advisor.py").is_file():
        raise SystemExit(f"Unexpected checkout ({head}); do not use {out}.")
    print(f"Mentor advisor source at {out} (commit {COMMIT[:12]}).")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/mentor_source"))
