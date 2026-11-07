"""Tests for BotMessageEvent class."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.core.message.components import (
    At,
    AtAll,
    ButtonInteraction,
    Face,
    Forward,
    Image,
    Plain,
    Reply,
)
from bot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from bot.core.pipeline.respond.stage import RespondStage
from bot.core.platform.bot_message_event import (
    BotMessageEvent,
    current_message_event,
    event_context,
    streaming_response_event,
)
from bot.core.platform.bot_message import BotMessage, Group, MessageMember
from bot.core.platform.message_type import MessageType
from bot.core.platform.platform_metadata import PlatformMetadata
from bot.core.star.context import Context


class ConcreteBotMessageEvent(BotMessageEvent):
    """Concrete implementation of BotMessageEvent for testing purposes."""

    async def send(self, message):
        """Send message implementation."""
        await super().send(message)


@pytest.fixture
def platform_meta():
    """Create platform metadata for testing."""
    return PlatformMetadata(
        name="test_platform",
        description="Test platform",
        id="test_platform_id",
    )


@pytest.fixture
def message_member():
    """Create a message member for testing."""
    return MessageMember(user_id="user123", nickname="TestUser")


@pytest.fixture
def bot_message(message_member):
    """Create an BotMessage for testing."""
    message = BotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "bot123"
    message.session_id = "session123"
    message.message_id = "msg123"
    message.sender = message_member
    message.message = [Plain(text="Hello world")]
    message.message_str = "Hello world"
    message.raw_message = None
    return message


@pytest.fixture
def bot_message_event(platform_meta, bot_message):
    """Create an BotMessageEvent instance for testing."""
    return ConcreteBotMessageEvent(
        message_str="Hello world",
        message_obj=bot_message,
        platform_meta=platform_meta,
        session_id="session123",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_response", [False, True])
async def test_streaming_trace_only_records_final_response(
    bot_message_event, monkeypatch, tool_response
):
    """Aggregate adapter chunks without hiding independent tool responses."""
    event = bot_message_event
    event.trace = MagicMock()
    monkeypatch.setattr(
        "bot.core.platform.bot_message_event.Metric.upload", AsyncMock()
    )

    async def stream():
        yield MessageChain([Plain("Hello")])
        if tool_response:
            await event.send(MessageChain([Plain("Tool reply")]))
        yield MessageChain([Plain("hidden reasoning")], type="reasoning")
        yield MessageChain([], type="break")
        yield MessageChain([Plain(" world")])

    async def deliver(generator, use_fallback):
        async for chunk in generator:
            if chunk.type not in {"reasoning", "break"}:
                await event.send(chunk)
        # The QQ streaming protocol may require an empty closing frame.
        await event.send(MessageChain([Plain("\n")]))

    event.send_streaming = AsyncMock(side_effect=deliver)
    event.set_result(
        MessageEventResult()
        .set_result_content_type(ResultContentType.STREAMING_RESULT)
        .set_async_stream(stream())
    )
    stage = RespondStage()
    stage.config = {"provider_settings": {}}
    await stage.process(event)

    records = event.trace.record.call_args_list
    assert [record.args[0] for record in records] == ["response_sent"] * (
        2 if tool_response else 1
    )
    assert [record.kwargs["message"] for record in records] == (
        ["Tool reply", "Hello world"] if tool_response else ["Hello world"]
    )
    assert records[-1].kwargs["streaming"] is True
    assert "source" not in records[-1].kwargs
    assert streaming_response_event.get() is None
    await event.send(MessageChain([Plain("Normal reply")]))
    assert event.trace.record.call_args.kwargs["message"] == "Normal reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
async def test_streaming_trace_context_restored_after_failure(
    bot_message_event, failure
):
    """Failed or cancelled delivery must not suppress subsequent responses."""
    event = bot_message_event
    event.trace = MagicMock()
    event.send_streaming = AsyncMock(side_effect=failure("delivery interrupted"))

    async def stream():
        yield MessageChain([Plain("Hello")])

    event.set_result(
        MessageEventResult()
        .set_result_content_type(ResultContentType.STREAMING_RESULT)
        .set_async_stream(stream())
    )
    stage = RespondStage()
    stage.config = {"provider_settings": {}}
    with pytest.raises(failure):
        await stage.process(event)
    event.trace.record.assert_not_called()
    assert streaming_response_event.get() is None


@pytest.mark.asyncio
async def test_streaming_outline_limit_does_not_truncate_delivery(bot_message_event):
    """Bound diagnostic content without changing the outgoing stream."""
    event = bot_message_event
    event.trace = MagicMock()
    fragment = "字" * 20000
    delivered = []

    async def stream():
        yield MessageChain([Plain(fragment)])
        yield MessageChain([Plain("End")])

    async def deliver(generator, use_fallback):
        async for chunk in generator:
            delivered.append(chunk.chain[0].text)

    event.send_streaming = AsyncMock(side_effect=deliver)
    event.set_result(
        MessageEventResult()
        .set_result_content_type(ResultContentType.STREAMING_RESULT)
        .set_async_stream(stream())
    )
    stage = RespondStage()
    stage.config = {"provider_settings": {}}
    await stage.process(event)
    event.trace.record.assert_called_once()
    fields = event.trace.record.call_args.kwargs
    assert len(fields["message"].encode()) <= 4096
    assert fields["truncated"] is True
    assert delivered == [fragment, "End"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_type",
    [
        ResultContentType.GENERAL_RESULT,
        ResultContentType.LLM_RESULT,
        ResultContentType.STREAMING_RESULT,
    ],
)
async def test_response_trace_omits_sending_owner(
    bot_message_event, monkeypatch, result_type
):
    """Record delivered responses without attributing a sending owner."""
    event = bot_message_event
    event.trace = MagicMock()
    monkeypatch.setattr(
        "bot.core.platform.bot_message_event.Metric.upload", AsyncMock()
    )
    monkeypatch.setattr(
        "bot.core.pipeline.respond.stage.call_event_hook",
        AsyncMock(return_value=False),
    )

    async def stream():
        yield MessageChain([Plain("Reply")])

    async def deliver(generator, use_fallback):
        async for chunk in generator:
            await event.send(chunk)

    result = MessageEventResult().set_result_content_type(result_type)
    if result_type == ResultContentType.STREAMING_RESULT:
        result.set_async_stream(stream())
        event.send_streaming = AsyncMock(side_effect=deliver)
    else:
        result.message("Reply")
    event.set_result(result)
    stage = RespondStage()
    stage.config = {"provider_settings": {}}
    stage.platform_settings = {"path_mapping": []}
    stage.enable_seg = False
    await stage.process(event)

    sent = [
        record
        for record in event.trace.record.call_args_list
        if record.args[0] == "response_sent"
    ]
    assert len(sent) == 1
    assert "source" not in sent[0].kwargs
    assert sent[0].kwargs["message"] == "Reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "missing", "failure"])
async def test_public_context_send_records_only_successful_delivery(
    bot_message_event, outcome
):
    """Attach successful public sends to the active event without a source."""
    event = bot_message_event
    event.trace = MagicMock()
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="target"),
        send_by_session=AsyncMock(
            side_effect=RuntimeError("send failed") if outcome == "failure" else None
        ),
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[] if outcome == "missing" else [platform]
        ),
        get_config=lambda **kwargs: {},
    )
    with event_context(event):
        assert current_message_event.get() is event
        if outcome == "failure":
            with pytest.raises(RuntimeError, match="send failed"):
                await Context.send_message(
                    context,
                    "target:FriendMessage:other",
                    MessageChain([Plain("Hello")]),
                )
        else:
            assert await Context.send_message(
                context, "target:FriendMessage:other", MessageChain([Plain("Hello")])
            ) is (outcome == "success")
    deliveries = [
        call
        for call in event.trace.record.call_args_list
        if call.args[0] == "response_sent"
    ]
    assert len(deliveries) == (1 if outcome == "success" else 0)
    if deliveries:
        assert deliveries[0].kwargs["message"] == "Hello"
        assert deliveries[0].kwargs["target_umo"] == "target:FriendMessage:other"
        assert "source" not in deliveries[0].kwargs
    assert current_message_event.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_public_context_send_skips_unscoped_and_stream_chunk_records(
    bot_message_event, scoped
):
    """Do not invent traces or duplicate streaming chunk delivery records."""
    event = bot_message_event
    event.trace = MagicMock()
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="target"),
                    send_by_session=AsyncMock(),
                )
            ]
        ),
        get_config=lambda **kwargs: {},
    )
    if scoped:
        with event_context(event):
            token = streaming_response_event.set(event)
            try:
                await Context.send_message(
                    context,
                    "target:FriendMessage:other",
                    MessageChain([Plain("Chunk")]),
                )
            finally:
                streaming_response_event.reset(token)
    else:
        await Context.send_message(
            context, "target:FriendMessage:other", MessageChain([Plain("Hello")])
        )
    assert all(
        call.args[0] != "response_sent" for call in event.trace.record.call_args_list
    )


@pytest.mark.asyncio
async def test_nested_event_forwarding_records_delivery_once(monkeypatch):
    """Record nested public sends only once without duplicating event metrics."""
    from bot.core.platform.agent_wakeup_event import AgentWakeupEvent
    from bot.core.platform.message_session import MessageSession

    monkeypatch.setattr(
        "bot.core.platform.bot_message_event.Metric.upload", AsyncMock()
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="target"),
                    send_by_session=AsyncMock(),
                )
            ]
        ),
        get_config=lambda **kwargs: {},
    )

    async def send_message(session, chain):
        """Dispatch through the real public context method."""
        return await Context.send_message(context, session, chain)

    context.send_message = send_message
    event = AgentWakeupEvent(
        context=context,
        session=MessageSession.from_str("target:FriendMessage:user"),
        platform_meta=PlatformMetadata(
            id="target", name="test-platform", description="Test"
        ),
        prompt="Scheduled",
        source="cron",
    )
    event.trace = MagicMock()
    with event_context(event):
        await event.send(MessageChain([Plain("Hello")]))
    deliveries = [
        call
        for call in event.trace.record.call_args_list
        if call.args[0] == "response_sent"
    ]
    assert len(deliveries) == 1
    assert deliveries[0].kwargs["message"] == "Hello"
    assert event._has_send_oper is True


def test_retained_attachment_is_excluded_from_event_cleanup(
    bot_message_event, tmp_path
):
    source = tmp_path / "source.jpg"
    derived = tmp_path / "model_image.jpg"
    source.write_bytes(b"original")
    derived.write_bytes(b"prepared")
    event = bot_message_event
    event.track_temporary_local_file(str(source))
    event.track_temporary_local_file(str(source))
    event.track_temporary_local_file(str(derived))
    event.untrack_temporary_local_file(str(source))
    event.untrack_temporary_local_file(str(source))
    event.untrack_temporary_local_file(str(tmp_path / "untracked"))
    assert event._temporary_local_files == [str(derived)]
    event.cleanup_temporary_local_files()
    event.cleanup_temporary_local_files()
    assert source.read_bytes() == b"original"
    assert not derived.exists()
    assert event._temporary_local_files == []


class TestBotMessageEventInit:
    """Tests for BotMessageEvent initialization."""

    def test_init_basic(self, bot_message_event):
        """Test basic BotMessageEvent initialization."""
        assert bot_message_event.message_str == "Hello world"
        assert bot_message_event.role == "member"
        assert bot_message_event.is_wake is False
        assert bot_message_event.is_at_or_wake_command is False
        assert bot_message_event._extras == {}
        assert bot_message_event._result is None
        assert bot_message_event.call_llm is False

    def test_init_session(self, bot_message_event):
        """Test session initialization."""
        assert bot_message_event.session_id == "session123"
        assert bot_message_event.session.platform_name == "test_platform_id"

    def test_init_platform_reference(self, bot_message_event, platform_meta):
        """Test platform reference initialization."""
        assert bot_message_event.platform_meta == platform_meta
        assert bot_message_event.platform == platform_meta  # back compatibility

    def test_init_created_at(self, bot_message_event):
        """Test created_at timestamp is set."""
        assert bot_message_event.created_at is not None
        assert isinstance(bot_message_event.created_at, float)

    def test_init_trace(self, bot_message_event):
        """Test trace/span initialization."""
        assert bot_message_event.trace is not None
        assert bot_message_event.span is not None
        assert bot_message_event.trace == bot_message_event.span

    def test_button_interaction_accessors(self, bot_message_event):
        interaction = ButtonInteraction(
            action_id="approve",
            data={"request_id": "req-1"},
            interaction_id="interaction-1",
            source_message_id="message-1",
        )
        bot_message_event.message_obj.message.append(interaction)

        assert bot_message_event.is_button_interaction()
        assert bot_message_event.get_button_interaction() == interaction


class TestUnifiedMsgOrigin:
    """Tests for unified_msg_origin property."""

    def test_unified_msg_origin_getter(self, bot_message_event):
        """Test unified_msg_origin getter."""
        expected = "test_platform_id:FriendMessage:session123"
        assert bot_message_event.unified_msg_origin == expected

    def test_unified_msg_origin_setter(self, bot_message_event):
        """Test unified_msg_origin setter."""
        bot_message_event.unified_msg_origin = "new_platform:GroupMessage:new_session"

        assert bot_message_event.session.platform_name == "new_platform"
        assert bot_message_event.session.session_id == "new_session"


class TestSessionId:
    """Tests for session_id property."""

    def test_session_id_getter(self, bot_message_event):
        """Test session_id getter."""
        assert bot_message_event.session_id == "session123"

    def test_session_id_setter(self, bot_message_event):
        """Test session_id setter."""
        bot_message_event.session_id = "new_session_id"

        assert bot_message_event.session_id == "new_session_id"


class TestGetPlatformInfo:
    """Tests for platform info methods."""

    def test_get_platform_name(self, bot_message_event):
        """Test get_platform_name method."""
        assert bot_message_event.get_platform_name() == "test_platform"

    def test_get_platform_id(self, bot_message_event):
        """Test get_platform_id method."""
        assert bot_message_event.get_platform_id() == "test_platform_id"


class TestGetMessageInfo:
    """Tests for message info methods."""

    def test_get_message_str(self, bot_message_event):
        """Test get_message_str method."""
        assert bot_message_event.get_message_str() == "Hello world"

    def test_get_message_str_none(self, platform_meta, bot_message):
        """Test get_message_str keeps None when source message_str is None."""
        bot_message.message_str = None
        event = ConcreteBotMessageEvent(
            message_str=None,
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.get_message_str() is None

    def test_get_messages(self, bot_message_event):
        """Test get_messages method."""
        messages = bot_message_event.get_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], Plain)
        assert messages[0].text == "Hello world"

    def test_get_message_type(self, bot_message_event):
        """Test get_message_type method."""
        assert bot_message_event.get_message_type() == MessageType.FRIEND_MESSAGE

    def test_get_session_id(self, bot_message_event):
        """Test get_session_id method."""
        assert bot_message_event.get_session_id() == "session123"

    def test_get_group_id_empty_for_private(self, bot_message_event):
        """Test get_group_id returns empty for private messages."""
        assert bot_message_event.get_group_id() == ""

    def test_get_self_id(self, bot_message_event):
        """Test get_self_id method."""
        assert bot_message_event.get_self_id() == "bot123"

    def test_get_sender_id(self, bot_message_event):
        """Test get_sender_id method."""
        assert bot_message_event.get_sender_id() == "user123"

    def test_get_sender_name(self, bot_message_event):
        """Test get_sender_name method."""
        assert bot_message_event.get_sender_name() == "TestUser"

    def test_get_sender_name_empty_when_none(self, platform_meta, bot_message):
        """Test get_sender_name returns empty string when nickname is None."""
        bot_message.sender = MessageMember(user_id="user123", nickname=None)
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.get_sender_name() == ""

    def test_get_sender_name_coerces_non_string(self, platform_meta, bot_message):
        """Test get_sender_name stringifies non-string nickname values."""
        bot_message.sender = MessageMember(user_id="user123", nickname=None)
        bot_message.sender.nickname = 12345
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.get_sender_name() == "12345"


class TestGetMessageOutline:
    """Tests for get_message_outline method."""

    def test_outline_plain_text(self, bot_message_event):
        """Test outline with plain text message."""
        outline = bot_message_event.get_message_outline()
        assert "Hello world" in outline

    def test_outline_with_image(self, platform_meta, bot_message):
        """Test outline with image component."""
        bot_message.message = [
            Plain(text="Look at this"),
            Image(file="http://example.com/img.jpg"),
        ]
        event = ConcreteBotMessageEvent(
            message_str="Look at this",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "Look at this" in outline
        assert "[图片]" in outline

    def test_outline_with_at(self, platform_meta, bot_message):
        """Test outline with At component."""
        bot_message.message = [At(qq="12345"), Plain(text=" hello")]
        event = ConcreteBotMessageEvent(
            message_str=" hello",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "[At:12345]" in outline

    def test_outline_with_at_all(self, platform_meta, bot_message):
        """Test outline with AtAll component."""
        bot_message.message = [AtAll()]
        event = ConcreteBotMessageEvent(
            message_str="",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert outline == "[At:全体成员]"

    def test_outline_with_face(self, platform_meta, bot_message):
        """Test outline with Face component."""
        bot_message.message = [Face(id="123")]
        event = ConcreteBotMessageEvent(
            message_str="",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "[表情:123]" in outline

    def test_outline_with_forward(self, platform_meta, bot_message):
        """Test outline with Forward component."""
        # Forward requires an id parameter
        bot_message.message = [Forward(id="test_forward_id")]
        event = ConcreteBotMessageEvent(
            message_str="",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "[转发消息]" in outline

    def test_outline_with_reply(self, platform_meta, bot_message):
        """Test outline with Reply component."""
        # Reply requires an id parameter
        reply = Reply(id="test_reply_id")
        reply.message_str = "Original message"
        reply.sender_nickname = "Sender"
        bot_message.message = [reply, Plain(text=" reply")]
        event = ConcreteBotMessageEvent(
            message_str=" reply",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "[引用消息(Sender: Original message)]" in outline

    def test_outline_with_reply_no_message(self, platform_meta, bot_message):
        """Test outline with Reply component without message_str."""
        # Reply requires an id parameter
        reply = Reply(id="test_reply_id")
        reply.message_str = None
        bot_message.message = [reply]
        event = ConcreteBotMessageEvent(
            message_str="",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert "[引用消息]" in outline

    def test_outline_empty_chain(self, platform_meta, bot_message):
        """Test outline with empty message chain."""
        bot_message.message = []
        event = ConcreteBotMessageEvent(
            message_str="",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert outline == ""

    def test_outline_very_long_plain_text(self, platform_meta, bot_message):
        """Test outline generation for very long plain text content."""
        long_text = "A" * 20000
        bot_message.message = [Plain(text=long_text)]
        event = ConcreteBotMessageEvent(
            message_str=long_text,
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        outline = event.get_message_outline()
        assert outline.startswith("A")
        assert len(outline) >= 20000


class TestExtras:
    """Tests for extra information methods."""

    def test_set_extra(self, bot_message_event):
        """Test set_extra method."""
        bot_message_event.set_extra("key1", "value1")
        assert bot_message_event._extras["key1"] == "value1"

    def test_get_extra_with_key(self, bot_message_event):
        """Test get_extra with specific key."""
        bot_message_event.set_extra("key1", "value1")
        assert bot_message_event.get_extra("key1") == "value1"

    def test_get_extra_with_default(self, bot_message_event):
        """Test get_extra with default value."""
        result = bot_message_event.get_extra("nonexistent", "default_value")
        assert result == "default_value"

    def test_get_extra_all(self, bot_message_event):
        """Test get_extra without key returns all extras."""
        bot_message_event.set_extra("key1", "value1")
        bot_message_event.set_extra("key2", "value2")
        all_extras = bot_message_event.get_extra()
        assert all_extras == {"key1": "value1", "key2": "value2"}

    def test_clear_extra(self, bot_message_event):
        """Test clear_extra method."""
        bot_message_event.set_extra("key1", "value1")
        bot_message_event.clear_extra()
        assert bot_message_event._extras == {}


class TestSetResult:
    """Tests for set_result method."""

    def test_set_result_with_message_event_result(self, bot_message_event):
        """Test set_result with MessageEventResult object."""
        result = MessageEventResult().message("Test message")
        bot_message_event.set_result(result)

        assert bot_message_event._result == result

    def test_set_result_with_string(self, bot_message_event):
        """Test set_result with string creates MessageEventResult."""
        bot_message_event.set_result("Test message")

        assert bot_message_event._result is not None
        assert len(bot_message_event._result.chain) == 1
        assert isinstance(bot_message_event._result.chain[0], Plain)

    def test_set_result_with_empty_chain(self, bot_message_event):
        """Test set_result handles empty chain correctly."""
        result = MessageEventResult()
        # chain is already an empty list by default
        bot_message_event.set_result(result)

        assert bot_message_event._result.chain == []


class TestStopContinueEvent:
    """Tests for stop_event and continue_event methods."""

    def test_stop_event_creates_result_if_none(self, bot_message_event):
        """Test stop_event creates result if none exists."""
        bot_message_event.stop_event()

        assert bot_message_event._result is not None
        assert bot_message_event.is_stopped() is True

    def test_stop_event_with_existing_result(self, bot_message_event):
        """Test stop_event with existing result."""
        bot_message_event.set_result(MessageEventResult().message("Test"))
        bot_message_event.stop_event()

        assert bot_message_event.is_stopped() is True

    def test_continue_event_creates_result_if_none(self, bot_message_event):
        """Test continue_event creates result if none exists."""
        bot_message_event.continue_event()

        assert bot_message_event._result is not None
        assert bot_message_event.is_stopped() is False

    def test_continue_event_with_existing_result(self, bot_message_event):
        """Test continue_event with existing result."""
        bot_message_event.set_result(MessageEventResult().message("Test"))
        bot_message_event.stop_event()
        bot_message_event.continue_event()

        assert bot_message_event.is_stopped() is False

    def test_is_stopped_default_false(self, bot_message_event):
        """Test is_stopped returns False by default."""
        assert bot_message_event.is_stopped() is False


class TestIsPrivateChat:
    """Tests for is_private_chat method."""

    def test_is_private_chat_true(self, bot_message_event):
        """Test is_private_chat returns True for friend message."""
        assert bot_message_event.is_private_chat() is True

    def test_is_private_chat_false(self, platform_meta, bot_message):
        """Test is_private_chat returns False for group message."""
        bot_message.type = MessageType.GROUP_MESSAGE
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=bot_message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.is_private_chat() is False


class TestIsWakeUp:
    """Tests for is_wake_up method."""

    def test_is_wake_up_default_false(self, bot_message_event):
        """Test is_wake_up returns False by default."""
        assert bot_message_event.is_wake_up() is False

    def test_is_wake_up_when_set(self, bot_message_event):
        """Test is_wake_up returns True when is_wake is set."""
        bot_message_event.is_wake = True
        assert bot_message_event.is_wake_up() is True


class TestIsAdmin:
    """Tests for is_admin method."""

    def test_is_admin_default_false(self, bot_message_event):
        """Test is_admin returns False by default."""
        assert bot_message_event.is_admin() is False

    def test_is_admin_when_admin(self, bot_message_event):
        """Test is_admin returns True when role is admin."""
        bot_message_event.role = "admin"
        assert bot_message_event.is_admin() is True


class TestProcessBuffer:
    """Tests for process_buffer method."""

    @pytest.mark.asyncio
    async def test_process_buffer_splits_by_pattern(self, bot_message_event):
        """Test process_buffer splits buffer by pattern."""
        buffer = "Line 1\nLine 2\nLine 3\nRemaining"
        pattern = re.compile(r".*\n")

        with patch.object(
            bot_message_event, "send", new_callable=AsyncMock
        ) as mock_send:
            result = await bot_message_event.process_buffer(buffer, pattern)

            # Should have sent 3 lines and remaining should be "Remaining"
            assert mock_send.call_count == 3
            assert result == "Remaining"

    @pytest.mark.asyncio
    async def test_process_buffer_no_match(self, bot_message_event):
        """Test process_buffer returns original when no match."""
        buffer = "No newlines here"
        pattern = re.compile(r"\n")

        result = await bot_message_event.process_buffer(buffer, pattern)

        assert result == "No newlines here"


class TestResultHelpers:
    """Tests for result helper methods."""

    def test_make_result(self, bot_message_event):
        """Test make_result creates empty MessageEventResult."""
        result = bot_message_event.make_result()
        assert isinstance(result, MessageEventResult)

    def test_plain_result(self, bot_message_event):
        """Test plain_result creates result with text."""
        result = bot_message_event.plain_result("Hello")

        assert isinstance(result, MessageEventResult)
        assert len(result.chain) == 1
        assert isinstance(result.chain[0], Plain)
        assert result.chain[0].text == "Hello"

    def test_image_result_url(self, bot_message_event):
        """Test image_result with URL."""
        result = bot_message_event.image_result("http://example.com/image.jpg")

        assert isinstance(result, MessageEventResult)
        assert len(result.chain) == 1
        assert isinstance(result.chain[0], Image)

    def test_image_result_path(self, bot_message_event):
        """Test image_result with file path."""
        result = bot_message_event.image_result("/path/to/image.jpg")

        assert isinstance(result, MessageEventResult)
        assert len(result.chain) == 1
        assert isinstance(result.chain[0], Image)


class TestGetResult:
    """Tests for get_result and clear_result methods."""

    def test_get_result_returns_none_by_default(self, bot_message_event):
        """Test get_result returns None by default."""
        assert bot_message_event.get_result() is None

    def test_get_result_returns_set_result(self, bot_message_event):
        """Test get_result returns set result."""
        result = MessageEventResult().message("Test")
        bot_message_event.set_result(result)

        assert bot_message_event.get_result() == result

    def test_clear_result(self, bot_message_event):
        """Test clear_result clears the result."""
        bot_message_event.set_result(MessageEventResult().message("Test"))
        bot_message_event.clear_result()

        assert bot_message_event.get_result() is None


class TestShouldCallLlm:
    """Tests for should_call_llm method."""

    def test_should_call_llm_default(self, bot_message_event):
        """Test call_llm default is False."""
        assert bot_message_event.call_llm is False

    def test_should_call_llm_when_set(self, bot_message_event):
        """Test should_call_llm sets call_llm."""
        bot_message_event.should_call_llm(True)
        assert bot_message_event.call_llm is True


class TestRequestLlm:
    """Tests for request_llm method."""

    def test_request_llm_basic(self, bot_message_event):
        """Test request_llm creates ProviderRequest."""
        request = bot_message_event.request_llm(prompt="Hello")

        assert request.prompt == "Hello"
        assert request.session_id == ""
        assert request.image_urls == []
        assert request.contexts == []

    def test_request_llm_with_all_params(self, bot_message_event):
        """Test request_llm with all parameters."""
        request = bot_message_event.request_llm(
            prompt="Hello",
            session_id="session123",
            image_urls=["http://example.com/img.jpg"],
            contexts=[{"role": "user", "content": "Hi"}],
            system_prompt="You are helpful",
        )

        assert request.prompt == "Hello"
        assert request.session_id == "session123"
        assert request.image_urls == ["http://example.com/img.jpg"]
        assert request.contexts == [{"role": "user", "content": "Hi"}]
        assert request.system_prompt == "You are helpful"


class TestSendStreaming:
    """Tests for send_streaming method."""

    @pytest.mark.asyncio
    async def test_send_streaming_sets_has_send_oper(self, bot_message_event):
        """Test send_streaming sets _has_send_oper flag."""
        assert bot_message_event._has_send_oper is False

        async def generator():
            yield MessageEventResult().message("Test")

        with patch(
            "bot.core.platform.bot_message_event.Metric.upload",
            new_callable=AsyncMock,
        ):
            await bot_message_event.send_streaming(generator())

        assert bot_message_event._has_send_oper is True


class TestSendTyping:
    """Tests for send_typing method."""

    @pytest.mark.asyncio
    async def test_send_typing_default_empty(self, bot_message_event):
        """Test send_typing default implementation is empty."""
        # Should not raise any exception
        await bot_message_event.send_typing()


class TestStopTyping:
    """Tests for stop_typing method."""

    @pytest.mark.asyncio
    async def test_stop_typing_default_empty(self, bot_message_event):
        """Test stop_typing default implementation is empty."""
        await bot_message_event.stop_typing()


class TestReact:
    """Tests for react method."""

    @pytest.mark.asyncio
    async def test_react_sends_emoji(self, bot_message_event):
        """Test react sends emoji as message."""
        with patch.object(
            bot_message_event, "send", new_callable=AsyncMock
        ) as mock_send:
            await bot_message_event.react("👍")

            mock_send.assert_called_once()
            call_arg = mock_send.call_args[0][0]
            # MessageChain is a dataclass with chain attribute
            assert len(call_arg.chain) == 1
            assert isinstance(call_arg.chain[0], Plain)
            assert call_arg.chain[0].text == "👍"


class TestGetGroup:
    """Tests for get_group method."""

    @pytest.mark.asyncio
    async def test_get_group_returns_none_for_private(self, bot_message_event):
        """Test get_group returns None for private chat."""
        result = await bot_message_event.get_group()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_group_with_group_id_param(self, bot_message_event):
        """Test get_group with group_id parameter."""
        result = await bot_message_event.get_group(group_id="group123")
        assert result == Group(group_id="group123")

    @pytest.mark.asyncio
    async def test_get_group_returns_message_group(self, bot_message_event):
        """Test get_group returns group data already attached to the message."""
        bot_message_event.message_obj.group = Group(
            group_id="group123",
            group_name="Test Group",
        )

        result = await bot_message_event.get_group()

        assert result is bot_message_event.message_obj.group


class TestMessageTypeHandling:
    """Tests for message type handling edge cases."""

    def test_message_type_from_valid_string(self, platform_meta):
        """Valid MessageType string should be converted correctly."""
        message = BotMessage()
        message.type = "FRIEND_MESSAGE"
        message.message = []
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.session.message_type == MessageType.FRIEND_MESSAGE
        assert event.get_message_type() == MessageType.FRIEND_MESSAGE

    def test_message_type_from_invalid_string_defaults_to_friend(self, platform_meta):
        """Invalid message type should default to FRIEND_MESSAGE."""
        message = BotMessage()
        message.type = "InvalidMessageType"
        message.message = []
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.session.message_type == MessageType.FRIEND_MESSAGE
        assert event.get_message_type() == MessageType.FRIEND_MESSAGE

    def test_message_type_from_none_defaults_to_friend(self, platform_meta):
        """None message type should default to FRIEND_MESSAGE."""
        message = BotMessage()
        message.type = None
        message.message = []
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.session.message_type == MessageType.FRIEND_MESSAGE
        assert event.get_message_type() == MessageType.FRIEND_MESSAGE

    def test_message_type_from_integer_defaults_to_friend(self, platform_meta):
        """Integer message type should default to FRIEND_MESSAGE."""
        message = BotMessage()
        message.type = 123
        message.message = []
        event = ConcreteBotMessageEvent(
            message_str="test",
            message_obj=message,
            platform_meta=platform_meta,
            session_id="session123",
        )
        assert event.session.message_type == MessageType.FRIEND_MESSAGE
        assert event.get_message_type() == MessageType.FRIEND_MESSAGE


class TestDefensiveGetattr:
    """Tests for defensive getattr behavior in BotMessageEvent."""

    def test_get_messages_without_message_attr(self, bot_message_event):
        """get_messages should handle message_obj without 'message' attribute."""
        bot_message_event.message_obj = type("DummyMessage", (), {})()
        messages = bot_message_event.get_messages()
        assert isinstance(messages, list)

    def test_get_message_type_without_type_attr(self, bot_message_event):
        """get_message_type should handle message_obj without 'type' attribute."""
        bot_message_event.message_obj = type("DummyMessage", (), {})()
        message_type = bot_message_event.get_message_type()
        assert isinstance(message_type, MessageType)

    def test_get_sender_fields_without_sender_attr(self, bot_message_event):
        """get_sender_id and get_sender_name should handle missing 'sender'."""
        bot_message_event.message_obj = type("DummyMessage", (), {})()
        sender_id = bot_message_event.get_sender_id()
        sender_name = bot_message_event.get_sender_name()
        assert isinstance(sender_id, str)
        assert isinstance(sender_name, str)

    def test_get_message_type_with_non_enum_type(self, bot_message_event):
        """get_message_type should handle message_obj.type that is not a MessageType."""

        class DummyMessage:
            def __init__(self):
                self.type = "not_an_enum"
                self.message = []

        bot_message_event.message_obj = DummyMessage()
        message_type = bot_message_event.get_message_type()
        assert isinstance(message_type, MessageType)
