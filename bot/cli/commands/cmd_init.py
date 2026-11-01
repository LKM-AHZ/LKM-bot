import asyncio
import os
from pathlib import Path

import click
from filelock import FileLock, Timeout

DASHBOARD_INITIAL_PASSWORD_ENV = "BOT_DASHBOARD_INITIAL_PASSWORD"


def _initialize_config_from_env(bot_root: Path) -> None:
    if DASHBOARD_INITIAL_PASSWORD_ENV not in os.environ:
        return

    from bot.core.config.bot_config import BotConfig

    BotConfig(config_path=str(bot_root / "data" / "cmd_config.json"))
    click.echo("Initialized data/cmd_config.json with dashboard initial password.")


async def initialize_bot(bot_root: Path, yes: bool = False) -> None:
    """Execute LKMBot initialization logic.

    Args:
        bot_root: LKMBot root directory path.
        yes: Whether to skip the installation confirmation.
    """
    dot_bot = bot_root / ".bot"

    if not dot_bot.exists():
        if yes or click.confirm(
            f"Install LKMBot to this directory? {bot_root}",
            default=True,
            abort=True,
        ):
            dot_bot.touch()
            click.echo(f"Created {dot_bot}")

    paths = {
        "data": bot_root / "data",
        "config": bot_root / "data" / "config",
        "plugins": bot_root / "data" / "plugins",
        "temp": bot_root / "data" / "temp",
    }

    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        click.echo(f"{'Created' if not path.exists() else 'Directory exists'}: {path}")

    _initialize_config_from_env(bot_root)


@click.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def init(yes: bool) -> None:
    """Initialize LKMBot.

    Args:
        yes: Whether to skip confirmation prompts.
    """
    from ..utils import get_bot_root

    click.echo("Initializing LKMBot...")

    bot_root = get_bot_root()
    lock_file = bot_root / "bot.lock"
    lock = FileLock(lock_file, timeout=5)

    try:
        with lock.acquire():
            asyncio.run(initialize_bot(bot_root, yes=yes))
            click.echo("Done! You can now run 'lkmbot run' to start LKMBot")
    except Timeout:
        raise click.ClickException(
            "Cannot acquire lock file. Please check if another instance is running"
        )

    except Exception as e:
        raise click.ClickException(f"Initialization failed: {e!s}")
