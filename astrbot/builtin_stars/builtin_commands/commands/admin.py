from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain


class AdminCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def update_dashboard(self, event: AstrMessageEvent) -> None:
        """告知管理面板已随社区站发布，不再由机器人更新。"""
        await event.send(
            MessageChain().message(
                "The dashboard ships with the community website and is no longer "
                "updated by the bot."
            )
        )
