"""Gray rollout and shadow observation for the Life layer (S7a).

Two pieces belong together because the second measures the first:

- a stable percentage bucket turns ``scope.rollout_percent`` into a sticky
  on/off decision per group (or per private session), so a rollout never flips a
  conversation on random messages;
- a durable decision log records every released turn (reply/wait, gate
  likelihood, debounce latency) without message or reply text, so a two-week
  gray window can be reviewed after restarts and reports.
"""
