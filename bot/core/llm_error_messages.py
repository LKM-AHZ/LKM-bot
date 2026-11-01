"""Translations for built-in LLM request failures."""

LLM_ERROR_MESSAGES = {
    "zh-CN": {
        "invalidSessionProviderType": "该会话来源的对话模型（提供商）的类型不正确: {provider_type}",
        "noProvider": "LLM 请求失败：未找到任何可用的对话模型（提供商）。请先在 WebUI 中配置并启用可用模型。",
        "providerNotFound": "LLM 请求失败：未找到指定的提供商 `{provider}`。请检查提供商配置或重新选择可用模型。",
        "invalidProviderType": "LLM 请求失败：选择的提供商类型无效（{provider_type}），已跳过本次请求。",
        "requestFailed": "LLM 请求失败：{detail}",
        "blockedProvider": "LLM 请求失败：Provider API base `{api_base}` 因安全原因被拦截，请更换可用的 AI 提供商。",
    },
    "en-US": {
        "invalidSessionProviderType": "The chat model (provider) for this session has an invalid type: {provider_type}",
        "noProvider": "LLM request failed: No available chat model (provider) was found. Configure and enable a model in WebUI first.",
        "providerNotFound": "LLM request failed: Provider `{provider}` was not found. Check the provider configuration or select an available model.",
        "invalidProviderType": "LLM request failed: The selected provider type ({provider_type}) is invalid. This request was skipped.",
        "requestFailed": "LLM request failed: {detail}",
        "blockedProvider": "LLM request failed: Provider API base `{api_base}` was blocked for security reasons. Use another AI provider.",
    },
}
