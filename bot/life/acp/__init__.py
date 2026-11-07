"""ACP (Agent Client Protocol) v1 client for the Life layer (S6d).

The external agent runs as a subprocess speaking NDJSON over stdio. This module
implements only the client side the bot needs: framing, request/response
matching, session creation and one prompt per turn. It is deliberately small;
the Life layer never lets the external agent call back into the bot's tools.
"""
