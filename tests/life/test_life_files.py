"""Tests for the group file service: store, sending, download and tools."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.core.message.components import File
from bot.life.config import FilesConfig, LifeConfig
from bot.life.files.service import FileService
from bot.life.files.store import FileStore, FileStoreError
from bot.life.files.tools import register_file_tools, unregister_file_tools
from bot.life.service import LifeService, set_life_service


def _store(tmp_path, **kwargs) -> FileStore:
    defaults = {
        "max_file_bytes": 1024,
        "allowed_extensions": [".md", ".txt", ".json", ".png"],
        "max_files": 5,
    }
    defaults.update(kwargs)
    return FileStore(tmp_path / "files", **defaults)


def test_sanitize_strips_directories_and_unsafe_chars(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.sanitize("../../etc/passwd") == "passwd"
    assert store.sanitize("我的 报告/最终.md") == "最终.md"
    assert store.sanitize("a b\"c.md") == "a_b_c.md"
    with pytest.raises(FileStoreError):
        store.sanitize("   ")
    with pytest.raises(FileStoreError):
        store.sanitize("CON.md")


def test_extension_allowlist(tmp_path) -> None:
    store = _store(tmp_path)
    store.check_extension("notes.md")
    with pytest.raises(FileStoreError):
        store.check_extension("payload.exe")
    with pytest.raises(FileStoreError):
        store.check_extension("noextension")


def test_save_enforces_size_and_writes(tmp_path) -> None:
    store = _store(tmp_path)
    stored = store.save("notes.md", "hello")
    assert stored.size == 5
    assert stored.path.read_text(encoding="utf-8") == "hello"

    with pytest.raises(FileStoreError):
        store.save("big.md", "x" * 2000)


def test_resolve_rejects_missing_and_unknown_dir(tmp_path) -> None:
    store = _store(tmp_path)
    store.save("notes.md", "hi")
    assert store.resolve("notes.md").name == "notes.md"
    with pytest.raises(FileStoreError):
        store.resolve("missing.md")
    with pytest.raises(FileStoreError):
        store.save("x.md", "hi", subdir="elsewhere")


def test_cleanup_removes_by_age(tmp_path) -> None:
    store = _store(tmp_path)
    old = store.save("old.md", "old")
    fresh = store.save("fresh.md", "fresh")
    scratch = store.save("scratch.txt", "tmp", subdir="tmp")
    now = time.time()
    import os

    os.utime(old.path, (now - 9 * 86400, now - 9 * 86400))
    os.utime(scratch.path, (now - 48 * 3600, now - 48 * 3600))

    removed = store.cleanup(keep_days=7, tmp_ttl_hours=24, now=now)

    assert removed == 2
    assert not old.path.exists()
    assert not scratch.path.exists()
    assert fresh.path.exists()


def test_max_files_keeps_the_newest(tmp_path) -> None:
    store = _store(tmp_path, max_files=2)
    for index in range(3):
        store.save(f"f{index}.md", "x")
        time.sleep(0.01)
    remaining = [item.name for item in store.list_recent(limit=10)]
    assert remaining == ["f2.md", "f1.md"]
    assert store.stats()["files"] == 2


def test_sendable_rejects_oversize(tmp_path) -> None:
    store = _store(tmp_path)
    store.save("big.md", "x" * 100)
    with pytest.raises(FileStoreError):
        store.sendable("big.md", max_bytes=10)
    assert store.sendable("big.md", max_bytes=1000).size == 100


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.chains: list = []

    async def __call__(self, umo: str, chain):
        self.chains.append((umo, chain))
        return SimpleNamespace(ok=self.ok, error="" if self.ok else "send failed")


def _service(tmp_path, sender: FakeSender, **config) -> FileService:
    values = {
        "enabled": True,
        "allowed_extensions": [".md", ".txt", ".json"],
        "max_file_bytes": 1000,
        "max_send_bytes": 1000,
    }
    values.update(config)
    return FileService(FilesConfig(**values), tmp_path, sender)


@pytest.mark.asyncio
async def test_write_and_send_uses_file_component(tmp_path) -> None:
    sender = FakeSender()
    service = _service(tmp_path, sender)

    reply = await service.write_and_send("s1", "report.md", "# 报告")

    assert reply.startswith("sent: report.md")
    umo, chain = sender.chains[0]
    assert umo == "s1"
    assert isinstance(chain.chain[0], File)
    assert chain.chain[0].name == "report.md"
    assert service.status()["counters"]["written"] == 1
    assert service.status()["counters"]["sent"] == 1


@pytest.mark.asyncio
async def test_write_and_send_rejects_bad_input(tmp_path) -> None:
    service = _service(tmp_path, FakeSender())
    assert "content is empty" in await service.write_and_send("s1", "a.md", "  ")
    assert "not allowed" in await service.write_and_send("s1", "a.exe", "x")
    assert service.status()["counters"]["failed"] == 2


@pytest.mark.asyncio
async def test_send_reports_platform_failure(tmp_path) -> None:
    sender = FakeSender(ok=False)
    service = _service(tmp_path, sender)
    await service.write_and_send("s1", "report.md", "x")
    assert service.status()["counters"]["failed"] == 1


@pytest.mark.asyncio
async def test_download_saves_file_and_infers_name(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/files/doc.md"
        return httpx.Response(200, content=b"# doc")

    service = _service(
        tmp_path,
        FakeSender(),
        download_timeout_seconds=5,
    )
    service._transport = httpx.MockTransport(handler)

    reply = await service.download("s1", "https://example.com/files/doc.md")

    assert reply == "downloaded: doc.md (5 bytes)"
    assert (tmp_path / "files" / "out" / "doc.md").read_text(encoding="utf-8") == "# doc"
    assert service.status()["counters"]["downloaded"] == 1


@pytest.mark.asyncio
async def test_download_rejects_scheme_and_oversize(tmp_path) -> None:
    service = _service(tmp_path, FakeSender(), max_file_bytes=10)
    service._transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 100)
    )

    assert "http(s)" in await service.download("s1", "file:///etc/passwd")
    assert "limit" in await service.download("s1", "https://example.com/big.md")
    assert service.status()["counters"]["downloaded"] == 0


@pytest.mark.asyncio
async def test_cleanup_counts_removed_files(tmp_path) -> None:
    service = _service(tmp_path, FakeSender())
    stored = service.store.save("old.md", "x")
    import os

    old = time.time() - 10 * 86400
    os.utime(stored.path, (old, old))

    removed = await service.cleanup()

    assert removed == 1
    assert service.status()["counters"]["cleaned"] == 1


class FakeManager:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_func(self, name, func_args, desc, handler) -> None:
        self.tools[name] = handler

    def remove_func(self, name) -> None:
        self.tools.pop(name, None)


def _tool_service(service: FileService, *, enabled: bool = True):
    config = LifeConfig(enabled=True)
    config.scope.mode = "all" if enabled else "allowlist"
    return SimpleNamespace(config=config, files=service)


def _event(umo: str = "aiocqhttp:GroupMessage:1"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_group_id=lambda: "1",
    )


@pytest.mark.asyncio
async def test_file_tools_register_and_respect_scope(tmp_path) -> None:
    sender = FakeSender()
    files = _service(tmp_path, sender)
    manager = FakeManager()
    register_file_tools(manager, _tool_service(files))
    assert set(manager.tools) == {"send_document", "send_file", "download_file"}

    reply = await manager.tools["send_document"](_event(), "a.md", "hello")
    assert reply.startswith("sent: a.md")

    out_of_scope = FakeManager()
    register_file_tools(out_of_scope, _tool_service(files, enabled=False))
    denied = await out_of_scope.tools["send_file"](_event(), "a.md")
    assert "not enabled for this session" in denied

    unregister_file_tools(manager)
    assert manager.tools == {}


@pytest_asyncio.fixture
async def life_db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_life_service_wires_files_and_reports_status(life_db, tmp_path) -> None:
    config = LifeConfig(enabled=True, files=FilesConfig(enabled=True))
    config.scope.mode = "all"
    service = LifeService(
        life_db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        assert service.files is not None
        block = (await service.status())["files"]
        assert block["enabled"] is True
        assert block["counters"]["sent"] == 0
        assert (tmp_path / "life" / "files" / "out").is_dir()
    finally:
        await service.stop()
        set_life_service(None)
