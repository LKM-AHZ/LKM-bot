"""Planner monitoring and reasoning display (S6c).

The host is event-driven, so "the planner" is two observable moments: the
heart-flow decision that releases or holds a turn, and the outbound reply the
agent produced. Both are recorded in a bounded ring with no message text; the
panel reads it to answer "why did the bot answer / stay silent, and what was it
thinking".
"""
