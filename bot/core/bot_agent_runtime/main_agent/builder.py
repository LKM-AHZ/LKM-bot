"""The single entry point for building a ready-to-run main agent."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.core import logger
from bot.core.agent.tool import ToolSet
from bot.core.agent_input import CollectedInput
from bot.core.bot_agent_runtime.context import (
    BotAgentContext,
)
from bot.core.bot_agent_runtime.main_agent.providers import decoded_blocked
from bot.core.bot_agent_runtime.runner import AgentRunner
from bot.core.bot_agent_runtime.tools.cron_tools import FutureTaskTool
from bot.core.bot_agent_runtime.tools.message_tools import (
    GetGroupMessageHistoryTool,
    SendMessageToUserTool,
)
from bot.core.llm_error_messages import LLM_ERROR_MESSAGE_EXTRA_KEY
from bot.core.pipeline.context_utils import call_event_hook
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.platform.message_type import MessageType
from bot.core.provider import Provider
from bot.core.provider.entities import ProviderRequest
from bot.core.skills.skill_manager import SkillInfo
from bot.core.star import agent_capabilities as plugins
from bot.core.star.context import Context
from bot.core.star.star_handler import EventType
from bot.core.utils.image_input import prepare_request_images

from . import (
    computer_use,
    knowledge_base,
    persona,
    providers,
    request,
    web_search,
    workspace,
)
from .config import MainAgentBuildConfig

if TYPE_CHECKING:
    from bot.core.bot_agent_runtime.runtime import BotAgentRuntime

TOOL_CALL_PROMPT = (
    "<tool_use_instructions>\n"
    "When using tools: "
    "never return an empty response; "
    "briefly explain the purpose when starting a new type of task, but not before every tool call; "
    "follow the tool schema exactly and do not invent parameters; "
    "keep the conversation style consistent."
    "\n</tool_use_instructions>"
)

TOOL_CALL_PROMPT_SKILLS_LIKE_MODE = (
    "<tool_use_instructions>\n"
    "You MUST NOT return an empty response, especially after invoking a tool."
    " Before calling any tool, provide a brief explanatory message to the user stating the purpose of the tool call."
    " Tool schemas are provided in two stages: first only name and description; "
    "if you decide to use a tool, the full parameter schema will be provided in "
    "a follow-up step. Do not guess arguments before you see the schema."
    " After the tool call is completed, you must briefly summarize the results returned by the tool for the user."
    " Keep the role-play and style consistent throughout the conversation."
    "\n</tool_use_instructions>"
)

LIVE_MODE_SYSTEM_PROMPT = (
    "<live_mode_instructions>\n"
    "You are in a real-time conversation. "
    "Speak like a real person, casual and natural. "
    "Keep replies short, one thought at a time. "
    "No templates, no lists, no formatting. "
    "No parentheses, quotes, or markdown. "
    "It is okay to pause, hesitate, or speak in fragments. "
    "Respond to tone and emotion. "
    "Simple questions get simple answers. "
    "Sound like a real conversation, not a Q&A system."
    "\n</live_mode_instructions>"
)


@dataclass(slots=True)
class MainAgentBuildResult:
    """An initialized main agent together with its prepared request and provider."""

    agent_runner: AgentRunner
    provider_request: ProviderRequest
    provider: Provider


async def build_main_agent(
    *,
    event: BotMessageEvent,
    plugin_context: Context,
    config: MainAgentBuildConfig,
    provider: Provider | None = None,
    req: ProviderRequest | None = None,
    collected_input: CollectedInput | None = None,
    plugin_tools: ToolSet | None = None,
    plugin_skills: list[SkillInfo] | None = None,
    runtime: "BotAgentRuntime | None" = None,
) -> MainAgentBuildResult | None:
    """Build a main agent through the same ordered stages for every trigger.

    Args:
        event: Triggering event, including session identity and permissions.
        plugin_context: Provider, conversation, persona, and tool services.
        config: Resolved session settings.
        provider: Explicit model, or None to select the session's model.
        req: Explicit request, or None to collect input from the event.
        collected_input: Core-collected input and temporary media ownership.
        plugin_tools: Core-authorized plugin tools, or None for legacy lookup.
        plugin_skills: Core-authorized plugin skills, or None for legacy lookup.
        runtime: Native session owner supplying internal agent services.

    Returns:
        An initialized runner with its request and provider, or None when the
        provider/input is unavailable, validation fails, or a hook stops the run.
    """
    # Resolve the model and prepare content before composing capabilities.
    provider = provider or await providers.select_chat_provider(event, plugin_context)
    if provider is None:
        logger.info("No chat provider is available; skipping the request.")
        if not event.get_extra(LLM_ERROR_MESSAGE_EXTRA_KEY):
            providers.set_llm_error_message(event, "noProvider")
        return None
    prepared = await request.prepare_request(
        event, plugin_context, config, provider, req, collected_input
    )
    if prepared is None:
        return None
    req = prepared.request

    # Seed caller capabilities before persona selection.
    tool_mgr = plugin_context.get_llm_tool_manager()
    if config.enable_message_delivery:
        if req.func_tool is None:
            req.func_tool = ToolSet()
        req.func_tool.add_tool(tool_mgr.get_builtin_tool(SendMessageToUserTool))
    await persona.apply_persona(
        req,
        config.provider_settings,
        plugin_context,
        event,
        plugin_tools=plugin_tools,
        plugin_skills=plugin_skills,
    )

    # Add session context before capability instructions.
    await workspace.apply_workspace_instructions(event, req, plugin_context)
    await knowledge_base.apply_knowledge_base(event, req, plugin_context, config)

    # Session plugin filtering precedes builtins, which have their own settings.
    if req.func_tool is not None:
        req.func_tool = plugins.filter_tools(event, req.func_tool)
    search_instructions = web_search.apply_web_search(
        req, plugin_context, config.provider_settings, event
    )
    if config.llm_safety_mode:
        persona.apply_safety_instructions(config, req)
    agent_context = BotAgentContext(
        context=plugin_context,
        event=event,
        plugin_tools=plugin_tools,
        plugin_skills=plugin_skills,
        _runtime=runtime,
    )
    await computer_use.apply_computer_use(req, plugin_context, config, agent_context)

    builtin_types = []
    if config.add_cron_tools:
        builtin_types.append(FutureTaskTool)
    if event.platform_meta.support_proactive_message:
        builtin_types.append(SendMessageToUserTool)
    if (
        event.get_message_type() == MessageType.GROUP_MESSAGE
        and config.group_message_history_enabled
    ):
        builtin_types.append(GetGroupMessageHistoryTool)
    if builtin_types:
        if req.func_tool is None:
            req.func_tool = ToolSet()
        for tool_type in builtin_types:
            req.func_tool.add_tool(tool_mgr.get_builtin_tool(tool_type))

    # Final instructions describe the assembled capabilities and output mode.
    if req.func_tool and req.func_tool.tools:
        tool_prompt = (
            TOOL_CALL_PROMPT
            if config.tool_schema_mode == "full"
            else TOOL_CALL_PROMPT_SKILLS_LIKE_MODE
        )
        req.system_prompt += f"\n{tool_prompt}\n"
    if event.get_extra("action_type") == "live":
        req.system_prompt += f"\n{LIVE_MODE_SYSTEM_PROMPT}\n"
    req.system_prompt += search_instructions

    # Final model selection happens after captioning, so only remaining images count.
    fallbacks = providers.resolve_fallback_providers(
        provider, plugin_context, config.fallback_provider_ids
    )
    selected_provider = providers.select_image_provider(provider, req, fallbacks)
    if selected_provider is not provider:
        provider = selected_provider
        req.model = None
        fallbacks = [item for item in fallbacks if item is not provider]
    await prepare_request_images(
        req,
        event,
        max_size=prepared.image_max_size,
        prepared=prepared.images,
        captioned_refs=prepared.captioned_refs,
        supports_image=providers.supports_modality(provider, "image"),
    )

    # Endpoint validation is independent of plugin hook dispatch.
    if config.validate_provider_endpoint:
        api_base = provider.provider_config.get("api_base", "")
        if any(host in api_base for host in decoded_blocked):
            providers.set_llm_error_message(event, "blockedProvider", api_base=api_base)
            logger.error(
                "Provider API base %s was blocked for security reasons", api_base
            )
            return None
    if config.call_request_hook:
        if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
            return None
        # Life context runs after plugin hooks so injected memories and
        # expressions are not overwritten by a prompt rewrite.
        from bot.life.memory.inject import inject_life_context

        await inject_life_context(event, req)
        # Hooks may add images; preserve the cache from input preparation.
        await prepare_request_images(
            req,
            event,
            max_size=prepared.image_max_size,
            prepared=prepared.images,
            supports_image=providers.supports_modality(provider, "image"),
        )

    from bot.core.bot_agent_runtime.execution import create_native_runner

    agent_runner = await create_native_runner(
        plugin_context,
        provider,
        req,
        config,
        agent_context,
        fallback_providers=fallbacks,
    )
    return MainAgentBuildResult(
        agent_runner=agent_runner, provider_request=req, provider=provider
    )
