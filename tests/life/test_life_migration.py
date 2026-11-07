"""Tests for the MaiBot -> Life migration (S7b)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.emoji.models import LifeEmoji
from bot.life.expression.models import LifeExpression
from bot.life.jargon.models import LifeJargon
from bot.life.memory.models import LifeMemory
from bot.life.migration.service import MigrationService
from bot.life.migration.source import MaiBotSource, build_session_key, to_ts
from bot.life.models import LifeMessage
from bot.life.person.models import LifePerson, LifePersonFact

UMO = "aiocqhttp:GroupMessage:1001"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32


def _source(tmp_path: Path) -> Path:
    """Build a minimal MaiBot deployment and return its root."""
    root = tmp_path / "maibot"
    data = root / "data"
    (data / "emoji").mkdir(parents=True)
    (data / "emoji" / "a.jpg").write_bytes(JPEG)
    (data / "a-memorix").mkdir()

    connection = sqlite3.connect(data / "MaiBot.db")
    connection.executescript(
        """
        CREATE TABLE mai_messages (
            id INTEGER PRIMARY KEY,
            message_id TEXT,
            timestamp TEXT,
            received_at TEXT,
            platform TEXT,
            user_id TEXT,
            user_nickname TEXT,
            user_cardname TEXT,
            group_id TEXT,
            session_id TEXT,
            reply_to TEXT,
            is_mentioned INTEGER DEFAULT 0,
            is_at INTEGER DEFAULT 0,
            is_emoji INTEGER DEFAULT 0,
            is_picture INTEGER DEFAULT 0,
            is_command INTEGER DEFAULT 0,
            processed_plain_text TEXT
        );
        CREATE TABLE person_info (
            id INTEGER PRIMARY KEY,
            person_id TEXT,
            person_name TEXT,
            platform TEXT,
            user_id TEXT,
            user_nickname TEXT,
            group_cardname TEXT,
            memory_points TEXT,
            know_counts INTEGER DEFAULT 0,
            first_known_time TEXT,
            last_known_time TEXT
        );
        CREATE TABLE expressions (
            id INTEGER PRIMARY KEY,
            situation TEXT,
            style TEXT,
            content_list TEXT,
            count INTEGER DEFAULT 0,
            last_active_time TEXT,
            create_time TEXT,
            session_id TEXT,
            checked INTEGER DEFAULT 0
        );
        CREATE TABLE jargons (
            id INTEGER PRIMARY KEY,
            content TEXT,
            meaning TEXT,
            session_id_dict TEXT,
            count INTEGER DEFAULT 0,
            is_jargon INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0,
            is_global INTEGER DEFAULT 0,
            created_by TEXT,
            created_timestamp TEXT,
            updated_timestamp TEXT,
            evidence_messages TEXT
        );
        CREATE TABLE images (
            id INTEGER PRIMARY KEY,
            image_hash TEXT,
            description TEXT,
            full_path TEXT,
            image_type TEXT,
            query_count INTEGER DEFAULT 0,
            is_registered INTEGER DEFAULT 1,
            is_banned INTEGER DEFAULT 0,
            no_file_flag INTEGER DEFAULT 0,
            last_used_time TEXT
        );
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            platform TEXT,
            user_id TEXT,
            user_nickname TEXT,
            user_cardname TEXT,
            group_id TEXT
        );
        CREATE TABLE bot_platform_accounts (
            id INTEGER PRIMARY KEY,
            platform TEXT,
            account_id TEXT,
            disabled INTEGER DEFAULT 0
        );
        """
    )
    connection.executemany(
        "INSERT INTO mai_messages (message_id, timestamp, platform, user_id, "
        "user_nickname, user_cardname, group_id, session_id, is_at, is_picture, "
        "is_command, processed_plain_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("m1", "2026-11-01 09:00:00", "qq", "u1", "阿一", "一群的一号", "1001",
             "qq_group_1001", 1, 0, 0, "早上好"),
            ("m2", "2026-11-01 09:05:00", "qq", "u2", "阿二", None, None,
             "qq_private_u2", 0, 1, 0, "在吗"),
            ("m3", "2026-11-01 09:06:00", "qq", "u9", "机器人", None, "1001",
             "qq_group_1001", 0, 0, 0, "（出站）"),
            ("m4", "2026-11-01 09:07:00", "telegram", "u3", "Tele", None, "2002",
             "tg_group_2002", 0, 0, 0, "hi"),
            ("m5", "2026-11-01 09:08:00", "matrix", "u4", "Mat", None, "3003",
             "mx_group_3003", 0, 0, 0, "hi"),
            ("", "2026-11-01 09:09:00", "qq", "u5", "无名", None, "1001",
             "qq_group_1001", 0, 0, 0, "没有 id"),
        ],
    )
    connection.execute(
        "INSERT INTO bot_platform_accounts (platform, account_id) VALUES ('qq', 'u9')"
    )
    connection.executemany(
        "INSERT INTO person_info (person_id, person_name, platform, user_id, "
        "user_nickname, group_cardname, memory_points, know_counts, last_known_time) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "p1",
                "一号",
                "qq",
                "u1",
                "阿一",
                json.dumps([{"group_id": "1001", "group_cardname": "一群的一号"}]),
                json.dumps(["喜好:喜欢咖啡:0.9", "习惯:经常熬夜:0.7"]),
                5,
                "2026-11-02 10:00:00",
            ),
            ("p2", "二号", "qq", "u2", "阿二", None, None, 0, None),
        ],
    )
    connection.executemany(
        "INSERT INTO expressions (situation, style, content_list, count, "
        "last_active_time, create_time, session_id) VALUES (?,?,?,?,?,?,?)",
        [
            ("被叫到", "卖萌", json.dumps(["好的呀", "好哒"]), 7,
             "2026-11-02 10:00:00", "2026-11-01 10:00:00", "qq_group_1001"),
            ("被拒绝", "撒娇", json.dumps(["行吧"]), 2,
             "2026-11-02 10:00:00", "2026-11-01 10:00:00", None),
        ],
    )
    connection.executemany(
        "INSERT INTO jargons (content, meaning, session_id_dict, count, is_jargon, "
        "is_complete, is_global, created_by, created_timestamp, updated_timestamp, "
        "evidence_messages) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("吃瓜", "围观", json.dumps({"qq_group_1001": 2}), 9, 1, 1, 0, "AI",
             "2026-11-01 10:00:00", "2026-11-02 10:00:00",
             json.dumps([[{"platform": "qq", "message_id": "m1"}], []])),
            ("不是黑话", "普通词", "{}", 1, 0, 0, 0, "AI",
             "2026-11-01 10:00:00", "2026-11-02 10:00:00", None),
            ("全局梗", "到处都在用", "{}", 3, 1, 0, 1, "MANUAL",
             "2026-11-01 10:00:00", "2026-11-02 10:00:00", None),
        ],
    )
    connection.executemany(
        "INSERT INTO images (image_hash, description, full_path, image_type, "
        "query_count, is_banned, no_file_flag, last_used_time) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("h1", "开心,点赞", "data/emoji/a.jpg", "emoji", 3, 0, 0,
             "2026-11-02 10:00:00"),
            ("h2", "普通图片", "data/emoji/a.jpg", "IMAGE", 0, 0, 0, None),
            ("h3", "文件没了", "data/emoji/missing.jpg", "emoji", 0, 0, 1, None),
        ],
    )
    connection.executemany(
        "INSERT INTO chat_sessions (session_id, platform, user_id, group_id) "
        "VALUES (?,?,?,?)",
        [
            ("qq_group_1001", "qq", None, "1001"),
            ("qq_private_u2", "qq", "u2", None),
        ],
    )
    connection.commit()
    connection.close()

    memory = sqlite3.connect(data / "a-memorix" / "metadata.db")
    memory.executescript(
        """
        CREATE TABLE paragraphs (
            hash TEXT PRIMARY KEY,
            content TEXT,
            knowledge_type TEXT,
            created_at REAL,
            event_time REAL,
            is_deleted INTEGER DEFAULT 0
        );
        """
    )
    memory.executemany(
        "INSERT INTO paragraphs (hash, content, knowledge_type, created_at, "
        "event_time, is_deleted) VALUES (?,?,?,?,?,?)",
        [
            ("k1", "用户喜欢在周五晚上打游戏", "fact", 1700000000.0, 1700000000.0, 0),
            ("k2", "某次活动是在春天举办的", "episode", 1700000100.0, 1700000100.0, 0),
            ("k3", "已删除的记忆", "note", 1700000200.0, 1700000200.0, 1),
        ],
    )
    memory.commit()
    memory.close()
    return root


@pytest_asyncio.fixture
async def life_db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest_asyncio.fixture
async def service(life_db, tmp_path):
    migration = MigrationService(life_db, tmp_path / "life")
    await migration.ensure_schema()
    return migration


async def _count(db, model) -> int:
    async with db.get_db() as session:
        rows = (await session.execute(select(model))).scalars().all()
    return len(rows)


def test_to_ts_handles_seconds_milliseconds_and_text():
    assert to_ts(1700000000) == 1700000000
    assert to_ts(1700000000123) == 1700000000
    assert to_ts("2026-11-01 09:00:00") > 0
    assert to_ts(None) == 0
    assert to_ts("not a date") == 0


def test_build_session_key_maps_platforms():
    assert build_session_key("qq", "1001") == UMO
    assert build_session_key("qq", None, "u2") == "aiocqhttp:FriendMessage:u2"
    assert build_session_key("telegram", "2002") == "telegram:GroupMessage:2002"
    assert build_session_key("matrix", "3003") is None
    assert build_session_key("qq") is None


@pytest.mark.asyncio
async def test_preflight_reports_layout(service, tmp_path):
    root = _source(tmp_path)

    report = await service.preflight(root)

    names = {item["name"]: item for item in report["datasets"]}
    assert set(names) == {"messages", "people", "expressions", "jargon", "emoji", "memory"}
    assert names["messages"]["rows"] == 6
    assert names["memory"]["rows"] == 3
    assert report["memory_db"] is not None
    assert report["sessions"]["mapped"] == 2
    assert report["sessions"]["unmapped_platforms"] == ["matrix"]
    assert report["warnings"] == []


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(service, life_db, tmp_path):
    root = _source(tmp_path)

    report = await service.run(root, umos=[UMO], dry_run=True)

    assert report["dry_run"] is True
    assert report["batch_id"] is None
    assert report["totals"]["written"] == 0
    messages = report["datasets"]["messages"]
    assert messages["seen"] == 6
    assert messages["ready"] == 3
    assert messages["skipped_reasons"] == {
        "bot_message": 1,
        "missing_id": 1,
        "unknown_platform": 1,
    }
    assert report["datasets"]["people"]["detail"] == {"people": 2, "facts": 2}
    assert report["datasets"]["expressions"]["ready"] == 3
    assert report["datasets"]["jargon"]["skipped_reasons"] == {"not_jargon": 1}
    assert report["datasets"]["emoji"]["ready"] == 1
    assert report["datasets"]["emoji"]["skipped_reasons"] == {
        "not_emoji": 1,
        "missing_file": 1,
    }
    assert report["datasets"]["memory"]["ready"] == 2
    assert report["datasets"]["memory"]["skipped_reasons"] == {"deleted": 1}
    assert await _count(life_db, LifeMessage) == 0
    assert await _count(life_db, LifePerson) == 0


@pytest.mark.asyncio
async def test_import_writes_every_dataset(service, life_db, tmp_path):
    root = _source(tmp_path)

    report = await service.run(root, umos=[UMO], dry_run=False)

    assert report["batch_id"] is not None
    assert report["totals"]["written"] == 3 + 4 + 3 + 2 + 1 + 2
    assert await _count(life_db, LifeMessage) == 3
    assert await _count(life_db, LifePerson) == 2
    assert await _count(life_db, LifePersonFact) == 2
    assert await _count(life_db, LifeExpression) == 3
    assert await _count(life_db, LifeJargon) == 2
    assert await _count(life_db, LifeEmoji) == 1
    assert await _count(life_db, LifeMemory) == 2

    async with life_db.get_db() as session:
        messages = (await session.execute(select(LifeMessage))).scalars().all()
        people = (await session.execute(select(LifePerson))).scalars().all()
        facts = (await session.execute(select(LifePersonFact))).scalars().all()
        expressions = (await session.execute(select(LifeExpression))).scalars().all()
        jargon = (await session.execute(select(LifeJargon))).scalars().all()
        emoji = (await session.execute(select(LifeEmoji))).scalars().all()
        memories = (await session.execute(select(LifeMemory))).scalars().all()

    by_id = {row.message_id: row for row in messages}
    assert set(by_id) == {"m1", "m2", "m4"}
    assert by_id["m1"].session_key == UMO
    assert by_id["m1"].flags["at"] is True
    assert by_id["m1"].sender_name == "一群的一号"
    assert by_id["m2"].session_key == "aiocqhttp:FriendMessage:u2"
    assert by_id["m2"].flags["image"] is True
    assert by_id["m4"].session_key == "telegram:GroupMessage:2002"
    assert {row.person_id for row in people} == {"p1", "p2"}
    assert {row.content for row in facts} == {"喜欢咖啡", "经常熬夜"}
    assert {row.category for row in facts} == {"喜好", "习惯"}
    assert {row.style for row in expressions} == {"好的呀", "好哒", "行吧"}
    assert {row.term for row in jargon} == {"吃瓜", "全局梗"}
    assert all(row.scope == "global" for row in memories)
    assert all(row.meta["source"] == "a_memorix" for row in memories)
    assert emoji[0].session_key == UMO
    assert Path(emoji[0].file_path).read_bytes() == JPEG
    assert emoji[0].tags == ["开心", "点赞"]


@pytest.mark.asyncio
async def test_import_is_idempotent(service, life_db, tmp_path):
    root = _source(tmp_path)
    await service.run(root, umos=[UMO], dry_run=False)

    second = await service.run(root, umos=[UMO], dry_run=False)

    assert second["totals"]["written"] == 0
    assert await _count(life_db, LifeMessage) == 3
    assert await _count(life_db, LifeMemory) == 2


@pytest.mark.asyncio
async def test_rollback_deletes_only_the_batch(service, life_db, tmp_path):
    root = _source(tmp_path)
    report = await service.run(root, umos=[UMO], dry_run=False)

    rolled = await service.rollback(report["batch_id"])

    assert rolled["status"] == "rolled_back"
    assert rolled["deleted"]["life_messages"] == 3
    assert await _count(life_db, LifeMessage) == 0
    assert await _count(life_db, LifePerson) == 0
    assert await _count(life_db, LifePersonFact) == 0
    assert await _count(life_db, LifeExpression) == 0
    assert await _count(life_db, LifeJargon) == 0
    assert await _count(life_db, LifeEmoji) == 0
    assert await _count(life_db, LifeMemory) == 0
    runs = await service.runs()
    assert runs[0]["status"] == "rolled_back"


@pytest.mark.asyncio
async def test_rollback_keeps_later_rows(service, life_db, tmp_path):
    root = _source(tmp_path)
    report = await service.run(root, umos=[UMO], dry_run=False)
    async with life_db.get_db() as session:
        session.add(LifeMessage(
            platform_id="aiocqhttp",
            session_key=UMO,
            message_id="live-after-import",
            ts=1,
            components=[],
            plain_text="x",
            flags={},
        ))
        await session.commit()

    await service.rollback(report["batch_id"])

    async with life_db.get_db() as session:
        remaining = (await session.execute(select(LifeMessage))).scalars().all()
    assert [row.message_id for row in remaining] == ["live-after-import"]


@pytest.mark.asyncio
async def test_rollback_rejects_unknown_batch(service):
    with pytest.raises(ValueError):
        await service.rollback("nope")


@pytest.mark.asyncio
async def test_unknown_dataset_is_rejected(service, tmp_path):
    root = _source(tmp_path)
    with pytest.raises(ValueError):
        await service.run(root, datasets=["unknown"])


@pytest.mark.asyncio
async def test_emoji_needs_a_target_session(service, tmp_path):
    root = _source(tmp_path)
    with pytest.raises(ValueError):
        await service.run(root, datasets=["emoji"], dry_run=True)


@pytest.mark.asyncio
async def test_missing_source_table_is_reported(service, tmp_path):
    root = _source(tmp_path)
    connection = sqlite3.connect(root / "data" / "MaiBot.db")
    connection.execute("DROP TABLE mai_messages")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="mai_messages"):
        await service.run(root, umos=[UMO], dry_run=False)


@pytest.mark.asyncio
async def test_failed_batch_rollback_is_a_noop(service, life_db, tmp_path):
    async with life_db.get_db() as session:
        session.add(
            LifeMessage(
                platform_id="aiocqhttp",
                session_key=UMO,
                message_id="pre-existing",
                ts=1,
                components=[],
                plain_text="x",
                flags={},
            )
        )
        await session.commit()
    root = _source(tmp_path)
    connection = sqlite3.connect(root / "data" / "MaiBot.db")
    connection.execute("DROP TABLE mai_messages")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError):
        await service.run(root, datasets=["messages"], dry_run=False)

    runs = await service.runs()
    assert runs[0]["status"] == "failed"
    rolled = await service.rollback(runs[0]["batch_id"])

    assert rolled["deleted"] == {}
    assert await _count(life_db, LifeMessage) == 1


@pytest.mark.asyncio
async def test_runs_lists_batches(service, tmp_path):
    root = _source(tmp_path)
    first = await service.run(root, datasets=["messages"], umos=[UMO], dry_run=False)
    second = await service.run(root, datasets=["messages"], umos=[UMO], dry_run=False)

    runs = await service.runs()

    assert {row["batch_id"] for row in runs} == {first["batch_id"], second["batch_id"]}
    assert all(row["status"] == "completed" for row in runs)


class _FakeMigration:
    """Stand-in for MigrationService in endpoint tests."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.rolled: list[str] = []

    async def ensure_schema(self) -> None:
        return None

    async def runs(self, limit: int):
        return [{"batch_id": "b1", "status": "completed"}][:limit]

    async def preflight(self, source, **_kwargs):
        if self.fail:
            raise ValueError("source is not a MaiBot deployment")
        return {"source": source, "warnings": []}

    async def run(self, source, **_kwargs):
        if self.fail:
            raise ValueError("source is not a MaiBot deployment")
        return {"source": source, "dry_run": True, "turns": 1}

    async def rollback(self, batch: str):
        self.rolled.append(batch)
        return {"batch_id": batch, "status": "rolled_back", "deleted": {}}


