import argparse
import asyncio
import mimetypes
import os
import sys
from pathlib import Path

DASHBOARD_RESET_PASSWORD_ENV = "BOT_RESET_DASHBOARD_PASSWORD"


def _apply_startup_env_flags(argv: list[str]) -> None:
    """Apply startup flags that must take effect before core imports.

    Args:
        argv: Command-line arguments excluding the executable name.
    """

    if "-h" in argv or "--help" in argv:
        return

    startup_parser = argparse.ArgumentParser(add_help=False)
    startup_parser.add_argument("--reset-password", action="store_true")
    startup_args, _ = startup_parser.parse_known_args(argv)
    if startup_args.reset_password:
        os.environ[DASHBOARD_RESET_PASSWORD_ENV] = "1"


# Importing runtime_bootstrap pulls in bot.core, whose BotConfig
# constructor consumes BOT_RESET_DASHBOARD_PASSWORD. Flags must be applied
# first so bot.core never observes a stale environment.
_apply_startup_env_flags(sys.argv[1:])

import runtime_bootstrap  # noqa: E402

runtime_bootstrap.initialize_runtime_bootstrap()

from bot.core import LogBroker, LogManager, db_helper, logger  # noqa: E402
from bot.core.dashboard_assets import resolve_dashboard_dist  # noqa: E402
from bot.core.initial_loader import InitialLoader  # noqa: E402
from bot.core.utils.bot_path import (  # noqa: E402
    get_bot_config_path,
    get_bot_knowledge_base_path,
    get_bot_plugin_path,
    get_bot_root,
    get_bot_site_packages_path,
    get_bot_temp_path,
)
from bot.core.utils.runtime_env import is_packaged_desktop_runtime  # noqa: E402

# 将父目录添加到 sys.path
sys.path.append(Path(__file__).parent.as_posix())

logo_tmpl = r"""理科迷爱好者社区官方bot"""


def check_env() -> None:
    if not (sys.version_info.major == 3 and sys.version_info.minor >= 10):
        logger.error("Please run this project with Python 3.10 or later.")
        # 用 sys.exit 而不是 site 模块注入的 exit()：后者在 `python -S`/嵌入式解释器里不存在，
        # 会把「干净退出」变成 NameError；顺带以非零码退出，调用方能感知失败。
        sys.exit(1)

    bot_root = get_bot_root()
    if bot_root not in sys.path:
        sys.path.insert(0, bot_root)

    site_packages_path = get_bot_site_packages_path()
    if not is_packaged_desktop_runtime() and site_packages_path not in sys.path:
        sys.path.append(site_packages_path)

    os.makedirs(get_bot_config_path(), exist_ok=True)
    os.makedirs(get_bot_plugin_path(), exist_ok=True)
    os.makedirs(get_bot_temp_path(), exist_ok=True)
    os.makedirs(get_bot_knowledge_base_path(), exist_ok=True)
    os.makedirs(site_packages_path, exist_ok=True)

    # 针对问题 #181 的临时解决方案
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("application/json", ".json")


async def check_dashboard_files(webui_dir: str | None = None):
    """Resolve the optional static panel directory for startup.

    The panel frontend is served by the website at ``/bot/``; this only resolves
    an explicitly configured directory or a legacy ``data/dist`` copy so a
    standalone deployment can still serve prebuilt files from the bot process.

    Args:
        webui_dir: Optional explicit WebUI directory path from CLI.

    Returns:
        The directory path to serve, or None when no static panel is configured.
    """
    resolved_dist = resolve_dashboard_dist(webui_dir)
    if resolved_dist is None:
        logger.info("No static panel files configured; /bot/ is served by the website.")
        return None
    logger.info("Serving static panel files from %s", resolved_dist)
    return str(resolved_dist)


async def main_async(webui_dir_arg: str | None, log_broker: LogBroker) -> None:
    """主异步入口

    Args:
        webui_dir_arg: 命令行传入的 WebUI 目录，可为 None。
        log_broker: 日志广播器，由入口构造后显式传入（原先读模块级变量，
            非 `__main__` 入口调用本函数会 NameError）。
    """
    # 检查仪表板文件
    webui_dir = await check_dashboard_files(webui_dir_arg)
    if webui_dir is None:
        logger.info(
            "No static panel files are served by the bot process. The panel is "
            "served by the website at /bot/; use --webui-dir only for a standalone "
            "prebuilt panel."
        )

    db = db_helper

    # 打印 logo
    logger.info(logo_tmpl)

    core_lifecycle = InitialLoader(db, log_broker)
    core_lifecycle.webui_dir = webui_dir
    await core_lifecycle.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LKMBot")
    parser.add_argument(
        "--webui-dir",
        type=str,
        help="Specify the directory path for WebUI static files",
        default=None,
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help=(
            "Reset the dashboard initial password on startup and print it in "
            "startup logs"
        ),
    )
    args = parser.parse_args()

    check_env()

    # 启动日志代理
    log_broker = LogBroker()
    LogManager.set_queue_handler(logger, log_broker)

    # 只使用一次 asyncio.run()
    asyncio.run(main_async(args.webui_dir, log_broker))
