# Tuning the combat pilot on another machine

Instructions for an agent (or person) continuing the per-encounter tuning away from the main PC.

Tuning is simulator-only. It needs no game, no mod install, no API key and no run logs: the decks it
fights with are kept in `src/sts_agent/pilot/snapshots.json` (logged A10 Ironclad fight starts), and
each result is one file, `src/sts_agent/pilot/tuned/<ENCOUNTER_ID>.json`. The live pilot on the main PC
reads that folder at the start of every fight.

## 1. Set up

```
git clone https://github.com/Steventianjw1/STS.git && cd STS
git switch claude/pilot-tuning        # skip once this branch is merged into main
python -m pip install -e .
```

The simulator is Spire Mentor's `sts2_rs` module (tfriedel/spire-mentor, MIT). It is not in this
repository. Put it in `data/engine/` (ignored by Git), or name its folder in `STS_AGENT_ENGINE_DIR`.

- **Windows, Python 3.13 (verified).** Copy the single file `sts2_rs.pyd` from the main PC,
  `data\game\v0.107.1\mods\SpireMentor\SimAdvisor\_internal\sts2_rs\sts2_rs.pyd`, into `data\engine\`.
  It is self-contained: a lone copy in an empty folder gave the same fixed-seed search as the installed mod.
- **Linux or macOS, e.g. a cloud session (not verified, derived from upstream's CI, which builds it on
  Ubuntu).** Build it from the commit that released the installed mod (0.10.6). Needs `rustup` (the
  source pins Rust 1.99.1), a C linker and network access to GitHub, crates.io and PyPI:

  ```
  git init data/mentor_build && cd data/mentor_build
  git fetch --depth 1 https://github.com/tfriedel/spire-mentor.git fe9cde7f9a377d8846e14853bae1f3b3d64cb65b
  git checkout FETCH_HEAD && cd ../..
  python -m pip install --target data/engine ./data/mentor_build/sts2-rs/crates/sts2-py
  ```

Then check it:

```
python -m sts_agent.pilot engine      # the main PC prints: encounters 81, fingerprint 4420085d97ee07b1
python -m sts_agent.pilot scenarios   # 105 fight starts
```

An equal fingerprint means this machine simulates and searches like the mod the live game uses. A
different one on a source build may be floating-point differences or a different engine; say which
fingerprint you got when you hand results back, and do not present them as equivalent.

## 2. Tune

```
python -m sts_agent.pilot tune --tier Boss,Elite --skip-tuned --reverse --fights 24 --rounds 3
```

- `--skip-tuned` leaves out encounters that already have a file, so the command resumes after any stop.
- `--reverse` starts from the last encounter (act 3 bosses). The main PC's batch runs first to last,
  so the two meet in the middle. Pull first to see what is already done.
- `--low-priority` (Windows) keeps a machine usable while it runs. `--encounter A,B` picks by id.
- Time on a 16-thread desktop: about 2 hours per boss, 25 minutes per elite. It scales with cores;
  a 4-core machine is roughly four times slower. On a small machine start with `--tier Elite`.
- Keep `--fights 24 --rounds 3`. Fewer fights do not make a result wrong, but almost nothing will be accepted.

Each encounter compares ten single changes to the default search, plus Mentor's own settings, over
24, 48 and 96 simulated fights. A change is kept only if it beats the default by more than two standard
errors on the last round; otherwise the file records the default. Do not edit the files by hand.

## 3. Hand the results back

After each finished encounter (or at the end), commit only the new files and push your branch:

```
git add src/sts_agent/pilot/tuned
git commit -m "Tune <encounters> in the simulator"
git push -u origin HEAD
```

On the main PC: `git fetch origin` then `git merge origin/<your branch>`. Files for different encounters
never conflict. If both machines tuned the same encounter, keep either file. The main PC's running batch
and the live pilot pick the merged files up by themselves.

Report results as simulator evidence, with the `chosen`, `versus_default` and `versus_mentor` numbers
each file holds.