@pytest.mark.asyncio
async def test_migration_runs_endpoint(monkeypatch):
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    fake = _FakeMigration()
    service = SimpleNamespace(migration_service=lambda: fake)
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    payload = await life_api.life_migration_runs(limit=10, _auth=None)

    assert payload["data"]["enabled"] is True
    assert payload["data"]["runs"][0]["batch_id"] == "b1"


@pytest.mark.asyncio
async def test_migration_preview_endpoint(monkeypatch):
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    fake = _FakeMigration()
    service = SimpleNamespace(migration_service=lambda: fake)
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    payload = await life_api.life_migration_preview(
        payload={"source": "/srv/maibot", "umo": "x"},
        _auth=None,
    )

    assert payload["data"]["inspection"]["source"] == "/srv/maibot"
    assert payload["data"]["report"]["dry_run"] is True


@pytest.mark.asyncio
async def test_migration_preview_rejects_bad_source(monkeypatch):
    from bot.dashboard.api import life as life_api
    from bot.dashboard.responses import ApiError
    import bot.life.service as life_service_module

    fake = _FakeMigration(fail=True)
    service = SimpleNamespace(migration_service=lambda: fake)
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    with pytest.raises(ApiError):
        await life_api.life_migration_preview(payload={"source": "/nope"}, _auth=None)


@pytest.mark.asyncio
async def test_migration_rollback_endpoint(monkeypatch):
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    fake = _FakeMigration()
    service = SimpleNamespace(migration_service=lambda: fake)
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    payload = await life_api.life_migration_rollback(payload={"batch": "b1"}, _auth=None)

    assert payload["data"]["status"] == "rolled_back"
    assert fake.rolled == ["b1"]


@pytest.mark.asyncio
async def test_migration_endpoints_without_service(monkeypatch):
    from bot.dashboard.api import life as life_api
    from bot.dashboard.responses import ApiError
    import bot.life.service as life_service_module

    monkeypatch.setattr(life_service_module, "get_life_service", lambda: None)

    payload = await life_api.life_migration_runs(limit=5, _auth=None)
    assert payload["data"]["enabled"] is False
    with pytest.raises(ApiError):
        await life_api.life_migration_preview(payload={"source": "/x"}, _auth=None)
