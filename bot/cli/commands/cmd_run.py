import asyncio
import os
import sys
import traceback
from pathlib import Path

import click
from filelock import FileLock, Timeout

from ..utils import check_bot_root, check_dashboard, get_bot_root

DASHBOARD_RESET_PASSWORD_ENV = "BOT_RESET_DASHBOARD_PASSWORD"


async def run_bot(bot_root: Path) -> None:
    """Run LKMBot"""
    from bot.core import LogBroker, LogManager, db_helper, logger
    from bot.core.initial_loader import InitialLoader

    await check_dashboard(bot_root / "data")

    log_broker = LogBroker()
    LogManager.set_queue_handler(logger, log_broker)
    db = db_helper

    core_lifecycle = InitialLoader(db, log_broker)

    await core_lifecycle.start()


@click.option("--reload", "-r", is_flag=True, help="Auto-reload plugins")
@click.option("--port", "-p", help="LKMBot Dashboard port", required=False, type=str)
@click.option(
    "--reset-password",
    is_flag=True,
    help="Reset dashboard initial password on startup",
)
@click.command()
def run(reload: bool, port: str | None, reset_password: bool) -> None:
    """Run LKMBot"""
    try:
        os.environ["BOT_CLI"] = "1"
        bot_root = get_bot_root()

        if not check_bot_root(bot_root):
            raise click.ClickException(
                f"{bot_root} is not a valid LKMBot root directory. Use 'lkmbot init' to initialize",
            )

        os.environ["BOT_ROOT"] = str(bot_root)
        sys.path.insert(0, str(bot_root))

        if port:
            os.environ["DASHBOARD_PORT"] = port

        if reload:
            click.echo("Plugin auto-reload enabled")
            os.environ["BOT_RELOAD"] = "1"

        if reset_password:
            os.environ[DASHBOARD_RESET_PASSWORD_ENV] = "1"

        lock_file = bot_root / "bot.lock"
        lock = FileLock(lock_file, timeout=5)
        with lock.acquire():
            asyncio.run(run_bot(bot_root))
    except KeyboardInterrupt:
        click.echo("LKMBot has been shut down.")
    except Timeout:
        raise click.ClickException(
            "Cannot acquire lock file. Please check if another instance is running"
        )
    except Exception as e:
        raise click.ClickException(f"Runtime error: {e}\n{traceback.format_exc()}")
