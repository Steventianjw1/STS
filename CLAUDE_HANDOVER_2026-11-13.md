# Claude handover: Ironclad A10 run comparison

Prepared November 13, 2026, Asia/Shanghai. Repository: `C:\Users\steve\OneDrive\文档\STS Agent`.

## Brief and scope

Analyze why the latest run lost, compare it with the previous completed run, and prioritize concrete improvements to the agent and its integrations. This handover contains analysis only; no gameplay, implementation changes, or new tests were performed while preparing it. Conclusions below distinguish recorded live observations, present source behavior, and unproven counterfactuals.

There are multiple controller sessions per game run. Group by `run_id`, not trace directory or process start. To resolve “previous run” without silently dropping evidence, this document covers:

1. **Latest completed run:** `modded:profile1:1794491679`, November 12 evening. Initial session stopped at a model timeout; the same game resumed and eventually died to Knowledge Demon at Act 2 floor 33.
2. **Previous completed run:** `modded:profile1:1794412377`, November 11–12 overnight. Eight sessions; died to Infested Prism at Act 2 floor 24.
3. **Immediately preceding substantial but incomplete run:** `modded:profile1:1794488459`, November 12 evening. Stopped at floor 6 on a configured session time limit. It is not a defeat or a win.

These are different seeds, decks, policies, and code/prompt versions. Reaching floor 33 rather than 24 is descriptive, not evidence that a particular change improved win rate.

## Read these artifacts first

All paths below are relative to the repository unless marked otherwise. Traces and saved run state are local and Git-ignored; a remote checkout alone will not contain them. The key observations are embedded here so this Markdown can be handed over independently.

| Label | Artifact | Meaning |
| --- | --- | --- |
| L1 | `traces/20261112T135441Z-7f30f5/` | Latest run, from Ancient through floor-30 shop; decision prompt 14 |
| L0 | `traces/20261112T154121Z-73b379/` | Failed same-run restart: sandbox transport block; zero gameplay actions |
| L2 | `traces/20261112T154207Z-5a01ab/` | Successful resume, shop through death; decision prompt 16 |
| Console | `traces/resume-20261112T154204Z.out.log` | Human-readable successful-resume log |
| State | `traces/run_state/modded_profile1_1794491679.json` | Latest run's final persisted memory; no longer the pre-resume snapshot |
| P | `traces/20261112T130103Z-5be0e7/` | Intervening floor-6 partial run; decision prompt 12 |
| O | `traces/merged-live-20261111/` | Eight sessions of the previous completed run |
| Prior analysis | `traces/ironclad-a10-run-analysis-2026-11-12.md` | Earlier detailed report; rechecked major counts and cited decisions against original JSONL |

Each main session directory contains `manifest.json`, `decisions.jsonl`, and `debug_raw.jsonl`. Use `llm_decision.seq` together with the session label: sequence numbers restart between sessions. Multiple shop-plan outcomes can share one sequence number.

