# Handoff to WorkBuddy: learning from explained Slay the Spire 2 runs

Prepared November 25, 2026. Repository: `Steventianjw1/STS` (on the main PC: `C:\Users\steve\OneDrive\文档\STS Agent`).

## Goal

Watch three narrated, "over-explained" runs on YouTube by strong players, and write down how they play and why.
The notes will be used to improve an LLM agent that plays Slay the Spire 2. The most valuable part is the reasoning
the player says out loud: why they took or skipped a card, picked a path, rested or upgraded, bought or removed
something, or played a turn a certain way.

Videos:

1. https://www.youtube.com/watch?v=LKBhc87lAT0
2. https://www.youtube.com/watch?v=p_pImjcVpug
3. https://www.youtube.com/watch?v=aIVoArZ7DxM

The titles, creators, characters and ascensions were not seen yet. Record them from the videos themselves.

## Why this was handed over

A Claude cloud session tried first. Network access to YouTube was allowed, but YouTube answered every request from
the cloud server with "Sign in to confirm you're not a bot", so nothing was downloaded. Run the download from the
user's own computer, where YouTube works normally. Do not try to get around a block like that with spoofed clients
or someone's login cookies.

## Patch note

The videos are on game patch **0.111**. The agent is currently pinned to **v0.107.1**
(`PINNED_GAME_BUILD` in `src/sts_agent/game/game_pin.py`) and will move to 0.111 later. The notes are still useful
now, but mark every point as observed on 0.111. If a player mentions that something changed in a recent patch,
record it separately. Do not guess which cards or relics changed between the two versions.

## What the agent is (enough context for useful notes)

- `README.md` gives the overview. The agent plays through the STS2MCP mod's local HTTP API. For each screen, Python
  gathers facts and legal candidates, the model picks one, and Python checks and sends it.
- Development runs have been mostly **Ironclad at Ascension 10**. Some Silent guidance exists. Other characters are
  untried.
- The model's instructions are assembled per screen type in `src/sts_agent/decision/prompts.py`: card rewards, shop,
  map, rest site, events, card grids (remove/upgrade/transform), ancients, combat, plus deck-balance and elite
  guidance. Character-specific guidance lives in `src/sts_agent/strategic/silent.py`. A strategic assessor keeps a
  standing plan for the deck (`src/sts_agent/strategic/`, described in `docs/STRATEGIC_PLANNER.md`).
- Supporting evidence (expert pick rates, route risk, elite readiness, upgrade priority, boss demands) is computed
  in `src/sts_agent/evidence/`.
- Most fights can be played by a separate simulator-based combat pilot (`docs/PILOT_TUNING.md`), so for combat,
  general habits and principles matter more than turn-by-turn play.

Reading the prompt modules is optional, but it makes the "compared with the agent" section much better.

## Steps

1. Download into a Git-ignored folder in the repository, for example `data/videos/` (`data/` is ignored):

   ```
   yt-dlp -f "bv*[height<=720]+ba/b" --write-subs --write-auto-subs --sub-langs "en.*" --sub-format vtt -o "data/videos/%(id)s.%(ext)s" LKBhc87lAT0 p_pImjcVpug aIVoArZ7DxM
   ```

   Creator-made captions are better than auto-generated ones. Keep both if both exist.
2. Use the transcript as the main source. Use the video to read what the narration does not say: the options shown
   on card rewards, shops, the map, events, ancients and boss relics, and the HP, gold and deck at those moments.
   To get frames for those screens you can use ffmpeg, for example one frame every few seconds, or scene-change
   detection (`-vf "select='gt(scene,0.3)'"`).
3. Write the notes described below. Analysis only: do not change code, prompts or tests in this task.

## Deliverable

Write one Markdown file per video plus a combined summary, in Git-ignored `data/video_notes/`, unless the user names
another place. Ask the user before committing anything.

**Per video**

- Title, creator, link, character, ascension, patch, final result and the floor it ended on.
- A decision log with one row per meaningful decision: timestamp, act/floor, screen (card reward, shop, map,
  rest, event, ancient, boss relic, card removal/upgrade, combat), options available, choice, the reason as stated,
  and HP/gold/deck notes when they matter. Short quotes with timestamps are best.

**Combined summary**

Group the patterns by the agent's screen types so they can be turned into instructions:

- Deck building: what they value early versus late, when they skip, how they count damage, block, draw and scaling,
  and how big they let the deck get.
- Card removal, upgrades and transforms: priorities.
- Routing: elites versus safety, how HP and potions change the plan, when they rest versus upgrade.
- Shops: what they buy, when they remove, how they save gold.
- Events, ancients and relics: how they judge them.
- Boss preparation: what they build toward for each boss they mention.
- Combat habits: principles they state (turn planning, using potions, when to take damage), not every turn.
- Character-specific advice, kept separate per character.
- Disagreements between the players, or a player changing their mind.

If you read the prompt modules, end with **compared with the agent**: where the players' habits match the current
instructions, where they contradict them, and what is missing. Point to the module or file each point concerns.

## Quality rules

- Keep three kinds of evidence apart, and label them: **said** (the player stated it), **seen** (read from the
  screen), and **inferred** (your interpretation). Never present general Slay the Spire knowledge as something the
  player said or did.
- Mark unreadable or uncertain screens as uncertain instead of guessing card names or numbers.
- A rule seen once is an anecdote. Say how many times a pattern appeared and in which videos.
- Note the context a rule depends on (character, act, HP, deck size, relics), since the agent has to know when it
  applies.
