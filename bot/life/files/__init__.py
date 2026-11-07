"""Group file service for the Life layer (S6e).

Files live in a sandboxed directory: ``out/`` holds generated documents the bot
may send, ``tmp/`` holds scratch files with a short TTL. Names are sanitized,
extensions allowlisted and sizes capped on the way in, and a periodic cleanup
removes expired files.
"""
