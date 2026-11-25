"""The Regent's strategy, shared by planning and individual decisions.

Owner's guidance: balance Star gain against Star cost, while blocking and scaling
(potentially with Stars or with the sword).
"""

PROMPT_VERSION = "regent-strategic-planner-prompt-1"

DECK_GUIDANCE = """\
THE REGENT'S DECK. Stars are a second resource beside energy: some cards gain them, others cost them on top of their energy cost (a card's star_cost), and the starting relic gives a few at the start of each combat. Keep gain and cost in balance. The deck_balance track "stars" compares the Stars the deck's cards gain in one pass through the deck with the Star costs it holds. While it is short, a Star-cost card will often sit in hand unplayable, so take Star gain first and skip further Star costs however strong the card reads; a Star-cost card is priced by both costs, so check that the deck can pay its Stars in the turn it is drawn. While it is over-supplied, Stars go unspent: a strong Star-cost card or a payoff for gaining and spending Stars is then worth more than its text alone, and plain Star gain is a skip. The relic's opening Stars pay for one or two early plays, not for a deck of Star costs.

Block alongside, in every act: non-starter Block cards first, then Block that grows or repeats (Plating, a Power that gains Block when Stars are spent or when the Sovereign Blade is played). Star gain that comes on a Block card serves both needs.

Scaling comes from one of two routes; count the pieces held and build the one that is ahead instead of splitting. Stars: Powers that pay out every time Stars are gained or spent, or that gain Stars every turn, need a steady flow in both directions, so they follow a balanced "stars" track, not the other way round. The sword: the first Forge of a combat adds Sovereign Blade to the hand and every Forge adds to its damage for that fight, so the route needs several Forge sources that are played early, and then the cards that make the Blade hit more often, hit ALL enemies or return to hand. One Forge card alone is not scaling; several are, and deck_balance counts each as damage scaling."""

ELITE_GUIDANCE = """\
THE REGENT'S ELITE ROUTES. Follow the act stance below with one check of the Regent's own: an elite fight is short and front-loaded, so before an act 1 elite the deck must be able to spend its Stars in the first turns (a balanced "stars" track) and block the elite's first big attack with non-starter Block. The Regent has no healing after fights; place a rest site before or right behind an elite when the map allows, and say in the plan which elites are accepted and what the deck must pick up before each."""
