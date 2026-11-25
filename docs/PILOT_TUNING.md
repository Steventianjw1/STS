# Tuning the combat pilot and the deck plans away from the main PC

Instructions for an agent (or person) continuing the tuning on another machine. Three things are tuned
this way:

- the pilot's search settings per encounter, on the logged Ironclad fight starts (sections 2 and 3);
- the pilot for the other four characters (section 4);
- each character's deck plan, which the strategic planner and every deck decision read (sections 5 and 6).

Sections 1 to 5 are simulator-only. They need no game, no mod install, no model API key and no run logs.
Section 6 reads public Spire Codex statistics over the network and calls no game or model either.
Nothing here is a live result: report everything as simulator evidence or as public player statistics.

## 1. Set up

```
git clone https://github.com/Steventianjw1/STS.git && cd STS
python -m pip install -e .
```

The simulator is Spire Mentor's `sts2_rs` module (tfriedel/spire-mentor, MIT). It is not in this
repository. Put it in `data/engine/` (ignored by Git), or name its folder in `STS_AGENT_ENGINE_DIR`.

- **Windows, Python 3.13 (verified).** Copy the single file `sts2_rs.pyd` from the main PC,
  `data\game\v0.107.1\mods\SpireMentor\SimAdvisor\_internal\sts2_rs\sts2_rs.pyd`, into `data\engine\`.
  It is self-contained: a lone copy in an empty folder gave the same fixed-seed search as the installed mod.
- **Linux (verified in a cloud session, 2026-11-25) or macOS (not verified).** Build it from the commit
  that released the installed mod (0.10.6). Needs `rustup` (the source pins Rust 1.99.1), a C linker and
  network access to GitHub, crates.io and PyPI:

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

An equal fingerprint means this machine simulates and searches like the mod the live game uses. The Linux
source build prints `cc2c06eb91b21a2a`: say which fingerprint you got when you hand results back, and do
not present results from different fingerprints as equivalent.

Whole runs and the players' statistics (sections 5 and 6) also need a full checkout of Spire Mentor's
source in `data/mentor_build`, for its run-policy nets and card text. The Linux steps above leave one there. On Windows run the three
`git` lines without the `pip` line.

## 2. Tune the pilot per encounter (Ironclad)

```
python -m sts_agent.pilot tune --tier Boss,Elite --skip-tuned --fights 24 --rounds 3
```

- The fight starts are logged A10 Ironclad fights kept in `src/sts_agent/pilot/snapshots.json`. Each result
  is one file, `src/sts_agent/pilot/tuned/<ENCOUNTER_ID>.json`, which the live pilot reads at the start of
  every fight.
- The main PC tuned Kaiser Crab and then stopped (2026-11-20): the other 11 bosses and 12 elites are open.
  Pull first, so Kaiser Crab's file is there and is skipped.
- `--skip-tuned` leaves out encounters that already have a file, so the command resumes after any stop.
- The order is act 1 first (the fights every run meets), elites before bosses within each area.
  `--reverse` takes them last first, for when two machines share the work and meet in the middle.
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
never conflict. If both machines tuned the same encounter, keep either file. The live pilot picks the
merged files up by itself at its next fight.

Report results as simulator evidence, with the `chosen`, `versus_default` and `versus_mentor` numbers
each file holds.

## 4. The pilot for the other four characters

The Silent, Defect, Regent and Necrobinder have no logged runs. Their fight starts come from simulated
runs (section 5) and are kept in `src/sts_agent/pilot/snapshots_<character>.json`: 104 each over acts 1
and 2 (24 boss, 32 elite, 32 hallway and 16 easy-pool fights). `scenarios`, `show`, `bench`, `tune` and
`calibrate` take `--character` (Ironclad when left out):

```
python -m sts_agent.pilot scenarios --character SILENT
python -m sts_agent.pilot bench --character SILENT --tier Elite --limit 32 --worlds 1 --arms mentor,pilot --out traces/pilot/silent.jsonl
python -m sts_agent.pilot bench --character SILENT --tier Normal --limit 32 --worlds 1 --arms mentor,pilot --out traces/pilot/silent.jsonl
python -m sts_agent.pilot report traces/pilot/silent.jsonl
python -m sts_agent.pilot bench --character SILENT --tier Elite,Normal --limit 48 --worlds 1 --arms pilot --hp-scale 0.5 --out traces/pilot/silent_half.jsonl
python -m sts_agent.pilot calibrate traces/pilot/silent.jsonl traces/pilot/silent_half.jsonl --character SILENT
python -m sts_agent.pilot tune --character SILENT --tier Elite --whole-tier --knobs leaf-playouts-3,dominance --fights 32 --rounds 1 --snapshots 32
```

- `bench --arms mentor,pilot` fights every start with Mentor's search and with the pilot, on the same
  draws. `--hp-scale 0.5` enters the same fights at half HP, so that `calibrate` sees fights in doubt.
- `calibrate` fits the character's win-chance reading (what decides when a potion is held) and checks
  Ironclad's reading against the character's own fights. It keeps a reading of the character's own
  (`--save`) only when Ironclad's is unsafe for it and the fit is the more cautious one.
- `tune --whole-tier` compares knobs for a whole tier on the character's fight starts as they are and
  writes `src/sts_agent/pilot/tuned/<CHARACTER>/_character.json` (knobs per tier, the win-chance reading,
  and the evidence). A knob chosen on 32 fights is fought again on fresh ones before it is believed:
  `bench --character X --tier Elite --limit 32 --worlds 2 --arms pilot@mentor,<knob>`.
- Per-encounter tuning works as in section 2 with `--character X`; its files go to
  `src/sts_agent/pilot/tuned/<CHARACTER>/`.

What was measured (2026-11-25, fingerprint `cc2c06eb91b21a2a`, A10, paired fights; each cell is Mentor's
search → the pilot, HP is the pilot's HP at the end of the fight minus Mentor's):

| Character | Elites: fights, wins, HP, potions used | Hallway (32 fights): wins, HP, potions used |
| --- | --- | --- |
| Ironclad | 18, 100% → 100%, −6.7 ±2.7, 0.61 → 0.44 | 100% → 100%, −0.8 ±1.0, 0.62 → 0.06 |
| Silent | 32, 84.4% → 84.4%, −0.7 ±1.6, 0.53 → 0.25 | 93.8% → 96.9%, −1.0 ±1.5, 0.72 → 0.16 |
| Necrobinder | 32, 81.2% → 84.4%, +0.4 ±1.6, 0.72 → 0.50 | 93.8% → 96.9%, −2.2 ±1.1, 0.59 → 0.28 |
| Regent | 32, 87.5% → 87.5%, −5.4 ±1.6, 0.84 → 0.41 | 100% → 100%, −2.2 ±1.1, 0.53 → 0.03 |
| Defect | 32, 100% → 100%, −1.8 ±0.8, 0.53 → 0.12 | 100% → 100%, −0.3 ±0.9, 0.38 → 0.03 |

- The pilot won every fight Mentor's search won and three more. The HP gap is the potion hold: with an
  empty belt the pilot is level at elites (−0.2 ±0.9 HP over 48 fights), with potions it ends 3.8 ±1.1 HP
  lower and keeps 0.5 more potions a fight, 7.6 HP per potion kept (7.9 on Ironclad's logged fights).
- Ironclad's potion-hold line is safe for all five, so no character has a reading of its own.
- No knob was kept. `leaf-playouts-3` won the Silent's 32 elite fights by 2.0 standard errors and lost its
  lead on 64 fresh ones (+0.0021 ±0.0509 at twice the thinking time).
- Open: bosses and per-encounter tuning for the four characters, and fight starts beyond act 2.

## 5. Deck plans: whole simulated runs

A character's deck plan is one `DeckPlan` in `src/sts_agent/evidence/deck_balance.py` (`PLANS`): what the
first picks go to, when a role (damage, Block, the scaling of each) or one of the character's tracks
(Weak, Frost, Stars, Osty) counts as short or over-supplied, and whether the deck is kept thin. The
planner prompt, every card decision and the evidence text read the same plan, so changing a number there
changes what the agent is told. Ironclad's plan is also what a character without one gets.

`pilot.runs` plays whole runs to compare plans. Spire Mentor's run-policy net of the character makes the
choices between fights and the simulator's own small search plays the fights. An arm says who decides the
card rewards:

| Arm | Who drafts |
| --- | --- |
| `net` | the net alone: the reference |
| `shared` | the net's preferences inside Ironclad's plan |
| `character` | the same inside the character's own plan |
| `character:field=value;...` | a variant of that plan, for choosing its numbers (`shared:...` likewise) |

```
python -m sts_agent.pilot runs --characters SILENT,DEFECT --arms net,shared,character --runs 80 --sims 400 --boss-sims 1600 --stop-after-bosses 2 --out traces/runs/plans.jsonl
python -m sts_agent.pilot runs --characters NECROBINDER --arms 'character:caps.damage=4;targets.weak_min=2' --runs 80 --sims 400 --boss-sims 1600 --stop-after-bosses 2 --out traces/runs/plans.jsonl
python -m sts_agent.pilot runs-report traces/runs/plans.jsonl --arms shared,character,net
python -m sts_agent.pilot fight-starts traces/runs/plans.jsonl --arm net --save
```

- Every arm plays the same seeds, so arms are compared seed by seed: `runs-report` prints each arm and
  every paired difference with its standard error. Treat a difference under two standard errors as none.
- The file is appended to and never replays a run it holds, so a batch resumes, and a changed plan needs a
  new arm name or a new file.
- A variant changes plan fields by name: `scaling_full=4`, `early_picks=2`, `thin_from_act=null` (never
  thin), `early_roles=block+damage` (lists are joined with `+`), `targets.frost_min=3`,
  `caps.damage=[5,7,9]` (one number per act), `removals_from_act=null` (never first), `deck_targets.2=26`.
- The protocol used so far: A10, 80 seeds, `--sims 400 --boss-sims 1600 --stop-after-bosses 2` (a run ends
  at the door of act 3). That is about 13 runs a minute on 4 cores.
- `fight-starts --save` keeps a character's fight starts for section 4. It refuses to overwrite
  Ironclad's logged ones.

What the stand-in is and is not: it flattens the net's own probabilities and multiplies them by what the
plan says of each offer (a card for the first priority four times, one that fills a need twice, one that
worsens the balance never), takes a card for the plan's first roles whenever one is offered in the first
picks, and buys the shop's removal first when the plan says so. It is not the language model, the net it
leans on has tastes of its own (it drafts Poison for the Silent where top players draft Shivs), and the
fights are searched far less than the pilot searches them. So these runs rank plans against each other
under one fixed drafter. They do not say how far the agent gets.

First round (2026-11-25, fingerprint `cc2c06eb91b21a2a`): average floor reached / act 1 boss beaten / act
2 boss beaten, and the character's first own plan against the shared one.

| Character | `net` | `shared` | first own plan | own − shared: floor; act 2 boss |
| --- | --- | --- | --- | --- |
| Ironclad | 26.7 / 82.5% / 46.3% | 25.9 / 77.5% / 46.3% | the shared plan | n/a |
| Silent | 19.9 / 50.0% / 17.5% | 19.4 / 45.0% / 16.3% | 19.4 / 45.0% / 15.0% | +0.04 ±0.71; −1.3 ±2.8 points |
| Necrobinder | 20.2 / 52.5% / 16.3% | 19.3 / 48.7% / 8.7% | 19.2 / 48.7% / 5.0% | −0.06 ±0.68; −3.7 ±2.8 points |
| Regent | 22.5 / 66.2% / 22.5% | 22.6 / 65.0% / 11.3% | 22.3 / 62.5% / 20.0% | −0.28 ±0.43; +8.7 ±4.1 points |
| Defect | 26.0 / 80.0% / 45.0% | 25.4 / 76.2% / 31.2% | 25.8 / 76.2% / 36.2% | +0.42 ±0.71; +5.0 ±4.3 points |

No plan beat the net alone. Holding the Necrobinder's deck to 18 cards from the first picks cost the act 1
boss (−1.69 ±0.75 floors, −11.3 ±5.6 points).

Second round (2026-11-25, same fingerprint, seeds and protocol), after the Silent's, Defect's and
Necrobinder's plans were refitted to top players' decks (section 6):

| Character | `net` | `shared` | first own plan | refitted plan | refitted − first own: floor; act 1 boss; act 2 boss | refitted − `net` |
| --- | --- | --- | --- | --- | --- | --- |
| Silent | 19.9 / 50.0% / 17.5% | 19.4 / 45.0% / 16.3% | 19.4 / 45.0% / 15.0% | 19.3 / 45.0% / 17.5% | −0.12 ±0.17; +0.0 ±0.0; +2.5 ±3.1 points | −0.69 ±0.69; −5.0 ±4.7; +0.0 ±3.1 |
| Defect | 26.0 / 80.0% / 45.0% | 25.4 / 76.2% / 31.2% | 25.8 / 76.2% / 36.2% | 25.2 / 80.0% / 40.0% | −0.61 ±0.84; +3.7 ±4.5; +3.7 ±5.5 points | −0.80 ±0.52; +0.0 ±3.1; −5.0 ±5.9 |
| Necrobinder | 20.2 / 52.5% / 16.3% | 19.3 / 48.7% / 8.7% | 19.2 / 48.7% / 5.0% | 20.9 / 57.5% / 10.0% | +1.68 ±0.86; +8.7 ±5.1; +5.0 ±3.0 points | +0.65 ±0.61; +5.0 ±3.9; −6.2 ±4.1 |

- The Necrobinder's refitted plan is the stronger one: it also beats the shared plan by 1.61 ±0.65 floors
  and is the first plan arm level with the net on floors. The Silent's and Defect's are level with their
  first plans. They are kept because they read decks the way top players build them (section 6), not
  because the simulator prefers them. No plan beats the net at the act 2 boss.
- The Silent's refitted plan drafts the same act 1 as its first plan under this stand-in (the act 1 boss
  results are identical on all 80 seeds), so the simulator cannot tell them apart there.
- Buying the shop's removal first in every act cost wins: against not buying it first, the Silent lost
  0.71 ±0.46 floors and 6.2 ±2.7 points at the act 2 boss; against buying it from act 2, the Necrobinder
  lost 1.38 ±0.71 floors and 10.0 ±4.6 points at the act 1 boss. The plans now put the removal first from
  act 2 (`removals_from_act=2`), where the players make most of theirs.
- The Defect's first picks: Block alone lost to Block and damage (−0.50 ±0.49 floors, −6.2 ±3.3 points at
  the act 1 boss).
- A tighter damage cap (4 damage cards in every act for the Silent and Necrobinder, 6 for the Defect)
  measured the same as the caps kept.

The second round's 1040 runs are in `traces/runs/plans2.jsonl` of the machine that played them (ignored by
Git), with the first round's arms copied in as `net`, `shared` and `previous`.

## 6. Deck plans: what top players' decks hold

```
python -m sts_agent.pilot players --character SILENT --words Shiv,Poison
python -m sts_agent.pilot players --character NECROBINDER --words Doom,Osty --cards --arms shared,character,'character:caps.damage=4'
python -m sts_agent.pilot players --character DEFECT --offline
```

`players` reads the solo A10 standard runs that the character's highest-rated Spire Codex accounts
submitted on the pinned game build and the one before it (`v0.107.1`, `v0.107.0`: the same cards), and
counts them with the deck plans' own rules:

- the deck that won runs carried into each act boss: size, cards by role and track, starter cards left;
- per act: card rewards taken, removals and what was removed, elites fought and the HP each cost;
- how often a card of a kind was taken when offered, by how many of that kind the deck already held, and
  how often a reward was taken at each deck size;
- for every plan named in `--arms`: how it labels each offer (priority, improves, neutral, worsens) and
  how often the players took a card under each label. A plan that reads decks the way these players build
  them has them take what it calls worsening rarely and what it puts first often.

`--words` counts cards whose text names a word as a kind of their own; `--cards` lists every card's pick
rate. It asks `spire-codex.com` through the agent's own client (12 accounts, up to 30 runs each: about
400 requests a character, a few minutes) and keeps the answers in `traces/spire_codex_cache`, so
`--offline` and every later call read the same runs. Card text comes from the simulator's catalog, so it
needs the engine and `data/mentor_build`.

What it found (read 2026-11-25; the decks are those entering the act 1 / 2 / 3 boss in won runs):

| | Silent | Defect | Necrobinder |
| --- | --- | --- | --- |
| Runs, won, accounts | 166, 142, 12 | 133, 96, 12 | 137, 104, 12 |
| Deck size | 22 / 29 / 37 | 19 / 27 / 35 | 19 / 25 / 32 |
| Card rewards taken | 86% / 72% / 60% | 89% / 73% / 62% | 84% / 65% / 54% |
| Removals in the act | 0.5 / 1.8 / 1.2 | 0.4 / 1.8 / 0.9 | 0.7 / 2.0 / 1.5 |
| Strikes among act 1 removals | 75% | 71% | 82% |
| Damage cards added | 3.8 / 6.0 / 7.3 | 4.3 / 6.8 / 8.8 | 3.8 / 5.8 / 7.4 |
| Block cards added | 2.5 / 4.5 / 6.7 | 3.9 / 6.9 / 8.8 | 3.2 / 5.7 / 7.7 |
| Damage scaling, Block scaling | 1.3, 0.6 / 2.4, 1.4 / 3.6, 2.0 | 0.8, 0.5 / 1.9, 1.3 / 3.2, 2.2 | 0.7, 0.2 / 1.6, 0.7 / 2.3, 0.9 |
| No role (draw, energy, debuffs) | 2.5 / 5.2 / 8.2 | 1.8 / 4.5 / 7.3 | 1.7 / 3.8 / 6.2 |
| Weak sources, starter's included | 1.7 / 2.7 / 3.5 | 0.8 / 1.2 / 1.5 | 1.6 / 2.7 / 3.7 |
| Frost cards | | 1.4 / 2.3 / 2.9 | |
| Elites fought, HP lost to each | 2.4, 15% / 2.2, 19% / 1.8, 12% | 2.4, 17% / 2.1, 19% / 1.9, 13% | 2.2, 14% / 2.0, 20% / 1.7, 12% |

Pick rate when offered, by how many of the kind the deck held (0, 1, 2, 3, 4 and more):

| Kind | Silent | Defect | Necrobinder |
| --- | --- | --- | --- |
| Block scaling | 80%, 65%, 67% | 44%, 37%, 39% | 28%, 26% |
| Damage scaling | 37%, 46%, 32%, 26%, 19% | 26%, 26%, 23%, 25% | 43%, 31%, 26%, 21% |
| Block | 39%, 31%, 30%, 32%, about 27% | 44%, 47%, 46%, 37%, about 30% | 47%, 43%, 32%, 27%, about 28% |
| Damage | 33%, 27%, 19%, 13%, about 9% | 34%, 30%, 23%, 18%, about 14% | 25%, 22%, 15%, 14%, about 9% |
| An attack that only deals damage, by act | 17% / 5% / 2% | 20% / 14% / 11% | 16% / 7% / 5% |
| Weak (the count includes the starter's) | –, 34%, 26%, 26% | 65%, 21%, 7% | 82%, 62%, 64%, 57%, 54% |
| Frost | | 51%, 46%, 32%, 40%, 18% | |
| Summon (the count includes the starter's) | | | –, 20%, 23%, 21% |
| No role | about 33% at any count | about 33% at any count | about 40% at any count |

A reward was taken as often with a large deck as with a small one in every act (Necrobinder, act 2: 62%
with 16 to 19 cards, 64% with 20 to 23, 59% with 24 to 27, 63% with 28 to 31). The players get the smaller
Necrobinder deck from removals and from refusing attacks, not from a size limit.

How the three plans follow from it:

- No thin phase: a card that fills nothing is still taken in every act (`thin_from_act=None`).
- Block is never over-supplied. Damage is, once the deck holds more damage cards than the players' decks
  did at that act's boss: `caps={"damage": (5, 7, 9)}` for the Silent and Necrobinder, `(6, 8, 10)` for the
  Defect.
- Silent: Block scaling and damage scaling lead and stay short below two sources; scaling is full at four;
  the Weak track asks for two sources, three past 30 cards.
- Defect: Frost first (two cards, three past 24, over-supplied from four), one Weak source, Block scaling
  short below two sources; the first picks go to Block and damage, Frost leading while it is short.
- Necrobinder: the Weak track leads (three sources, four past 24 cards, five past 32); no deck-size
  target and no Summon target; the first picks go to Weak and Block.
- All three put the shop's removal first from act 2 (section 5 has the simulated runs that moved it out
  of act 1).

How each plan's labels match the picks, as `players` prints it (taken when offered, under the label):

| Plan | priority | improves | neutral | neutral while thin | worsens | Cards taken that the plan calls worsening |
| --- | --- | --- | --- | --- | --- | --- |
| Silent, shared | 30% | 34% | 30% | 26% | 14% | 20% |
| Silent, first own | 29% | 33% | 30% | 26% | 13% | 16% |
| Silent, now | 33% | 33% | 27% | | 11% | 12% |
| Defect, shared | 26% | 28% | 30% | 27% | 20% | 21% |
| Defect, first own | 31% | 24% | 28% | 27% | 19% | 16% |
| Defect, now | 38% | 35% | 25% | | 14% | 8% |
| Necrobinder, shared | 23% | 28% | 28% | 24% | 16% | 20% |
| Necrobinder, first own | 22% | 29% | 28% | 26% | 19% | 35% |
| Necrobinder, now | 58% | 30% | 21% | | 12% | 9% |

Limits. These are a dozen very strong accounts per character, not a sample of all players, and what they
take depends on what they were offered, on their relics and on how well they play the deck: a pick rate
is an observation, not a cause. The role rules count cards, not strength. The Spire Codex guides (builds
guide, the three tier lists, the two Necrobinder guides) were read for the build names in each
character's guidance text; they describe later patches and all game modes, and where they disagree with
these runs (a discard-first Silent, Poison after its rework) the text follows the runs.

## 7. Report what you changed

A plan number is changed in `PLANS` with the evidence in the comment above it: the `players` numbers it
follows and the `runs` comparison it was checked with, each with its sample and standard error. Bump the
character's `PROMPT_VERSION` (`src/sts_agent/strategic/<character>.py`) when its guidance text changes
and `DECISION_PROMPT_VERSION` when a decision module changes. Say plainly which results are simulator
runs under the stand-in drafter and which are public player statistics, and that neither is a live run.