Additional native Spire Mentor logs were inspected at **`%LOCALAPPDATA%\SpireMentor\logs\`**, outside the repository:

- `20261112_234435_Ironclad_ZTEGT0N5U6.jsonl`: latest floor-31 Decimillipede elite; full states and actions.
- `20261112_234733_Ironclad_ZTEGT0N5U6.jsonl`: latest floor-33 Knowledge Demon boss; full states and actions.
- `realgame_advisor/advisor-20261112-154435.jsonl`: elite advice, including initial request `a337`.
- `realgame_advisor/advisor-20261112-154733.jsonl`: boss advice, including requests `a360`, `a364`, `a385`, `a387`, and `a389`.

Native fight-log filenames use local time; advisor filenames and controller session IDs use UTC. Join native actions to advice with `advice_request_id` / `request_id` and position/state IDs. Do not assume adjacent records or equal step numbers identify the same decision; some initial advice is only in the separate advisor log.

Previous-completed-run session labels used below:

| Label | Directory under `traces/merged-live-20261111/` |
| --- | --- |
| O1 | `20261111T155303Z-efa35a` |
| O2 | `20261111T155517Z-b03d94` |
| O3 | `20261111T161907Z-23cd21` |
| O4 | `20261111T165459Z-5b0ac6` |
| O5 | `20261111T170601Z-df1738` |
| O6 | `20261111T170923Z-91456e` |
| O7 | `20261111T171949Z-153c2e` |
| O8 | `20261111T180353Z-b77b33` |

## Results and comparability

| Measure | Latest completed run, L1 + L2 | Previous completed run, O1–O8 | Partial run P |
| --- | --- | --- | --- |
| Character / difficulty / build | Ironclad A10, pinned v0.107.1 | Ironclad A10, v0.107.1 | Ironclad A10 |
| Observed outcome | Defeat, Knowledge Demon, A2 F33, round 6 | Defeat, Infested Prism, A2 F24, round 3 | Stopped alive, A1 F6 rewards, 56/80 HP |
| Combat controller | Native Spire Mentor Autobattle | DeepSeek chooses combat actions through Python | Native Spire Mentor Autobattle |
| Combat rooms | 14: 13 victories, 1 defeat | 11: 10 victories, 1 defeat; an additional false victory event exists | 3 victories |
| Controller decision records | 148: 126 LLM records and 22 automatic records; includes one failed LLM choice | 382 LLM decision records | 25 LLM decision records |
| Controller gameplay actions sent / resolved | 162 / 162; excludes actions executed internally by Mentor | 371 dispatched; 367 visibly resolved, 3 accepted/unobservable, 1 no visible change | 25 / 25 |
| Stale before dispatch / rejections | 0 / 0 | 8 stale before dispatch; see original records for other stops | 0 / 0 |
| Recorded model calls | 205 | 513 | 54 |
| Input / output tokens | 1,866,227 / 592,819 | 3,635,660 / 1,344,362 | 390,245 / 184,014 |
| Strategic update records | 68: 67 updated, 1 failed | 119: 88 updated, 31 failed | 25: all updated |

Latest totals exclude L0's two failed transport calls, zero tokens reported, and zero gameplay actions. L1/L2 controller execution time totals approximately 71 minutes; wall time from 21:54:41 to 23:48:11 includes the long pause. The previous completed run spans November 11 at 23:53:03 through November 12 at 02:29:48. Native combat actions are absent from the latest run's `actions_sent` total, so its action count is not a direct comparison with the older LLM combat count.

Provider was DeepSeek `deepseek-flash`. L1/L2 used 16,384 output tokens, a 60-second SDK timeout, no explicit reasoning effort, and zero SDK retries. Older sessions changed settings during play: initially 4,096 tokens / 60 seconds, later 16,384 / 180 seconds, and then explicit `low` reasoning effort. Do not treat either run as a fixed-configuration experiment.

## Latest run: consequential decisions and observed outcome

| Point | Recorded action and implication |
| --- | --- |
| L1 #1, A1 F1 | Took Cursed Pearl for 333 gold. Rationale planned to remove Greed at the first shop; later observations identify Greed as Eternal. Review legality before attributing removal value to that offer. |
| L1 #5, #8–15, A1 F2–3 | Drafted Second Wind; bought Bag of Preparation (177g), Unrelenting (78g), True Grit (51g), another Second Wind (38g), and removal (100g). Removed a Strike and left with 2g. Greed remained. |
| L1 #43–70, A1 F11–16 | Smithed Bash, Armaments, and True Grit. Won elites at F12 and F15 with recorded net HP losses of 1 and 2. Entered the boss at 61/87 HP; won after 8 turns with net HP loss 24. |
| L1 #76, A2 F18 | Chose Archaic Tooth: Bash+ became Break+, 30 damage and 7 Vulnerable, supporting Bully and Molten Fist. |
| L1 #89–100, A2 F21 | Bought Orrery for 186g and took all five resulting cards: Uppercut, Forgotten Ritual, a third Second Wind, another Uppercut, and Inferno. Deck reached 27 cards. Review each against skip and the combined effect on consistency. |
| L1 #119, A2 F25 | Added Rupture for Inferno synergy. This requires drawing and paying for both powers; the rationale is plausible but the synergy is not an immediate defense guarantee. |
| L1 #128–130, A2 F28 | At 56/87 HP, upgraded the second Uppercut rather than resting for 26 HP. Weak is real defensive value; whether healing would have changed the later outcome is unproven. |
| L1 #134–136, A2 F29 | Added Thunderclap, then selected Shop → Elite → Rest → Boss. Shop branch projected higher boss-arrival HP than the direct elite branch. Both visible choices involved an elite; do not invent an available elite-free exit at F29. |
| L1 #137–138, A2 F30 | Bought Shrug It Off for 25g, leaving 250g. Next strategic call and action call both timed out. Session ended `model_failed` at 61/87 HP; all 149 previous controller actions had resolved. |
| L2 #1, same shop | Restored the saved route plan and encounter history. Complete shop plan bought Blood Potion (51g), Burning Sticks (191g), then left with 8g. All three actions resolved under one model decision. |
| L2 #2, A2 F31 | Sole map destination was Decimillipede. Combat started at 63 HP after Blood Vial, ended at 27 HP after Burning Blood. Native log confirms Blood Potion healed 57 → 74 during round 3. |
| L2 #3–7, F31–32 | Took Bowler Hat and Drum of Battle; deck grew from 31 to 32. Reached rest at 27/87 HP. Recorded route projection had expected 58.1% arrival HP; actual was 31.0%, a 27.1 percentage-point shortfall. |
| L2 #8–10, F32–33 | Rested 27 → 53; entered the sole boss node at 55 HP after Blood Vial. Empty potion slots. Native Autobattle played the fight; game confirmed defeat at 0 HP. |

At the final boss the permanent deck contained 32 cards: four Strikes, four Defends, Ascender's Bane, Greed, Armaments+, Battle Trance, Break+, two Bully, Drum of Battle, Forgotten Ritual, Inferno, Molten Fist, Rupture, three Second Wind, two Shrug It Off, Stoke, Thunderclap, Tremble, True Grit+, Unrelenting, and two Uppercut+. There is substantial block and Vulnerable support, but also eight basics, two curses, several setup cards, and competing uses of exhaust. Deck size alone does not prove a drafting error; identify specific marginal additions and their effects.

### Knowledge Demon: native combat and picker handling

This fight is not a DeepSeek tactical loss: L2 records no LLM combat decisions. Native Mentor owns card plays, end turns, and in-combat selection screens.

| Round | Live native-log observations |
| --- | --- |
| 1 | 55 HP against 399 HP Knowledge Demon. Played Rupture+, Break+, and Strike+. Boss ended at 356 HP. Boss then offered **Disintegration (6 end-of-turn damage)** versus **Mind Rot (draw 1 fewer card each turn)**. Advisor request `a364` chose Disintegration. |
| 2 | Uppercut+ and Stoke; Stoke replaced the remaining hand with generated cards at zero remaining energy. Next round began at 39 HP. Disintegration 6 is present in raw state. |
| 3 | Tremble twice (Burning Sticks supplied a copy), both Bully cards, then Defend. Boss fell 337 → 241 HP. Next round began at 22 HP. |
| 4 | Two Shrug It Off, Second Wind, Thunderclap. Block reached 31; boss then healed from 232 to 262. Player remained at 22 HP. Inferno was exhausted by Second Wind rather than installed. |
| 5 | Armaments+, Battle Trance+, Molten Fist, True Grit+. Boss ended at 244 HP. Second boss offer: **Disintegration (7)** versus **Sloth (cannot play more than 3 cards each turn)**. Advisor request `a385` chose Disintegration again; next round has Disintegration 13. |
| 6 | Began at 22 HP, 3 energy, no block, facing 21 attack plus Disintegration 13. Bully dealt 78, then Stoke replaced the remaining hand; Dominate and Pommel Strike followed. Boss remained at 114 HP, player at 22 HP and zero block/energy. Raw state records HP dropping to 9 on the end-of-turn effect, followed by lethal enemy attack and game-over. |

**Highest-value finding:** both boss-offer advice records explicitly say **“run-policy net”** and **“scored as a deck pick; this copy only lasts the fight.”** `a364` assigned Disintegration a 0.7117 selection share; `a385` assigned it 0.9991. These are option-selection outputs, not verified win probabilities. Combat-origin `card_reward` was routed through a deck-pick policy, while the coordinator correctly kept ownership inside native Autobattle. Distinguish those two layers: Python encounter continuity worked here, but the delegated selection policy deserves immediate review.

This evidence establishes the routing and choices, not that selecting Mind Rot or Sloth wins the fight. Both alternatives have serious costs. Reconstruct the actual debuff-choice semantics and compare full-fight outcomes with controlled, explicitly synthetic simulation before claiming a superior line. Do not simply route every `card_reward` to ordinary deck drafting or turn off combat ownership checks.

The advice also exposes a metric trap. Initial boss request `a360` reported top-level `p_win=0.5573`, `p_win_raw=0.0825`, and best-option simulation wins 384/4,653 (about 8.25%). Initial elite `a337` reported 0.8712 versus 0.3533 raw. These fields have different meanings or transformations. Inspect Mentor's implementation before presenting top-level `p_win` as a calibrated survival probability or feeding it into route risk.

## Previous completed run: why it matters

The older run's important failure chain was visible before its last fight:

- O7 #75 chose the Act 2 left branch at 75/86 HP, citing route variety, expert-profile distance, an Unknown, and a shop. That branch put three hallway fights before a forced elite and before the first rest. Its conditional plan to reconsider the elite did not make the elite avoidable once committed.
- Tunneler, Exoskeleton, and Hunter Killer reduced HP 75 → 68 → 48 → 18. O7 #131 took Hemokinesis at 48 HP for damage/self-damage synergy. O8 #32 chose Rupture over Pommel Strike+ at 18 HP for long-term scaling.
- O8 #36 took Unknown toward the shop at 18 HP; the forced elite was already recognized. O8 #37 traded Attack Potion for a random relic (Pendulum). O8 #40 bought Omnislice for 87g, then bought Skill Potion for 52g, leaving 40g. Review immediate single-target survival value against the actual imminent fight, rather than generic future AoE or permanent relic value.
- Infested Prism began at 18 HP. Skill Potion supplied Second Wind. The agent installed Inferno and later Dark Embrace+ and Rupture. It reached round 3 at 9 HP; a hand without block led into draw/generation, self-damage, and attacks. Final observed state was 7 HP, zero block/energy, Tainted 6, with a lethal Whirlwind intent. Game-over confirmed defeat.

The older traces also contained integration damage: full-slot potion purchase accepted without visible resolution, merchant exit omitted while slots were full, potion discards to free slots, temporary rest transition misclassified as actionable, invalid model output, and lost route memory across a restart. The final elite emitted a false victory upon opening the potion picker, followed by an actual defeat for the same encounter. Deduplicate combat rooms; the extra event is not a second fight.

Rechecked totals from original JSONL: 382 decisions; outcome verdicts 367 resolved, 8 stale/not sent, 3 accepted/unobservable, 1 no visible change; 513 model-call attempt records; strategic statuses 88 updated / 31 failed. The detailed earlier report supplies more decision-level reconstruction.

### What improved, and what still needs work

| Topic | Evidence across runs | Interpretation |
| --- | --- | --- |
| Dispatch reliability | Latest controller actions all resolved; no stale/rejected decisions | Positive integration evidence, but most combat actions moved outside the Python dispatch path |
| Same-run memory | L2 explicitly restored floor-30 route plan; older O8 lost prior route state | Persistence now works for this observed resume; strategic model assessment itself is rebuilt |
| In-fight overlays | Latest boss handled two curse offers and True Grit picker without false victory; older Skill Potion produced false victory | Current coordinator behavior improved on these examples; delegated choice quality is a separate issue |
| Route preparation | Older run reached forced elite at 18 HP; latest reached elite at 63 HP with a potion and a rest afterward | Latest preparation was better in those observed states; still suffered a large elite loss and died at boss |
| Conditional scaling | Older low-HP Rupture/self-damage choices; latest Rupture/Inferno/exhaust rationale | Evaluate realized draw/energy/tempo, not just theoretical synergy or expert rank |
| Evidence coverage | Older report found 168 rate-bearing contexts all cohort-wide without deck matching; latest persisted coverage includes qualified nearby-floor and some deck matches | Coverage improved; does not establish causal decision quality or predict wins |

## The intervening floor-6 run

P ran 21:01–21:20 on November 12 and stopped `max_elapsed`: “not enough session time left for a model call.” Configured cap was 1,200 seconds; it stopped after about 1,140 seconds to reserve a model-call timeout. It was alive at 56/80 HP after three hallway wins. Do not count this as model failure, death, or a completed run.

It is valuable diagnostic evidence:

- Every one of 25 decisions triggered a strategic update. There were 54 calls for 25 actions; median recorded call duration was 20.42 seconds. `opportunities_changed` appeared on 24 updates, including routine reward and transition screens.
- P #15 justified Bloodletting by claiming “only 2 base energy,” while the supplied current `max_energy` was null. No historical-energy field was present in that frame. The claim was unsupported.
- P #16–17 proposed Headbutt retrieval of Molten Fist after play, despite Molten Fist's Exhaust text. This confuses discard and exhaust zones.
- Current source already adds historical max-energy evidence, zone constraints, and stronger prompt instructions. Current strategic refresh gating also avoids many routine transitions. L1 still has 64 strategic records for 138 decisions, so review remaining cost, but do not recommend adding these existing safeguards as if absent.

Other short November 12 attempts exist: `20261112T123735Z-fe8fee` stopped on 1,024-token truncation with no actions, and `20261112T135412Z-b8987a` stopped on transport with no actions. These are setup/attempt records, not additional completed games.

## Confirmed telemetry gaps and interpretation limits

### Potion accounting and HP loss

L2's elite summary says “potions used by this agent: 0.” Native state/action records show `PotionUsedEntry` at elite step 11, followed by HP 57 → 74 and an empty potion slot. Python only increments `combat.potions_used` for its own potion dispatches, so the statement has a narrower meaning than a whole-fight potion count.

The recorded elite `hp_lost=36` is **net** 63 → 27, including potion healing and post-combat healing. Adding the observed 17 potion heal and 6 Burning Blood heal implies 59 total HP loss during combat if those are the only healing effects; label that reconstruction conditional, not a directly logged damage total. Do not calibrate raw incoming damage against this net field without accounting for healing and self-damage.

L2 reports `autobattle_combats=1` but `combats_finished=2`: the defeat path raises before the successful-autobattle counter increment. Started/finished combat bookkeeping is consistent (2/2), so this is a metric-semantics issue, not evidence that the boss was skipped. Over L1+L2, the successful-native-combat counter is 13 while 14 rooms were played.

### Route optimism

Before the late elite, the run-wide loss multiplier was clipped at 0.5, based on 12 completed fights with a raw realized/expected ratio of 0.37. Most included early wins; it was not a dedicated estimate of late Act 2 elite/boss capability. After the elite and defeat the multiplier rose to 0.76. Actual rest arrival was 27.1 percentage points below projection, but the defeated boss's recorded 63.2% HP loss still fell just under the supplied p90 of 63.7%, yielding zero `worse_than_p90` bosses in the summary. That flag alone does not indicate adequate survival calibration.

Review class/act conditioning, net healing, uncertainty with few relevant fights, and whether broad early success can over-discount later danger. One trajectory cannot establish a calibrated probability model.

### Missing strategic context and mechanics

L2 #8 at F32 says there is “no visible next encounter” and recommends seeking another rest/shop, even though the saved route and immediately preceding map observations establish the impending boss. Resting was sensible at 27 HP, but its explanation exposes missing retained context. `_strategic_facts` supplies present-screen opportunities; it does not carry the full map, boss identity, or saved route plan into the strategic snapshot. Distinguish an information omission from an irrational choice using information the model never received.

L2's initial assessment explicitly lists undefined Plating and Eternal mechanics, while other choices rely on the value of Gorget/Stone Armor or curse removal. Prioritize observed game keyword/removability facts and relevant upcoming encounter context over further generic instructions. Preserve the rule that null remains unknown; historical energy must not become a guaranteed future value.

L2 #1 rejected Stone Armor partly because experts bought it 0/78 times. The supplied history also had an act-level 0/23 sample, not a deck-matched sample. The competing Burning Sticks package is coherent, but “strong repeated exhaust synergy” overstates a relic whose supplied text says **the first time each combat** an exhausted Skill adds a copy. Evaluate the once-per-combat effect, timing, opportunity cost, and alternative package (Stone Armor plus multiple potions or saved gold). No winning alternative purchase is proven.

### Evidence and latency

Latest final persisted evaluation reports 14 card-reward screens / 42 option appearances, all identifier-matched and rate-bearing; 24 qualified for nearby-floor evidence and 5 for deck matching. Shop: 41 options, 38 identifiers, 27 qualifying history rates, only 1 nearby-floor match and zero deck matches. Event: 8 options, 5 identifiers, 1 qualifying history rate. These are support coverage counts, not prediction accuracy or independent randomized samples.

L1 stopped on two consecutive 60-second provider timeouts (strategy, then decision). Successful calls can also exceed 60 seconds in recorded total duration; SDK timeout semantics are not necessarily a strict whole-call wall clock. L2's first strategy call took 59.47 seconds. Inspect request/overall timeout behavior and output/reasoning settings before changing thresholds. Preserve bounded budgets and the no-resend rule for ambiguous gameplay requests. A safe retry of a read-only model call must still validate fresh state before dispatch.

The first manual restart L0 failed because this execution sandbox denied external sockets (`WinError 10013`); an authorized network-enabled restart succeeded. That transport incident is separate from the original provider timeout. Secrets remain in ignored `.env`; do not include keys or raw provider exceptions in reports.

## Current implementation: inspect before changing

| File / symbol | Why relevant |
| --- | --- |
| `src/sts_agent/decision/coordinator.py`: `_wait_ready`, `_delegate_autobattle`, `_fight_overlay` | Native ownership across complete fights and selection screens; terminal handling and success-only counter |
| Same file: `_finish_combat`, `_execute_action` | Net HP measurement and Python-only potion-use increment |
| Same file: `_strategic_facts`, `_maybe_refresh_strategy`, `_assessment_for_frame` | Information supplied to assessor, refresh gating, historical assessment applicability |
| Same file: `_execute_shop_plan` | Sequential execution of one LLM-selected basket with state/availability checks |
| Same file: `_save_state`, `_load_state` | Same-run/character/position validation and persisted route/encounter evidence |
| `src/sts_agent/strategic/prompts.py` | Current prompt version 2; explicit null, historical-energy, exhaust/retrieval, durable-assessment guidance |
| `src/sts_agent/decision/prompts.py` | Current decision prompt 16; whole-shop plan contract and evidence interpretation |
| `src/sts_agent/expert_stats.py`: `_add_duplicate_evidence`, route support | Existing duplicate-card evidence and conditioned support; inspect before duplicating mechanisms |
| `src/sts_agent/route_risk.py`, `route_profile.py`, `route_paths.py`, `elite_readiness.py` | HP-risk estimates, route outcomes, forced paths, and readiness evidence |
| `src/sts_agent/providers/deepseek.py`: `complete` | Timeout classification, no retries, structured output, sanitized errors |
| `src/sts_agent/cli.py`: `run_llm_session`, `_run_state_path` | Provider/planner construction, session trace and resume setup |
| `src/sts_agent/spire_mentor.py`: `MentorAutobattle` | Native bridge commands; current whole-fight mode does not consume advisor recommendations per action |
| `mod/McpMod.AgentStartup.cs`: `ExecuteAgentAutobattle` | Bridge's combat-in-progress and mode reporting |

The current gate already ignores ordinary combat updates after its first strategy revision and skips routine non-combat changes unless deck, relic, act, max HP, observed max energy, a branching route, or a pending combat result warrants refresh. Source on disk is newer than P and parts of L1; prompt versions in each recorded frame are authoritative for that decision. No Git revision for the latest run was established in this analysis, so do not equate today's tree with every historical action.

The native boss-choice implementation is outside this Python decision policy. The installed Mentor bundle is under `data/game/v0.107.1/mods/SpireMentor/`; `.venv/mod-build/mentor-inspection/` contains local inspection material. Establish the matching upstream source/build before patching a delegated policy. The native log's deck-pick routing is observed evidence, not a completed upstream source audit.

## Prioritized next work for Claude

1. **Boss debuff-choice routing — high confidence in defect candidate, high potential impact.** Trace `a364`/`a385` from combat-origin offers through native policy dispatch. Determine whether it evaluates permanent-deck card value rather than immediate fight consequences. Fix context handling where it actually lives, or design an explicit supported handoff while preserving exclusive action ownership. Verify on the two recorded offers; any alternate combat outcome is synthetic until tested live. Do not silently enable an LLM combat fallback.
2. **Combat evidence and telemetry — confirmed gap.** Capture/link native decisions, selections, potion events, and end-turn effects into session evidence. Report separate net HP change, observed healing, self-damage where known, and unknown damage components. Name success-only counters clearly or add outcome-specific native fight counts. Verify the elite's 17-HP potion heal and both boss outcomes without changing game state.
3. **Upcoming-encounter facts and route calibration — strong information-gap evidence; calibration solution needs validation.** Carry verified boss/next-node and committed-route context into relevant rest/shop/reward assessments. Review late-act risk conditioning and healing effects before trusting a global 0.5 loss multiplier. Compare projected and actual rest arrival for this run; do not invent alternate routes after commitment.
4. **Drafting and shop decisions — plausible strategic weakness, unproven winning counterfactuals.** Evaluate Orrery's five additions, the third Second Wind, Rupture setup dependence, the second Uppercut upgrade versus rest, and the late Burning Sticks package. Use the existing duplicate and package evidence. Require a concrete explanation of marginal value over skip/saving and a distinction between once-per-combat effects and repeatable engines. Compare the older low-HP scaling choices with these examples.
5. **Provider resilience and unnecessary calls — confirmed operational cost.** Retain current refresh improvements; measure remaining calls by trigger and successful/failed duration. Review bounded retry of provider-only failures, compatible reasoning/output settings, and timeout semantics. Never retry a possibly sent gameplay action merely because a model or observation call failed.

For each proposed change, state the exact evidence, confidence, expected behavior, implementation location, and smallest meaningful verification. Start with recorded data and source inspection. If simulation is used, label it synthetic; a replay validates contracts, not tactical optimality or alternate live wins. Use bounded live verification only after the change is reviewable. No broad repeated test runs are needed for this handover.

## Project constraints to preserve

- Read `AGENTS.md`. Tests only when absolutely necessary; keep tests and fixtures local and ignored, preserve existing local tests, and make any necessary test edits in one pass.
- Do not create unsolicited Markdown reports. This file was explicitly requested; further documentation requires scope justification or a user request.
- After implementation, briefly summarize every changed file and how it works.
- Existing user authorization covers downloading/running the game and mod and bounded live verification. It does not turn counterfactual simulation into observed live evidence.
- Do not publish or send this handover. The user will deliver it.
- `play-run` continues an existing game; `start-run` replaces it. The latest recorded run is over, so do not describe it as paused or resumable at F30. No game state was queried or changed during preparation of this document.
