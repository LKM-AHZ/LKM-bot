"""JSON-RPC framing for ACP over a line transport.

One JSON object per line. Responses are matched to their request id; server
requests (``session/request_permission`` and friends) are answered by a callback;
everything else is treated as a notification and handed to the client.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from bot import logger


class AcpError(RuntimeError):
    """The peer answered a request with a JSON-RPC error."""

    def __init__(self, error: dict[str, Any]) -> None:
        """Initialize the error.

        Args:
            error: JSON-RPC error object from the peer.
        """
        self.error = dict(error)
        super().__init__("ACP: " + json.dumps(self.error, ensure_ascii=False))


@runtime_checkable
class LineTransport(Protocol):
    """Minimal transport: read one line, write one line, close."""

    async def read_line(self) -> str | None:
        """Read one line without its newline; None at end of stream."""
        ...

    async def write_line(self, line: str) -> None:
        """Write one line; the transport adds the newline."""
        ...

    async def close(self) -> None:
        """Close the transport."""
        ...


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class AcpConnection:
    """Request bookkeeping over one ACP line transport."""

    def __init__(
        self,
        transport: LineTransport,
        *,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> None:
        """Initialize the connection without reading yet.

        Args:
            transport: Line transport carrying JSON-RPC messages.
            on_notification: Callback for ``method``-only messages.
            on_request: Callback for peer requests that need a result.
        """
        self._transport = transport
        self._on_notification = on_notification
        self._on_request = on_request
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sequence = 0
        self._reader_task: asyncio.Task | None = None
        self.failure: Exception | None = None

    def start(self) -> None:
        """Start the background reader loop."""
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="life_acp_reader"
        )

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Send one request and wait for its result.

        Args:
            method: JSON-RPC method.
            params: Request parameters.
            timeout: Seconds to wait for the result.

        Returns:
            The ``result`` object.

        Raises:
            AcpError: The peer returned an error.
            TimeoutError: No result within ``timeout``.
            RuntimeError: The connection is closed or broken.
        """
        if self.failure is not None:
            raise self.failure
        self._sequence += 1
        request_id = self._sequence
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return await asyncio.wait_for(future, timeout=max(0.1, float(timeout)))
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send one notification.

        Args:
            method: JSON-RPC method.
            params: Notification parameters.
        """
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        """Cancel the reader and close the transport."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        self._fail_pending(RuntimeError("ACP connection closed"))
        try:
            await self._transport.close()
        except Exception:
            logger.debug("[life] ACP transport close failed", exc_info=True)

    async def _send(self, payload: dict[str, Any]) -> None:
        """Serialize one payload and write it.

        Args:
            payload: JSON-RPC message.
        """
        await self._transport.write_line(json.dumps(payload, ensure_ascii=False))

    async def _read_loop(self) -> None:
        """Read and dispatch lines until the stream ends."""
        try:
            while True:
                line = await self._transport.read_line()
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[life] ACP sent a non-JSON line: %r", line[:120])
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            logger.warning("[life] ACP reader stopped: %s", exc)
        finally:
            if self.failure is None:
                self.failure = RuntimeError("ACP stream closed")
            self._fail_pending(self.failure)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        """Route one incoming message.

        Args:
            message: Parsed JSON-RPC message.
        """
        request_id = message.get("id")
        if request_id is not None and ("result" in message or "error" in message):
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(AcpError(message["error"]))
            else:
                future.set_result(message.get("result") or {})
            return

        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params") or {}

        if request_id is not None:
            result: dict[str, Any] | None = None
            error: dict[str, Any] | None = None
            if self._on_request is None:
                error = {"code": -32601, "message": f"unsupported request {method}"}
            else:
                try:
                    result = await self._on_request(method, params)
                except AcpError as exc:
                    error = exc.error
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
            if error is not None:
                reply["error"] = error
            else:
                reply["result"] = result or {}
            await self._send(reply)
            return

        if self._on_notification is not None:
            try:
                await self._on_notification(method, params)
            except Exception:
                logger.exception("[life] ACP notification handler failed")

    def _fail_pending(self, exc: Exception) -> None:
        """Fail every waiting request.

        Args:
            exc: Exception to set on the pending futures.
        """
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)
