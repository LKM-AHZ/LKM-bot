from collections.abc import AsyncGenerator

from bot.core import logger
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.platform.message_type import MessageType

from ..context import PipelineContext
from ..stage import Stage, register_stage


@register_stage
class WhitelistCheckStage(Stage):
    """检查是否在群聊/私聊白名单"""

    async def initialize(self, ctx: PipelineContext) -> None:
        self.enable_whitelist_check = ctx.bot_config["platform_settings"][
            "enable_id_white_list"
        ]
        self.whitelist = ctx.bot_config["platform_settings"]["id_whitelist"]
        self.whitelist = [
            str(i).strip() for i in self.whitelist if str(i).strip() != ""
        ]
        self.wl_ignore_admin_on_group = ctx.bot_config["platform_settings"][
            "wl_ignore_admin_on_group"
        ]
        self.wl_ignore_admin_on_friend = ctx.bot_config["platform_settings"][
            "wl_ignore_admin_on_friend"
        ]
        self.wl_log = ctx.bot_config["platform_settings"]["id_whitelist_log"]

    async def process(
        self,
        event: BotMessageEvent,
    ) -> None | AsyncGenerator[None, None]:
        if not self.enable_whitelist_check:
            # 白名单检查未启用
            return

        if len(self.whitelist) == 0:
            # 白名单为空，不检查
            return

        if event.get_platform_name() == "webchat":
            # WebChat 豁免
            return

        # 检查是否在白名单
        if self.wl_ignore_admin_on_group:
            if (
                event.role == "admin"
                and event.get_message_type() == MessageType.GROUP_MESSAGE
            ):
                return
        if self.wl_ignore_admin_on_friend:
            if (
                event.role == "admin"
                and event.get_message_type() == MessageType.FRIEND_MESSAGE
            ):
                return
        if (
            event.unified_msg_origin not in self.whitelist
            and str(event.get_group_id()).strip() not in self.whitelist
        ):
            if self.wl_log:
                logger.info(
                    f"Session ID {event.unified_msg_origin} is not in the session "
                    "allowlist, so event propagation was stopped. Add this session "
                    "ID in WebUI: Config -> Access Control -> Whitelist -> "
                    "Whitelist ID List, using the profile assigned to this bot.",
                )
            event.stop_event()
