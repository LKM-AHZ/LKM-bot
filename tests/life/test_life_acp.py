"""Tests for the ACP adapter: framing, handshake, prompts, tool and stage."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.life.acp.client import AcpClient
from bot.life.acp.protocol import AcpConnection, AcpError
from bot.life.acp.service import AcpService
from bot.life.acp.tools import register_acp_tools, unregister_acp_tools
from bot.life.config import AcpConfig, LifeConfig
from bot.life.pipeline import LifeAcpStage

FAKE_SERVER = Path(__file__).with_name("acp_fake_server.py")


class FakeTransport:
    """Line transport driven by a queue and recording what was written."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.written: list[dict] = []
        self.closed = False

    async def read_line(self) -> str | None:
        return await self.inbox.get()

    async def write_line(self, line: str) -> None:
        self.written.append(json.loads(line))

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict) -> None:
        self.inbox.put_nowait(json.dumps(payload, ensure_ascii=False))

    def last_request(self) -> dict:
        for item in reversed(self.written):
            if "method" in item and "id" in item:
                return item
        raise AssertionError("no request was written")


async def _request_in_background(connection, transport, method="x", params=None, timeout=2.0):
    task = asyncio.create_task(connection.request(method, params or {}, timeout))
    for _ in range(50):
        await asyncio.sleep(0)
        try:
            transport.last_request()
        except AssertionError:
            continue
        return task
    raise AssertionError("request was not written")


