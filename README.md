> **LKMBot** 是本仓库维护的 LKM 品牌发行版，基于上游 AstrBot 开发。内部 Python 包名为
> `bot`，面向用户的产品名和主命令均为 `LKMBot` / `lkmbot`。

## 代码结构与开发入口

目录与入口核对日期：2026-11-01。开发环境需要 Python 3.12+；发行名为 LKMBot，内部
Python 包名为 `bot`。

| 路径 | 职责 |
|---|---|
| `main.py`、`runtime_bootstrap.py` | 启动参数、运行路径初始化与源码启动入口 |
| `bot/core/initial_loader.py`、`bot/core/core_lifecycle.py` | Bot Core 与 Dashboard 的启动和关闭 |
| `bot/core/pipeline/`、`bot/core/platform/`、`bot/core/provider/`、`bot/core/star/` | 消息阶段、平台适配、模型与插件生命周期 |
| `bot/api/`、`bot/builtin_stars/` | 插件公共 API 与内置插件 |
| `bot/dashboard/api/`、`bot/dashboard/services/`、`bot/dashboard/server.py` | FastAPI 路由、服务逻辑、Hypercorn 与静态资源 |
| `bot/lkm/` | 社区 SSO 与面板子路径集成 |
| `dashboard/src/` | Vue WebUI，开发命令见 [Dashboard 指南](./dashboard/README.md) |
| `openspec/`、`tests/`、`docs/` | API 规格、回归测试与中英文使用/开发文档 |
| `scripts/` | 构建、发布和 API 文档工具 |

先在仓库根目录执行 `uv sync`、`uv run main.py`；另开终端进入 `dashboard/` 执行
`pnpm install`、`pnpm dev`。社区集成优先放在 `bot/lkm/`，沿用 `bot` 命名空间。
`dashboard/src/api/generated/` 和打包后的 `bot/dashboard/dist/` 不直接编辑；API 变更后
在 `dashboard/` 运行 `pnpm generate:api`，同步相应测试和文档。

Dashboard 配置服务按职责归属文件：`config_profile_service.py` 管理配置档和系统设置，
`config_routing_service.py` 管理会话配置路由，`config_display_service.py` 构造编辑器元数据，
`config_file_service.py` 处理配置附件，`bot_config_service.py` / `provider_config_service.py`
管理平台与模型源。共享校验和保存逻辑保留在 `config_service.py`；
`bot/dashboard/api/app.py` 统一装配，各路由直接导入所需服务。

