from pathlib import Path

import click


def check_bot_root(path: str | Path) -> bool:
    """Check if the path is an LKMBot root directory"""
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists() or not path.is_dir():
        return False
    if not (path / ".bot").exists():
        return False
    return True


def get_bot_root() -> Path:
    """Get the LKMBot root directory path"""
    return Path.cwd()


async def check_dashboard(bot_root: Path) -> None:
    """Report the optional static panel directory, if one is configured.

    Args:
        bot_root: LKMBot root directory whose ``data/dist`` may hold a
            prebuilt standalone panel.
    """
    from bot.core.dashboard_assets import resolve_dashboard_dist

    dashboard_path = resolve_dashboard_dist(bot_root / "dist")
    if dashboard_path is None:
        click.echo(
            "No static panel files configured; the panel is served by the website."
        )
        return
    click.echo(f"Dashboard is ready at {dashboard_path}")
