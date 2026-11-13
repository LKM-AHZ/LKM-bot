from collections.abc import AsyncGenerator

from bot.core.config.agent import normalize_agent
from bot.core.platform.agent_wakeup_event import AgentWakeupEvent
from bot.core.platform.bot_message_event import BotMessageEvent

from ...context import PipelineContext
from ...stage import Stage
from .agent_sub_stages.internal import InternalAgentSubStage
from .agent_sub_stages.third_party import ThirdPartyAgentSubStage


class AgentRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.ctx = ctx
        self.config = ctx.bot_config

        agent_runner = normalize_agent(self.config.get("agent"))
        self.config["agent"] = agent_runner
        agent_runner_type = agent_runner["runtime"]
        self.runtime_sub_stage = InternalAgentSubStage()
        await self.runtime_sub_stage.initialize(ctx)
        if agent_runner_type in ctx.agent_runtimes:
            self.agent_sub_stage = self.runtime_sub_stage
        elif agent_runner_type in ("dify", "coze", "dashscope", "deerflow"):
            self.agent_sub_stage = ThirdPartyAgentSubStage()
            await self.agent_sub_stage.initialize(ctx)
        else:
            raise RuntimeError(f"Agent runtime unavailable: {agent_runner_type}")

    async def process(self, event: BotMessageEvent) -> AsyncGenerator[None, None]:  # ty: ignore[invalid-method-override]  # 基类声明为协程、子类用 yield 实现为异步生成器（同一层级两种调用形态），ty 的 LSP 检查无法表达该联合契约
        config = self.ctx.plugin_manager.context.get_config(
            umo=event.unified_msg_origin
        )
        if not config["provider_settings"]["enable"]:
            return

        provider_wake_prefix = config["provider_settings"]["wake_prefix"]
        for prefix in config["wake_prefix"]:
            if provider_wake_prefix.startswith(prefix):
                provider_wake_prefix = provider_wake_prefix[len(prefix) :]

        agent_config = config.get("agent", {})
        runtime_name = (
            event.agent_session.runtime
            if isinstance(event, AgentWakeupEvent) and event.agent_session is not None
            else agent_config.get("runtime", "bot")
        )
        event.trace.record(
            "agent_start",
            kind="agent",
            status="running",
            runner_type=runtime_name,
            provider_wake_prefix=provider_wake_prefix,
        )
        try:
            stage = (
                self.runtime_sub_stage
                if isinstance(event, AgentWakeupEvent)
                and event.agent_session is not None
                else self.agent_sub_stage
            )
            async for resp in stage.process(event, provider_wake_prefix):
                yield resp
        except BaseException as exc:
            event.trace.record(
                "agent_end",
                kind="agent",
                status="error",
                error=str(exc),
                runner_type=runtime_name,
            )
            raise
        else:
            event.trace.record(
                "agent_end",
                kind="agent",
                status="success",
                runner_type=runtime_name,
            )
