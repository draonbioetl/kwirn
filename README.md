# Kwirn

A two-player game of dots, lines and crossings. Place a dot, draw a line, and score for every enemy line you cross. Close a loop to armour your lines and net the dots inside.

- `kwirn.html` is the whole game (engine, bots and UI in one file). Open it directly to play the bots or pass-and-play.
- `kwirn_server.py` serves the page and relays moves for online games against a friend. It uses only Python's standard library.

Run locally: `python kwirn_server.py`, then open http://localhost:8765.
