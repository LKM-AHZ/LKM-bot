"""Tests for BotMessage and MessageMember classes."""

import time
from unittest.mock import patch

from bot.core.message.components import Image, Plain
from bot.core.platform.bot_message import BotMessage, Group, MessageMember
from bot.core.platform.message_type import MessageType


class TestMessageMember:
    """Tests for MessageMember dataclass."""

    def test_message_member_creation_basic(self):
        """Test creating a MessageMember with required fields."""
        member = MessageMember(user_id="user123")

        assert member.user_id == "user123"
        assert member.nickname is None

    def test_message_member_creation_with_nickname(self):
        """Test creating a MessageMember with nickname."""
        member = MessageMember(user_id="user123", nickname="TestUser")

        assert member.user_id == "user123"
        assert member.nickname == "TestUser"

    def test_message_member_str_with_nickname(self):
        """Test __str__ method with nickname."""
        member = MessageMember(user_id="user123", nickname="TestUser")
        result = str(member)

        assert "User ID: user123" in result
        assert "Nickname: TestUser" in result

    def test_message_member_str_without_nickname(self):
        """Test __str__ method without nickname."""
        member = MessageMember(user_id="user123")
        result = str(member)

        assert "User ID: user123" in result
        assert "Nickname: N/A" in result


class TestGroup:
    """Tests for Group dataclass."""

    def test_group_creation_basic(self):
        """Test creating a Group with required fields."""
        group = Group(group_id="group123")

        assert group.group_id == "group123"
        assert group.group_name is None
        assert group.group_avatar is None
        assert group.group_owner is None
        assert group.group_admins is None
        assert group.members is None
        assert group.member_count is None

    def test_group_creation_with_all_fields(self):
        """Test creating a Group with all fields."""
        members = [MessageMember(user_id="user1"), MessageMember(user_id="user2")]
        group = Group(
            group_id="group123",
            group_name="Test Group",
            group_avatar="http://example.com/avatar.jpg",
            group_owner="owner123",
            group_admins=["admin1", "admin2"],
            members=members,
            member_count=2,
        )

        assert group.group_id == "group123"
        assert group.group_name == "Test Group"
        assert group.group_avatar == "http://example.com/avatar.jpg"
        assert group.group_owner == "owner123"
        assert group.group_admins == ["admin1", "admin2"]
        assert group.members == members
        assert group.member_count == 2

    def test_group_str_with_all_fields(self):
        """Test __str__ method with all fields."""
        members = [MessageMember(user_id="user1", nickname="User One")]
        group = Group(
            group_id="group123",
            group_name="Test Group",
            group_avatar="http://example.com/avatar.jpg",
            group_owner="owner123",
            group_admins=["admin1"],
            members=members,
            member_count=1,
        )
        result = str(group)

        assert "Group ID: group123" in result
        assert "Name: Test Group" in result
        assert "Avatar: http://example.com/avatar.jpg" in result
        assert "Owner ID: owner123" in result
        assert "Admin IDs: ['admin1']" in result
        assert "Member Count: 1" in result
        assert "Members Len: 1" in result

    def test_group_str_with_minimal_fields(self):
        """Test __str__ method with minimal fields."""
        group = Group(group_id="group123")
        result = str(group)

        assert "Group ID: group123" in result
        assert "Name: N/A" in result
        assert "Avatar: N/A" in result
        assert "Owner ID: N/A" in result
        assert "Admin IDs: N/A" in result
        assert "Member Count: N/A" in result
        assert "Members Len: 0" in result
        assert "First Member: N/A" in result


