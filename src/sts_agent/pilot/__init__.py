"""The agent's own combat pilot: a search per decision, tailored to the fight.

Spire Mentor (tfriedel/spire-mentor, MIT) ships a faithful combat simulator with the
mod. The pilot drives that simulator itself instead of taking Mentor's pick:

* ``engine``   loads the simulator from the installed mod;
* ``profiles`` says how to search and pick in each fight (per encounter, with defaults per tier);
* ``search``   makes one decision: a search, then a re-search of the options it cannot tell apart;
* ``sim``      plays whole simulated fights from the decks of logged runs, to compare and tune profiles.

Everything here runs on the simulator alone. Nothing in this package touches the live game.
"""
