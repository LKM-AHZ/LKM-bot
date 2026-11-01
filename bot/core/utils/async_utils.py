"""通用协程工具：接受「值 / 可等待对象 / 返回前两者的零参可调用」并统一 await 出结果。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")


async def resolve_maybe_awaitable(value: T | Awaitable[T]) -> T:
    """把可能是 awaitable 的值解析成最终结果（嵌套 awaitable 也会被逐层展开）。"""
    while inspect.isawaitable(value):
        value = await cast(Awaitable[T], value)
    return cast(T, value)


async def run_maybe_async(
    operation: Callable[[], T | Awaitable[T]] | T | Awaitable[T],
) -> T:
    """执行 ``operation`` 并 await 出结果：可调用则先调用，同步返回值也可直接传入。"""
    result: Any = (
        cast(Callable[[], T | Awaitable[T]], operation)()
        if callable(operation)
        else operation
    )
    return await resolve_maybe_awaitable(result)