嵌入 LKM-Website 时，完整栈部署以父仓库的 [部署手册](../docs/DEPLOYMENT.md) 为准；
单独克隆时可查看 [LKM-Website 文档](https://github.com/LKM-AHZ/LKM-Website/tree/master/docs)。

## 主要功能

1. 💯 免费 & 开源。
2. ✨ AI 大模型对话，多模态，Agent，MCP，Skills，知识库，人格设定，自动压缩对话。
3. 🤖 支持接入 Dify、阿里云百炼、Coze 等智能体平台。
4. 🌐 多平台，支持 QQ、企业微信、飞书、钉钉、微信公众号、Telegram、Slack 以及[更多](#支持的消息平台)。
5. 📦 插件扩展，已有 1000+ 个插件可一键安装。
6. 🛡️ [Agent Sandbox](https://docs.astrbot.app/use/astrbot-agent-sandbox.html) 隔离化环境，安全地执行任何代码、调用 Shell、会话级资源复用。
7. 💻 WebUI 支持。
8. 🌈 Web ChatUI 支持，ChatUI 内置代理沙盒、网页搜索等。
9. 🌐 国际化（i18n）支持。

## 快速开始

### 一键部署

> [!NOTE]
> 需要安装 [uv](https://docs.astral.sh/uv/)。
> 对于 macOS 用户：由于 macOS 安全检查，首次运行 `lkmbot` 命令可能需要较长时间（约 10-20 秒）。

对于想快速体验 LKMBot、且熟悉命令行并能够自行安装 `uv` 环境的用户，我们推荐使用 `uv` 一键部署方式 ⚡️。

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
uv tool install . --python 3.12
lkmbot init # 仅首次执行此命令以初始化环境
lkmbot run
```

后续更新 `lkmbot`：

```bash
git pull --ff-only
uv tool install --force . --python 3.12
```

### Docker 部署

对于熟悉容器、希望获得更稳定且更适合生产环境部署方式的用户，我们推荐使用 Docker / Docker Compose 部署 LKMBot。
## 支持的消息平台

将 LKMBot 连接到你常用的聊天平台。

| 平台 | 维护方 |
|---------|---------------|
| **QQ** | 官方维护 |
| **OneBot v11** | 官方维护 |
| **Telegram** | 官方维护 |
| **企微应用 & 企微智能机器人** | 官方维护 |
| **微信公众号** | 官方维护 |
| **飞书** | 官方维护 |
| **钉钉** | 官方维护 |
| **Slack** | 官方维护 |
| **Discord** | 官方维护 |
| **LINE** | 官方维护 |
| **Satori** | 官方维护 |
| **KOOK** | 官方维护 |
| **Misskey** | 官方维护 |
| **Mattermost** | 官方维护 |
| **WhatsApp（将支持）** | 官方维护 |
| [**Matrix**](https://github.com/stevessr/astrbot_plugin_matrix_adapter) | 社区维护 |
| [**Rocket.Chat**](https://github.com/NET-Homeless/astrbot_plugin_rocket_chat_adapter) | 社区维护 |
| [**VoceChat**](https://github.com/HikariFroya/astrbot_plugin_vocechat) | 社区维护 |

## 支持的模型提供商

| 提供商 | 类型 |
|---------|---------------|
| OpenAI 及兼容服务 | LLM |
| Anthropic | LLM |
| Google Gemini | LLM |
| Moonshot AI | LLM |
| 智谱 AI | LLM |
| DeepSeek | LLM |
| Ollama (本地部署) | LLM |
| LM Studio (本地部署) | LLM |
| MiraRouter | LLM (API 网关, 支持所有模型) |
| 胜算云 | LLM (API 网关, 支持所有模型) |
| AIHubMix | LLM (API 网关, 支持所有模型) |
| 302.AI | LLM (API 网关, 支持所有模型)|
| 小马算力 | LLM (API 网关, 支持所有模型)|
| 硅基流动 | LLM (API 网关, 支持所有模型)  |
| PPIO 派欧云 | LLM (API 网关, 支持所有模型) |
| ModelScope | LLM |
| OneAPI | LLM |
| Dify | LLMOps 平台 |
| 阿里云百炼应用 | LLMOps 平台 |
| Coze | LLMOps 平台 |
| OpenAI Whisper | 语音转文本 |
| SenseVoice | 语音转文本 |
| Xiaomi MiMo Omni | 语音转文本 |
| OpenAI TTS | 文本转语音 |
| Gemini TTS | 文本转语音 |
| GPT-Sovits-Inference | 文本转语音 |
| GPT-Sovits | 文本转语音 |
| FishAudio | 文本转语音 |
| Edge TTS | 文本转语音 |
| 阿里云百炼 TTS | 文本转语音 |
| Azure TTS | 文本转语音 |
| Minimax TTS | 文本转语音 |
| Xiaomi MiMo TTS | 文本转语音 |
| 火山引擎 TTS | 文本转语音 |

## ❤️ 贡献

欢迎任何 Issues/Pull Requests！只需要将你的更改提交到此项目 ：)

### 如何贡献

你可以通过查看问题或帮助审核 PR（拉取请求）来贡献。任何问题或 PR 都欢迎参与，以促进社区贡献。当然，这些只是建议，你可以以任何方式进行贡献。对于新功能的添加，请先通过 Issue 讨论。

### 开发环境

LKMBot 使用 `ruff` 进行代码格式化和检查。

```bash
git clone https://github.com/Alma1314/LKM-bot.git
pip install pre-commit
pre-commit install
```

## 🌍 社区

### QQ 群组

我们有 15 个以上的交流群，详情请参阅[社区](https://docs.astrbot.app/community.html)。

### Discord 频道

<a href="https://discord.gg/hAVk6tgV36"><img alt="Discord_community" src="https://img.shields.io/badge/Discord-AstrBot-purple?style=for-the-badge&color=76bad9"></a>

## ❤️ Special Thanks

特别感谢所有 Contributors 和插件开发者对 LKMBot 的贡献 ❤️

<a href="https://github.com/AstrBotDevs/AstrBot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=AstrBotDevs/AstrBot&max=500&columns=17" />
</a>

开源伙伴 ❤️

- [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) - 出色的猫猫框架
- [Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot) - QQ 中强大的“数字生命”！

## ⭐ Star History

> [!TIP]
> 如果本项目对您的生活 / 工作产生了帮助，或者您关注本项目的未来发展，请给项目 Star，这是我们维护这个开源项目的动力 <3

<div align="center">

[![Star History Chart](https://api.star-history.com/svg?repos=astrbotdevs/astrbot&type=Date)](https://star-history.com/#astrbotdevs/astrbot&Date)

</div>

<div align="center">

_陪伴与能力从来不应该是对立面。我们希望创造的是一个既能理解情绪、给予陪伴，也能可靠完成工作的机器人。_

_私は、高性能ですから!_

<img src="https://files.astrbot.app/watashiwa-koseino-desukara.gif" width="100"/>

</div>
