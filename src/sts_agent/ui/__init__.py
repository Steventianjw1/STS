"""The agent's interface: local web panels tiled around the game window.

    hub.py         what the running session reports, kept for the panels; pause/step/stop control
    combat_log.py  fight log rebuilt from consecutive game observations
    windows.py     screen layout, game-window placement and panel windows (Windows only)
    server.py      localhost HTTP server: the page, the state feed, the commands
    app.py         ties them together; started by ``sts-agent ui`` / ``./start``
    panels.html    the page shown in every panel window
"""
