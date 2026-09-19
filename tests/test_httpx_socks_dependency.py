"""httpx[socks] 依赖验收：SOCKS 代理支持的「声明 ↔ 锁定」一致性。

依赖真相源是 ``pyproject.toml``（声明）与 ``uv.lock``（锁定）。``requirements.txt`` 已随
构建链转向 uv 而移除，故原先「与 requirements.txt 比对 spec」的检查改为与 ``uv.lock`` 比对：
光在 pyproject 里声明 extra 不够，锁定结果里必须真的带上该 extra，SOCKS 代理才可用。
"""

import re
from pathlib import Path

import pytest
import tomllib

from astrbot.core.utils.toml_parser import read_pyproject_project_dependencies

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
UV_LOCK_PATH = PROJECT_ROOT / "uv.lock"
HTTPX_SOCKS_PATTERN = re.compile(r"^httpx\[socks\](?:\s*[<>=!~][^;]*)?(?:\s*;.*)?$")


def _read_httpx_socks_dependency(entries: list[str]) -> str | None:
    for entry in entries:
        candidate = entry.strip()
        if HTTPX_SOCKS_PATTERN.match(candidate):
            return candidate
    return None


def _read_pyproject_dependencies() -> list[str]:
    return read_pyproject_project_dependencies(PYPROJECT_PATH)


def _locked_httpx_extras() -> dict:
    """``uv.lock`` 里 httpx 的 optional-dependencies；缺包或缺 section 时返回空 dict。"""
    lock = tomllib.loads(UV_LOCK_PATH.read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        if package.get("name") == "httpx":
            return package.get("optional-dependencies", {})
    return {}


def test_pyproject_declares_httpx_socks_dependency() -> None:
    pyproject_dependency = _read_httpx_socks_dependency(_read_pyproject_dependencies())

    assert pyproject_dependency is not None, (
        "Expected httpx[socks] dependency in pyproject.toml for SOCKS proxy support"
    )


def test_uv_lock_pins_httpx_socks_extra() -> None:
    """pyproject 声明了 httpx[socks]，锁文件里就必须真的带上该 extra（含 socksio）。"""
    assert _read_httpx_socks_dependency(_read_pyproject_dependencies()) is not None, (
        "Expected httpx[socks] dependency in pyproject.toml for SOCKS proxy support"
    )
    socks_extra = _locked_httpx_extras().get("socks")
    assert socks_extra, (
        "Expected uv.lock to record the httpx 'socks' extra for SOCKS proxy support"
    )
    assert any(dep.get("name") == "socksio" for dep in socks_extra), (
        "Expected uv.lock's httpx 'socks' extra to pull in socksio"
    )


@pytest.mark.parametrize(
    "entry",
    [
        "httpx[socks]",
        "httpx[socks]==0.27.0",
        "httpx[socks]==0.28.1",
        "httpx[socks]>=0.27.0,<0.28.0",
        "httpx[socks]>=0.27,<0.29",
        'httpx[socks]; python_version >= "3.11"',
        'httpx[socks]>=0.27.0 ; python_version < "3.13"',
        'httpx[socks] ; python_version < "3.13"',
        'httpx[socks]  >=0.27  ; python_version < "3.13"',
    ],
)
def test_httpx_socks_pattern_matches_valid_variants(entry: str) -> None:
    match = HTTPX_SOCKS_PATTERN.match(entry)

    assert match is not None, (
        f"Expected httpx[socks] dependency pattern to match valid entry for "
        f"SOCKS proxy support: {entry}"
    )
    assert match.group(0) == entry, (
        f"Expected httpx[socks] dependency pattern to fully match valid entry "
        f"for SOCKS proxy support: {entry}"
    )


@pytest.mark.parametrize(
    "entry",
    [
        "httpx",
        "httpx==0.27.0",
        "httpx[http2]",
        "httpx[socks-extra]",
        "httpx [socks]",
        "someprefix httpx[socks]",
        "httpx[socks] trailing-text",
        "httpx[socks] extra ; markers",
        "httpx[socks]andmore",
    ],
)
def test_httpx_socks_pattern_rejects_invalid_variants(entry: str) -> None:
    assert HTTPX_SOCKS_PATTERN.match(entry) is None, (
        f"Expected httpx[socks] dependency pattern to reject invalid entry for "
        f"SOCKS proxy support: {entry}"
    )
