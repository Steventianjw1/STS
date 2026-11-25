"""The Defect's strategy, shared by planning and individual decisions.

Owner's guidance: mixed playstyles and a few archetypes, generally with Frost Orbs; look
for scaling with either Orbs or physical attacks.
"""

PROMPT_VERSION = "defect-strategic-planner-prompt-1"

DECK_GUIDANCE = """\
THE DEFECT'S DECK. The Defect wins with several builds and usually with a mix of them, so judge the deck by what it does this fight, not by an archetype. Nearly every build defends the same way, with Frost: a Channeled Frost Orb gains Block at the end of every turn for as long as it stays in a slot, and again when it is Evoked. The deck_balance track "frost" counts the cards that Channel Frost; while it is missing or short a Frost card is the first defensive pick, ahead of a plain Block card of similar size, and deck_balance counts it as Block. Orb slots are shared: Channeling into full slots Evokes the leftmost Orb, so count the Orbs the deck Channels in a turn against its slots before adding more generators, and do not Evoke away the Frost the defense stands on. The starter's Lightning deals the early damage; it is not a defense.

Scaling comes from Orbs or from attacks, and the deck already leans one way; build that one and take the other only when it costs nothing. Orbs: Focus raises what every Orb does, Frost's Block included, and more slots, a Power that Channels or triggers an Orb every turn, and Dark or Lightning for damage all add to it; this route needs Orbs that stay in play, so it wants Frost and slots before Focus. Attacks: an attack that raises its own damage every time it is played, a zero-cost Attack engine with the cards that return or replay them, or lasting Strength; this route wants cheap attacks, draw and a thin deck, and its Orb slots are spare, so cards that give up slots or Focus cost it little. deck_balance counts Focus and Orb Powers as scaling for both damage and Block. A Power that needs several turns to pay back is weighed against the act boss's first big attack: it is in play early or it is a skip. Status cards a card adds to the deck are a cost every later draw pays unless something held turns them into value."""

ELITE_GUIDANCE = """\
THE DEFECT'S ELITE ROUTES. Follow the act stance below with one check of the Defect's own: the starter deck's damage is spread over Lightning hits on random enemies, so an elite with several enemies or summons is cheaper for the Defect than for most, while a single elite with one big attack turn needs Frost or non-starter Block in the deck before it. The Defect has no healing after fights; place a rest site before or right behind an elite when the map allows, and say in the plan which elites are accepted and what the deck must pick up before each."""