@pytest.mark.asyncio
async def test_protocol_matches_request_and_response() -> None:
    transport = FakeTransport()
    connection = AcpConnection(transport)
    connection.start()
    task = await _request_in_background(connection, transport, "initialize")
    request = transport.last_request()
    assert request["method"] == "initialize"

    transport.push({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": 1}})

    assert await task == {"ok": 1}
    await connection.close()
    assert transport.closed is True


@pytest.mark.asyncio
async def test_protocol_raises_peer_error() -> None:
    transport = FakeTransport()
    connection = AcpConnection(transport)
    connection.start()
    task = await _request_in_background(connection, transport, "session/new")
    request = transport.last_request()
    transport.push(
        {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "nope"}}
    )

    with pytest.raises(AcpError):
        await task
    await connection.close()


@pytest.mark.asyncio
async def test_protocol_times_out() -> None:
    transport = FakeTransport()
    connection = AcpConnection(transport)
    connection.start()
    with pytest.raises(asyncio.TimeoutError):
        await connection.request("initialize", {}, 0.1)
    await connection.close()


@pytest.mark.asyncio
async def test_protocol_dispatches_notifications() -> None:
    transport = FakeTransport()
    seen: list[tuple[str, dict]] = []

    async def on_notification(method, params):
        seen.append((method, params))

    connection = AcpConnection(transport, on_notification=on_notification)
    connection.start()
    transport.push({"jsonrpc": "2.0", "method": "session/update", "params": {"a": 1}})
    for _ in range(20):
        await asyncio.sleep(0)
        if seen:
            break
    assert seen == [("session/update", {"a": 1})]
    await connection.close()


@pytest.mark.asyncio
async def test_protocol_answers_peer_requests() -> None:
    transport = FakeTransport()
    connection = AcpConnection(transport, on_request=lambda m, p: _async({"outcome": "cancelled"}))
    connection.start()
    transport.push({"jsonrpc": "2.0", "id": 7, "method": "session/request_permission", "params": {}})
    for _ in range(50):
        await asyncio.sleep(0)
        if any(item.get("id") == 7 for item in transport.written):
            break
    reply = next(item for item in transport.written if item.get("id") == 7)
    assert reply["result"] == {"outcome": "cancelled"}
    await connection.close()


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_protocol_fails_pending_when_stream_closes() -> None:
    transport = FakeTransport()
    connection = AcpConnection(transport)
    connection.start()
    task = await _request_in_background(connection, transport, "initialize")
    await transport.inbox.put(None)

    with pytest.raises(RuntimeError):
        await task


def _service(tmp_path, **config) -> AcpService:
    acp = AcpConfig(
        enabled=True,
        command=sys.executable,
        args=[str(FAKE_SERVER)],
        timeout_seconds=5,
        **config,
    )
    return AcpService(acp, tmp_path)


@pytest.mark.asyncio
async def test_service_returns_collected_chunks(tmp_path) -> None:
    service = _service(tmp_path)
    try:
        answer = await service.ask("aiocqhttp:GroupMessage:1", "打个招呼")
        assert answer == "你好，世界"
        assert service.status()["counters"]["ok"] == 1
        assert service.status()["sessions"] == 1
        assert (tmp_path / "acp").exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_reuses_session_and_caps_output(tmp_path) -> None:
    service = _service(tmp_path, max_output_chars=2)
    try:
        assert await service.ask("s1", "one") == "你好"
        assert await service.ask("s1", "two") == "你好"
        assert service.status()["sessions"] == 1
        assert service.status()["counters"]["asks"] == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_times_out_and_recovers(tmp_path) -> None:
    acp = AcpConfig(
        enabled=True,
        command=sys.executable,
        args=[str(FAKE_SERVER), "--hang"],
        timeout_seconds=0.5,
    )
    service = AcpService(acp, tmp_path)
    try:
        assert await service.ask("s1", "hello") is None
        assert service.status()["counters"]["timeouts"] == 1
        assert service.status()["connected"] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_is_inert_without_command(tmp_path) -> None:
    service = AcpService(AcpConfig(enabled=True), tmp_path)
    assert service.enabled is False
    assert await service.ask("s1", "hi") is None
    await service.close()


@pytest.mark.asyncio
async def test_client_handshake_rejects_wrong_version() -> None:
    transport = FakeTransport()
    client = AcpClient(transport, timeout_seconds=2)
    start_task = asyncio.create_task(client.start())
    for _ in range(100):
        await asyncio.sleep(0)
        if transport.written:
            break
    request = transport.written[-1]
    transport.push(
        {"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 2}}
    )

    with pytest.raises(ValueError):
        await start_task
    await client.close()


class FakeAcp:
    def __init__(self, answer: str | None = "答：好") -> None:
        self.answer = answer
        self.asks: list[tuple[str, str]] = []
        self.enabled = True
        self.config = SimpleNamespace(takeover=True)

    async def ask(self, umo: str, text: str) -> str | None:
        self.asks.append((umo, text))
        return self.answer


class StageEvent:
    def __init__(self, text: str = "你好", umo: str = "aiocqhttp:GroupMessage:1") -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._stopped = False

    def get_group_id(self) -> str:
        return "1"

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


def _stage(acp, *, sent_ok: bool = True, scope_all: bool = True) -> LifeAcpStage:
    config = LifeConfig(enabled=True)
    if scope_all:
        config.scope.mode = "all"

    async def send_text(umo: str, text: str):
        return SimpleNamespace(ok=sent_ok)

    stage = LifeAcpStage()
    stage.service = SimpleNamespace(config=config, acp=acp, send_text=send_text)
    return stage


@pytest.mark.asyncio
async def test_stage_answers_and_stops() -> None:
    acp = FakeAcp()
    stage = _stage(acp)
    event = StageEvent("你好")

    await stage.process(event)

    assert acp.asks == [("aiocqhttp:GroupMessage:1", "你好")]
    assert event.is_stopped() is True


@pytest.mark.asyncio
async def test_stage_falls_through_when_no_answer() -> None:
    acp = FakeAcp(answer=None)
    stage = _stage(acp)
    event = StageEvent("你好")

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_stage_skips_commands_and_out_of_scope() -> None:
    acp = FakeAcp()
    stage = _stage(acp)
    await stage.process(StageEvent("/help"))
    stage_out = _stage(acp, scope_all=False)
    await stage_out.process(StageEvent("你好"))
    assert acp.asks == []


def test_tool_registers_and_removes() -> None:
    calls: list[tuple[str, str]] = []
    acp = FakeAcp()

    class Manager:
        def __init__(self):
            self.tools = {}

        def add_func(self, name, func_args, desc, handler):
            self.tools[name] = handler

        def remove_func(self, name):
            self.tools.pop(name, None)

    class Service:
        config = LifeConfig(enabled=True)

        def __init__(self):
            self.acp = acp

    service = Service()
    service.config.scope.mode = "all"
    manager = Manager()
    register_acp_tools(manager, service)
    assert "ask_external_agent" in manager.tools

    event = SimpleNamespace(
        unified_msg_origin="aiocqhttp:GroupMessage:1",
        get_group_id=lambda: "1",
    )
    result = asyncio.run(manager.tools["ask_external_agent"](event, "问题"))
    assert result == "答：好"
    assert acp.asks == [("aiocqhttp:GroupMessage:1", "问题")]

    unregister_acp_tools(manager)
    assert manager.tools == {}
