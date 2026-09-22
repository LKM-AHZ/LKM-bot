"""
Custom Hatchling build hook.

Only runs when the environment variable ASTRBOT_BUILD_DASHBOARD=1 is set,
so that `uv sync` / editable installs are never affected.

Usage:
    ASTRBOT_BUILD_DASHBOARD=1 uv build

When enabled, this hook:
1. Runs `npm run build` inside the `dashboard/` directory.
2. Copies the resulting `dashboard/dist/` tree into
   `astrbot/dashboard/dist/` so the static assets are shipped
   inside the Python wheel.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import (  # ty: ignore[unresolved-import]  # 仅构建时安装 hatchling, 运行期不导入
    BuildHookInterface,
)


def _resolve_node_tool(name: str) -> str:
    """Resolve a Node CLI to its full path.

    On Windows these tools are `.cmd`/`.ps1` shims, and CreateProcess only appends `.exe`,
    so passing the bare name to subprocess (shell=False) raises FileNotFoundError even with
    Node installed. Resolve through PATH/PATHEXT and fail with a readable message.

    Args:
        name: CLI name, e.g. "pnpm".

    Returns:
        Absolute path to the executable.

    Raises:
        RuntimeError: The tool is not on PATH.
    """
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(
            f"[hatch_build] '{name}' not found on PATH. Install Node.js (which provides "
            f"'{name}') before building the dashboard, or leave ASTRBOT_BUILD_DASHBOARD unset "
            "to skip the dashboard build."
        )
    return executable


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Only run when explicitly requested (e.g. during CI / release builds).
        # This prevents `uv sync` / editable installs from triggering npm.
        if os.environ.get("ASTRBOT_BUILD_DASHBOARD", "").strip() != "1":
            return

        root = Path(self.root)
        dashboard_src = root / "dashboard"
        dist_src = dashboard_src / "dist"
        dist_target = root / "astrbot" / "dashboard" / "dist"

        if not dashboard_src.exists():
            print(
                "[hatch_build] 'dashboard/' directory not found – skipping dashboard build.",
                file=sys.stderr,
            )
            return

        # ── Install Node dependencies when missing or stale ──────────────────
        # dashboard 是 pnpm 工程（pnpm-lock.yaml + package.json 的 pnpm.overrides，
        # Dockerfile/CI 也都用 pnpm@10）：用 npm install 会无视 lockfile 与 overrides，
        # 装出来的版本与发布产物不可复现。node_modules 已存在但比 lockfile 旧时同样要重装，
        # 否则上一次解析出来的旧树会被静默复用。
        node_modules = dashboard_src / "node_modules"
        lockfile = dashboard_src / "pnpm-lock.yaml"
        needs_install = not node_modules.exists()
        if not needs_install and lockfile.is_file():
            try:
                needs_install = lockfile.stat().st_mtime > node_modules.stat().st_mtime
            except OSError:
                needs_install = True
        pnpm = _resolve_node_tool("pnpm")
        if needs_install:
            print("[hatch_build] Installing dashboard Node dependencies (pnpm)...")
            subprocess.run(
                [pnpm, "install", "--frozen-lockfile"],
                cwd=dashboard_src,
                check=True,
            )

        # ── Build the Vue/Vite dashboard ──────────────────────────────────────
        print("[hatch_build] Building Vue dashboard (pnpm run build)...")
        subprocess.run(
            [pnpm, "run", "build"],
            cwd=dashboard_src,
            check=True,
        )

        if not dist_src.exists():
            print(
                "[hatch_build] dashboard/dist not found after build – skipping copy.",
                file=sys.stderr,
            )
            return

        # ── Copy into the Python package tree ────────────────────────────────
        # 先复制到同目录的 staging，再一次性替换：原先先 rmtree 目标再 copytree，
        # 中途失败/被打断会把 astrbot/dashboard/dist 留成空目录或半份内容，
        # 紧接着的 uv build 就把这份残骸打进包里。另外目标若是指向 dashboard/dist 的
        # 软链接（常见的本地开发手法），rmtree 会直接抛 OSError。
        staging = dist_target.parent / (dist_target.name + ".staging")
        if staging.is_symlink() or staging.is_file():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(dist_src, staging)
        if dist_target.is_symlink() or dist_target.is_file():
            dist_target.unlink()
        elif dist_target.exists():
            shutil.rmtree(dist_target)
        os.replace(staging, dist_target)
        print(f"[hatch_build] Dashboard dist copied → {dist_target.relative_to(root)}")
