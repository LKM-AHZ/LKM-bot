"""Dashboard 子路径前缀（面板并入社区站同域 ``/bot/`` 时的 base path）。

背景：LKMBot 面板可被反向代理挂在**子路径**下（本部署为社区站同域的 ``/bot/``，网关
``proxy-rewrite`` 剥掉前缀后再转发到本进程）。此时后端**仍以根路径服务**（静态路由、API 前缀
都不变），但有两处必须知道前缀，否则浏览器侧的 cookie 与重定向会打到错的地方：

- **cookie Path**：面板 cookie 若仍写 ``/``，在社区站同域下会被发往社区站全部路径；收窄到
  ``/bot`` 只让面板自己的请求携带它。
- **重定向 Location**：302 必须回到浏览器可见的前缀下（``/bot/#/...``），而不是后端视角的根。

前端侧的前缀由**构建期** base 决定（``VITE_BASE_PATH``，见 ``dashboard/vite.config.ts``）；
本模块只管后端这两处运行时依赖。

配置：``ASTRBOT_DASHBOARD_BASE_PATH``（兼容 ``DASHBOARD_BASE_PATH``），留空 = 挂在根路径
（默认，保持上游单机部署行为不变）。值会被规范化为 ``""`` 或 ``"/bot"`` 形态（无尾斜杠）。
"""

from __future__ import annotations

import os

BASE_PATH_ENV = "ASTRBOT_DASHBOARD_BASE_PATH"
BASE_PATH_ENV_LEGACY = "DASHBOARD_BASE_PATH"


def dashboard_base_path() -> str:
    """规范化后的面板前缀：``""``（根路径）或 ``"/bot"``（无尾斜杠）。

    每次调用都读环境变量：该值在进程生命周期内不变，但测试需要能按用例覆盖，且读取成本
    可以忽略（``os.environ`` 是内存字典）。
    """
    raw = (
        os.environ.get(BASE_PATH_ENV) or os.environ.get(BASE_PATH_ENV_LEGACY) or ""
    ).strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


def with_base(path: str) -> str:
    """把后端视角的绝对路径补上面板前缀，得到浏览器可见路径。

    ``with_base("/")`` → ``"/bot/"``；前缀为空时原样返回。
    """
    normalized = path if path.startswith("/") else f"/{path}"
    base = dashboard_base_path()
    return f"{base}{normalized}" if base else normalized


def dashboard_cookie_path(suffix: str = "/") -> str:
    """面板 cookie 的 ``Path``：前缀作用域下的某一路径。

    前缀为空时退化为上游的根路径语义（``/`` 与 ``/api/auth``），保证单机部署行为不变；
    挂在 ``/bot`` 下时收窄为 ``/bot/`` 与 ``/bot/api/auth``——同域下不让面板 cookie 被送往
    社区站其它路径。
    """
    return with_base(suffix)
