"""Heart-flow runtime for the Life layer (S4).

The package is deliberately split into pure decision pieces so the replay set
can evaluate them without a model:

- ``debounce``: merge a burst of messages into one turn;
- ``gates``: reply likelihood and the dynamic threshold;
- ``policy``: the glue shared by the runtime and the replay runner.

The stateful runtime (focus timeline, planner loop, interrupt) arrives in the
next slices and builds on these pieces.
"""

from bot.life.heartflow.debounce import (
    DebounceAccumulator,
    DebouncePlan,
    PendingMessage,
)
from bot.life.heartflow.gates import BatchFeatures, GateContext, GateDecision
from bot.life.heartflow.policy import (
    HeartFlowPolicy,
    PolicyDecision,
    PolicyMessage,
)

__all__ = [
    "BatchFeatures",
    "DebounceAccumulator",
    "DebouncePlan",
    "GateContext",
    "GateDecision",
    "HeartFlowPolicy",
    "PendingMessage",
    "PolicyDecision",
    "PolicyMessage",
]
