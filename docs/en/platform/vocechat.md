# Connect to VoceChat

> [!TIP]
> LKMBot does not include this adapter by default. Install [astrbot_plugin_vocechat](https://github.com/HikariFroya/astrbot_plugin_vocechat), developed by [HikariFroya](https://github.com/HikariFroya).

> [!WARNING]
> This adapter is community-maintained and not officially maintained by LKMBot.

## Deploy VoceChat

VoceChat is an open-source instant messaging platform with simple multi-platform deployment.

See deployment methods on the [VoceChat official website](https://voce.chat/en-US).

## Install `astrbot_plugin_vocechat`

In LKMBot WebUI, open `Extensions → Plugins → LKMBot Plugin Market`, search for `astrbot_plugin_vocechat` and install it.

After installation, go to `Platforms` -> `Add Adapter` -> `VoceChat`.
If VoceChat is missing, restart LKMBot or verify plugin installation.

Enable the adapter in the configuration dialog.

## Configuration

- `vocechat_server_url` (required): full VoceChat server URL, e.g. `http://localhost:3009` or `https://your.vocechat.domain` (no trailing `/`).
- `api_key` (required): API key generated for the bot account in VoceChat.
- `webhook_path` (recommended default/custom): webhook path used by LKMBot to receive VoceChat messages, e.g. `/vocechat_webhook`.
- `webhook_listen_host` (usually `0.0.0.0`): listen host for LKMBot webhook server.
- `webhook_port` (required): listen port for LKMBot webhook server, e.g. `8080`.
- `get_user_nickname_from_api` (boolean, default `true`): fetch nickname via VoceChat API.
- `send_plain_as_markdown` (boolean, default `false`): send plain text in markdown format.
- `default_bot_self_uid` (required): UID of your VoceChat bot account.

After configuration, click Save and test in VoceChat.

## Issue Reporting

If needed, report issues to:

- Plugin repo: <https://github.com/HikariFroya/astrbot_plugin_vocechat/issues>
- LKMBot repo: <https://github.com/AstrBotDevs/AstrBot/issues/new?template=bug-report.yml>
