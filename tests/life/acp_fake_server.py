"""Tiny ACP server used by the S6d tests.

It speaks NDJSON JSON-RPC on stdio and implements just enough of ACP v1:
initialize, session/new, session/prompt (two text chunks) and session/cancel.
Pass ``--hang`` to never answer prompts, which exercises the timeout path.
"""

import json
import sys

SESSION_ID = "s-1"
HANG = "--hang" in sys.argv


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        reply(request_id, {"protocolVersion": 1, "agentCapabilities": {"mcpCapabilities": {"http": False}}})
    elif method == "session/new":
        reply(request_id, {"sessionId": SESSION_ID})
    elif method == "session/prompt":
        if HANG:
            continue
        notify(
            "session/update",
            {
                "sessionId": SESSION_ID,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "你好，"},
                },
            },
        )
        notify(
            "session/update",
            {
                "sessionId": SESSION_ID,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "世界"},
                },
            },
        )
        reply(request_id, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        pass
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unsupported"}})
