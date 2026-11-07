"""Background sub-agent tasks (S6b).

`tasks.py` owns the registry and lifecycle, `runner.py` runs one bounded tool
loop without the parent context, `inbox.py`/`notify.py` deliver the conclusion,
and `tools.py` exposes the three model-facing tools.
"""
