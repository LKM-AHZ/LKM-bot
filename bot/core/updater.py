import asyncio
import os
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bot.core import logger
from bot.core.config.default import VERSION
from bot.core.repository import GitHubRepository
from bot.core.utils.bot_path import (
    get_bot_path,
    get_bot_temp_path,
)
from bot.core.utils.io import ensure_dir

from .zip_updater import ReleaseInfo, _RepoZipUpdater

__all__ = ["BotUpdater", "UpdateProgress", "UpdateProgressCallback"]


@dataclass(frozen=True, slots=True)
class UpdateProgress:
    """Observable progress for an LKMBot update.

    Args:
        stage: Current update stage.
        status: Current stage status.
        message: Human-readable progress message.
        overall_percent: Overall update progress from 0 to 100.
        downloaded_bytes: Downloaded bytes during a download stage.
        total_bytes: Expected total bytes when supplied by the server.
        speed_kib_per_second: Current download speed in KiB/s.
    """

    stage: Literal["core", "verify", "apply"]
    status: Literal["running", "done"]
    message: str
    overall_percent: int
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_kib_per_second: float | None = None


UpdateProgressCallback = Callable[[UpdateProgress], Awaitable[None]]


class BotUpdater(_RepoZipUpdater):
    """Expose the complete, high-level LKMBot Core update operations."""

    def __init__(
        self,
        verify: str | bool | None = None,
    ) -> None:
        """Initialize the LKMBot core updater.

        Args:
            verify: TLS certificate verification configuration for HTTPX.
        """
        super().__init__(verify=verify)
        self._main_path = get_bot_path()
        # GitHub's REST API is the release source: the payload shape (tag_name / published_at /
        # body / zipball_url) is exactly what the old third-party proxy returned, so the rest of
        # this module is unchanged.
        self._release_api = (
            "https://api.github.com/repos/Alma1314/LKM-bot/releases?per_page=100"
        )
        self._repository_url = "https://github.com/Alma1314/LKM-bot"

    async def check_update(
        self,
        consider_prerelease: bool = True,
    ) -> ReleaseInfo | None:
        """Check whether a newer LKMBot release is available.

        Args:
            consider_prerelease: Whether prerelease versions may be selected.

        Returns:
            The newer release, or None when the current version is up to date.
        """
        return await self._check_update(
            self._release_api,
            VERSION,
            consider_prerelease,
        )

    async def get_releases(self) -> list[ReleaseInfo]:
        """Fetch available LKMBot releases.

        Returns:
            Releases in upstream order, including prereleases. Each item contains
            the update target version, publication timestamp, and release notes.
        """
        releases = await self._fetch_release_info(self._release_api)
        return [
            ReleaseInfo(
                version=release["tag_name"],
                published_at=release["published_at"],
                body=release["body"],
            )
            for release in releases
        ]

    async def update(
        self,
        version: str | None = None,
        proxy: str = "",
        progress_callback: UpdateProgressCallback | None = None,
    ) -> None:
        """Download, validate, and apply the Core package.

        Args:
            version: Release tag or commit hash. None selects the latest release.
            proxy: Optional URL-prefix mirror.
            progress_callback: Optional asynchronous progress observer. Observer
                failures are logged and do not interrupt the update.

        Returns:
            None.

        Raises:
            Exception: If the package cannot be prepared or applied.
        """

        async def emit_progress(
            stage: Literal["core", "verify", "apply"],
            status: Literal["running", "done"],
            message: str,
            percent: int,
            download: dict | None = None,
        ) -> None:
            if not progress_callback:
                return
            event = UpdateProgress(
                stage=stage,
                status=status,
                message=message,
                overall_percent=percent,
                downloaded_bytes=(
                    int(download.get("downloaded") or 0) if download else None
                ),
                total_bytes=int(download.get("total") or 0) if download else None,
                speed_kib_per_second=(
                    float(download.get("speed") or 0) if download else None
                ),
            )
            try:
                await progress_callback(event)
            except Exception:
                logger.exception("LKMBot update progress observer failed.")

        async def core_progress(payload: dict) -> None:
            await emit_progress(
                "core",
                "running",
                "正在下载 LKMBot 项目代码...",
                int(float(payload.get("percent") or 0) * 90),
                payload,
            )

        target_version = version
        target_release = None
        if not target_version or target_version == "latest":
            releases = await self._fetch_release_info(self._release_api)
            if not releases:
                raise RuntimeError("No LKMBot release is available.")
            target_release = releases[0]
            target_version = target_release["tag_name"]
            if self._compare_version(VERSION, target_version) >= 0:
                raise RuntimeError("LKMBot is already up to date.")
        elif target_version.startswith("v"):
            releases = await self._fetch_release_info(self._release_api)
            target_release = next(
                (
                    release
                    for release in releases
                    if release["tag_name"] == target_version
                ),
                None,
            )
            if target_release is None:
                raise RuntimeError(
                    f"No update package was found for version {target_version}."
                )

        update_temp_parent = Path(get_bot_temp_path()) / "updates"
        if update_temp_parent.is_symlink():
            update_temp_parent.unlink()
        update_temp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        update_temp_parent.chmod(0o700)

        with tempfile.TemporaryDirectory(
            prefix="project-update-",
            dir=update_temp_parent,
        ) as update_temp_dir_name:
            update_temp_dir = Path(update_temp_dir_name)
            core_zip_path = update_temp_dir / "core.zip"

            await emit_progress(
                "core",
                "running",
                "正在下载 LKMBot 项目代码...",
                0,
            )
            await self._download_core_package(
                latest=False,
                version=target_version,
                proxy=proxy,
                path=core_zip_path,
                progress_callback=core_progress,
                release_data=target_release,
            )
            await emit_progress(
                "core",
                "done",
                "项目代码下载完成。",
                90,
            )

            await emit_progress(
                "verify",
                "running",
                "下载完成，正在校验更新包...",
                90,
            )

            def verify_packages() -> None:
                with zipfile.ZipFile(core_zip_path, "r") as archive:
                    corrupt_member = archive.testzip()
                if corrupt_member:
                    raise ValueError(f"更新包校验失败: {corrupt_member}")

            await asyncio.to_thread(verify_packages)
            await emit_progress(
                "verify",
                "done",
                "更新包校验完成。",
                91,
            )

            await emit_progress(
                "apply",
                "running",
                "下载完成，正在应用更新...",
                91,
            )
            await asyncio.to_thread(self._apply_core_package, core_zip_path)
            await emit_progress(
                "apply",
                "done",
                "更新文件应用完成。",
                92,
            )

    async def _download_core_package(
        self,
        latest=True,
        version=None,
        proxy="",
        path: str | Path = "temp.zip",
        progress_callback=None,
        release_data: dict | None = None,
    ) -> Path:
        """Download an LKMBot core update package without applying it.

        Args:
            latest: Whether to download the latest release.
            version: Specific release tag or commit hash to download.
            proxy: Optional URL-prefix mirror for the archive request.
            path: Destination zip path.
            progress_callback: Optional callback for download progress payloads.
            release_data: Previously resolved metadata for the target release.

        Returns:
            Path to the downloaded update package.

        Raises:
            Exception: If update metadata cannot resolve a package URL.
        """

        file_url = None

        if os.environ.get("BOT_CLI") or os.environ.get("BOT_LAUNCHER"):
            raise Exception(
                "Error: You are running LKMBot via CLI, please use `pip` or `uv tool upgrade` to update LKMBot."
            )  # 避免版本管理混乱

        if latest:
            update_data = await self._fetch_release_info(self._release_api)
            if not update_data:
                raise RuntimeError("No LKMBot release is available.")
            latest_version = update_data[0]["tag_name"]
            if self._compare_version(VERSION, latest_version) >= 0:
                raise Exception("LKMBot is already up to date.")
            file_url = update_data[0]["zipball_url"]
        elif str(version).startswith("v"):
            if release_data is None:
                update_data = await self._fetch_release_info(self._release_api)
                release_data = next(
                    (data for data in update_data if data["tag_name"] == version),
                    None,
                )
            if release_data is None or release_data["tag_name"] != version:
                raise Exception(f"No update package was found for version {version}.")
            file_url = release_data["zipball_url"]
        else:
            if len(str(version)) != 40:
                raise Exception("The commit hash must be 40 characters long.")
            repository = GitHubRepository.parse(self._repository_url)
            file_url = repository.revision_archive_url(str(version))
        logger.info(f"Preparing to update LKMBot Core to version {version}")

        if proxy:
            proxy = proxy.removesuffix("/")
            file_url = f"{proxy}/{file_url}"

        zip_path = Path(path)
        ensure_dir(zip_path.parent)
        await self._download_file(
            file_url,
            str(zip_path),
            progress_callback=progress_callback,
        )
        return zip_path

    def _apply_core_package(self, zip_path: str | Path) -> None:
        """Apply a previously downloaded LKMBot core update package.

        Args:
            zip_path: Core update zip archive path.

        Returns:
            None.

        Raises:
            Exception: If the archive cannot be extracted or applied.
        """

        logger.info("LKMBot Core update package downloaded; extracting the archive.")
        self._extract_archive(str(zip_path), self._main_path)
