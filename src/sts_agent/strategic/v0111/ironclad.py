"""The Ironclad's strategy on game build v0.111, shared by planning and individual decisions.

On v0.107.1 the Ironclad played by the shared text alone. On v0.111 it has its own deck guidance and
reads the deck balance with its own plan, like the other four, because what this build's best players
do with it is known.

The numbers in the text are what the highest-rated Spire Codex players did with the Ironclad on this
build: 532 solo A10 standard runs of builds v0.111.1 and v0.111.0 by the 20 highest-rated accounts that
have them, 397 won, read 2026-11-25 with ``python -m sts_agent.pilot --build v0.111.0 players --character
IRONCLAD --players 20 --runs 40 --words Strength,Exhaust,Vulnerable`` (the decks carried into each act
boss and what was taken at 10142 card rewards, counted by ``evidence.deck_balance``'s role rules). The
simulated numbers are ``pilot.runs`` on Spire Mentor's v0.111.0 simulator: 120 A10 runs drafted by its
run-policy net, fights searched, no model and no live run.
"""

PROMPT_VERSION = "ironclad-strategic-planner-prompt-v0111-8"

DECK_GUIDANCE = """\
THE IRONCLAD'S DECK. The starter already hits hard and the starting relic heals after every fight, so the deck is not built by adding attacks. What it is built from is known. In 532 solo A10 runs on this game version by the highest-rated Spire Codex players (397 won), the deck entering the act 1, 2 and 3 bosses held about 19, 26 and 32 cards. It is not kept thin: they took 84% of the card rewards in act 1, 64% in act 2 and 51% in act 3, in act 2 as often with a large deck as with a small one, and what they added after act 1 was Skills and Powers (attacks go from 8 to 11 while Skills go from 8 to 15 and Powers from 1 to 5).

What they took when it was offered: a Power that gains Block every turn or every time a card is Exhausted or played, about half the time whether the deck held none, one or two; a first card that applies Weak or lowers enemy Strength 55% of the time (nearly always among the first three picks) and a second 31%; energy, draw and Exhaust utility (a card that pays HP or a card for energy or draw, a draw Skill) a third of the time or more in every act whatever the deck held, the best of them two times in three; a first damage-scaling source one time in three and later ones one in five. Cards that Exhaust or pay for Exhausting were taken more often the more of them the deck held (23% with none, 36% with six), so an Exhaust package is built on once it exists. What they left: a damage card (29% with none added, 14% with three, 9% with six). Plain Block was taken less as the deck held more of it (38% with none, 24% with three, 18% with six), more steeply than with any other character, so prefer Block that grows or a Block card that does something more over a sixth plain one.

deck_balance reads the deck by that plan. The track "weak" asks for one Weak source and it comes first while missing. Block scaling comes next and is short below four sources. The track "flow" counts the cards that draw or give energy and is always marked wanted: those players took one 41 to 49% of the times it was offered whether the deck held none or five, and whatever Powers it held, about twice as often as another card with no role, so such a card passes at any count, after a missing or short role. Damage is marked over-supplied once the deck holds more damage cards than those decks did (from the fifth in act 1, the seventh in act 2, the eighth after), Block is never marked over-supplied (in simulated runs a limit on Block cards cost act 2 boss wins), and damage scaling is full at four. So in act 1 still secure the attack that hits ALL enemies and the big hit the elites ask for, then stop adding attacks; answer a boss that asks for more damage by upgrading and removing, not by a tenth attack.

Removals: about three a run, half of them in act 2. Starter Strikes go first in act 1, with curses; in act 2 those players removed as many added cards as Strikes, so a card that no longer earns its draw is a removal target like a Strike is. A card with a lasting cost per play (Max HP, a curse or Wound added) is played in almost every fight: judge it by that cost times the fights remaining.
"""

ELITE_GUIDANCE = """\
THE IRONCLAD'S ELITE ROUTES. Follow the act stance below; the Ironclad is the character it describes best. In simulated A10 runs on this game version a strong drafter lost about one act 1 elite in forty and one act 2 elite in twenty, and an elite it beat cost a quarter of max HP in act 1 and three tenths in act 2. The highest-rated players take more elites with the Ironclad than with any other character: 2.7 in act 1, 2.5 in act 2 and 1.9 in act 3 a run, losing about 22% of max HP to each, and they still enter the act 1 boss at 62% HP and the act 2 boss at 71%, because the starting relic gives part of it back. Their deck at the act 1 boss holds about nine added cards: three or four that deal damage, two or three Block cards, one damage-scaling source, two energy, draw or utility cards, and a Block Power in four runs out of ten. Say in the plan which elites are accepted and what the deck must pick up before each.
"""