class TestBotMessage:
    """Tests for BotMessage class."""

    def test_bot_message_creation(self):
        """Test creating an BotMessage."""
        message = BotMessage()

        assert message.group is None
        assert message.timestamp is not None
        assert isinstance(message.timestamp, int)

    def test_bot_message_timestamp(self):
        """Test timestamp is set on creation."""
        with patch.object(time, "time", return_value=1234567890):
            message = BotMessage()
            assert message.timestamp == 1234567890

    def test_bot_message_all_attributes(self):
        """Test setting all attributes on BotMessage."""
        message = BotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "bot123"
        message.session_id = "session123"
        message.message_id = "msg123"
        message.sender = MessageMember(user_id="user123", nickname="TestUser")
        message.message = [Plain(text="Hello")]
        message.message_str = "Hello"
        message.raw_message = {"raw": "data"}

        assert message.type == MessageType.FRIEND_MESSAGE
        assert message.self_id == "bot123"
        assert message.session_id == "session123"
        assert message.message_id == "msg123"
        assert message.sender.user_id == "user123"
        assert len(message.message) == 1
        assert message.message_str == "Hello"
        assert message.raw_message == {"raw": "data"}

    def test_bot_message_str(self):
        """Test __str__ method."""
        message = BotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "bot123"

        result = str(message)
        assert "'type'" in result
        assert "'self_id'" in result


class TestBotMessageGroupId:
    """Tests for BotMessage group handling."""

    def test_group_is_none_without_a_group(self):
        """A message without a group keeps group None."""
        message = BotMessage()
        assert message.group is None

    def test_group_id_returns_group_id_when_group_exists(self):
        """Group id is read from the group object."""
        message = BotMessage()
        message.group = Group(group_id="group123")

        assert message.group.group_id == "group123"

    def test_set_group_creates_new_group(self):
        """set_group creates a group when none exists."""
        message = BotMessage()
        message.set_group("new_group123")

        assert message.group is not None
        assert message.group.group_id == "new_group123"

    def test_set_group_replaces_existing_group(self):
        """set_group replaces the existing group's id."""
        message = BotMessage()
        message.group = Group(group_id="old_group")
        message.set_group("new_group")

        assert message.group.group_id == "new_group"

    def test_set_group_with_none_removes_group(self):
        """set_group(None) removes the group."""
        message = BotMessage()
        message.group = Group(group_id="group123")
        message.set_group(None)

        assert message.group is None

    def test_set_group_with_empty_string_removes_group(self):
        """set_group("") removes the group."""
        message = BotMessage()
        message.group = Group(group_id="group123")
        message.set_group("")

        assert message.group is None


class TestBotMessageTypes:
    """Tests for BotMessage with different message types."""

    def test_friend_message_type(self):
        """Test BotMessage with FRIEND_MESSAGE type."""
        message = BotMessage()
        message.type = MessageType.FRIEND_MESSAGE

        assert message.type == MessageType.FRIEND_MESSAGE
        assert message.type.value == "FriendMessage"

    def test_group_message_type(self):
        """Test BotMessage with GROUP_MESSAGE type."""
        message = BotMessage()
        message.type = MessageType.GROUP_MESSAGE

        assert message.type == MessageType.GROUP_MESSAGE
        assert message.type.value == "GroupMessage"

    def test_other_message_type(self):
        """Test BotMessage with OTHER_MESSAGE type."""
        message = BotMessage()
        message.type = MessageType.OTHER_MESSAGE

        assert message.type == MessageType.OTHER_MESSAGE
        assert message.type.value == "OtherMessage"


class TestBotMessageChain:
    """Tests for BotMessage message chain."""

    def test_message_chain_with_plain_text(self):
        """Test message chain with plain text."""
        message = BotMessage()
        message.message = [Plain(text="Hello world")]

        assert len(message.message) == 1
        assert isinstance(message.message[0], Plain)
        assert message.message[0].text == "Hello world"

    def test_message_chain_with_multiple_components(self):
        """Test message chain with multiple components."""
        message = BotMessage()
        message.message = [
            Plain(text="Hello "),
            Plain(text="world"),
            Image(file="http://example.com/img.jpg"),
        ]

        assert len(message.message) == 3
        assert isinstance(message.message[0], Plain)
        assert isinstance(message.message[1], Plain)
        assert isinstance(message.message[2], Image)

    def test_message_chain_empty(self):
        """Test empty message chain."""
        message = BotMessage()
        message.message = []

        assert len(message.message) == 0
