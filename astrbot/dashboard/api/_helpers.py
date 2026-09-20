"""dashboard API 路由共享的内部小工具（不对外暴露，仅供 ``astrbot.dashboard.api`` 内使用）。"""

from __future__ import annotations

from typing import Any

from fastapi import Request


async def json_or_empty(request: Request) -> dict[str, Any]:
    """解析请求 JSON 体；体不是 JSON 对象（含解析失败）时返回空 dict。

    面板的多数 POST 端点把请求体当可选参数用，缺失或畸形时按「字段全空」处理，由服务层
    给出字段级报错，而不是让 FastAPI 在解析阶段直接 422。
    """
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
