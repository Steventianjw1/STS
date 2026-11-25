"""Download the source of Spire Mentor's advisor that the combat pilot runs.

Usage: python tools/fetch_mentor_source.py [OUTPUT] [--build BUILD]   (default data/mentor_source)
Source: tfriedel/spire-mentor (MIT), pinned to the commit of the Mentor release that goes with
the game build (src/sts_agent/game/builds.py: 0.10.6 for v0.107.1, 0.11.8 for v0.111), so the
source matches the engine that mod release ships; only the advisor's Python (sts2-pycli/python
and the three packages it imports) is checked out. The output path is git-ignored.
`[autobattle] pilot = true` runs this code in place of Mentor's packaged advisor, against the
engine the installed mod ships (see src/sts_agent/pilot/advisor.py). Fetch again after
changing the game build: the pilot refuses a checkout of another release.
"""
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sts_agent.game import builds  # noqa: E402

REPO = "https://github.com/tfriedel/spire-mentor.git"
PATHS = ["/LICENSE", "/NOTICE", "/sts2-pycli/python/*.py", "/sts2-pycli/python/sts2_hint/*",
         "/sts2-pycli/python/workbench/*.py", "/sts2-pycli/python/ppo/*.py"]


def git(out: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(out), *args], check=True, capture_output=True, text=True).stdout.strip()


def main(out: Path, build=None) -> None:
    release = builds.get(build)
    COMMIT = release.mentor_commit
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
    print(f"Mentor {release.mentor_release} advisor source for game build {release.key} at {out} (commit {COMMIT[:12]}).")


if __name__ == "__main__":
    args = sys.argv[1:]
    chosen = args.pop(args.index("--build") + 1) if "--build" in args else None
    args = [a for a in args if a != "--build"]
    main(Path(args[0] if args else "data/mentor_source"), chosen)
