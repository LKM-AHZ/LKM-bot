from ..register import register_provider_adapter
from .openai_source import ProviderOpenAIOfficial


@register_provider_adapter(
    "openrouter_chat_completion", "OpenRouter Chat Completion Provider Adapter"
)
class ProviderOpenRouter(ProviderOpenAIOfficial):
    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        # Reference to: https://openrouter.ai/docs/api/reference/overview#headers
        self.client._custom_headers["HTTP-Referer"] = (  # type: ignore
            "https://github.com/Alma1314/LKM-bot"
        )
        self.client._custom_headers["X-OpenRouter-Title"] = "LKMBot"  # type: ignore
        self.client._custom_headers["X-OpenRouter-Categories"] = (  # ty: ignore[invalid-assignment]  # SDK 私有属性运行时为 dict，桩件标注为只读 Mapping
            "general-chat,personal-agent"  # type: ignore
        )
        self.reasoning_key = "reasoning"
