"""Session timeline, turn compression and checkpoints (S4b).

- ``timeline``: pure timeline model with folding and checkpoint/restore;
- ``models``: ``life_focus`` and ``life_focus_turn`` tables;
- ``store``: persistence for the digest and the turns;
- ``service``: per-session cache, archive hook, prompt block and status.
"""
