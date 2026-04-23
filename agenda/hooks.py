from __future__ import annotations

"""Hook 注册表。"""

import asyncio
from typing import Any, Callable, Coroutine


HookFunc = Callable[["AgentLoop"], Coroutine[Any, Any, None]]


class HookRegistry:
    """在 Agent 循环的关键节点插入策略。"""

    def __init__(self) -> None:
        self._before_tool: list[HookFunc] = []
        self._after_tool: list[HookFunc] = []
        self._before_loop: list[HookFunc] = []
        self._after_loop: list[HookFunc] = []
        self._on_complete: list[HookFunc] = []
        self._on_error: list[HookFunc] = []

    def before_tool(self, func: HookFunc) -> HookFunc:
        self._before_tool.append(func)
        return func

    def after_tool(self, func: HookFunc) -> HookFunc:
        self._after_tool.append(func)
        return func

    def before_loop(self, func: HookFunc) -> HookFunc:
        self._before_loop.append(func)
        return func

    def after_loop(self, func: HookFunc) -> HookFunc:
        self._after_loop.append(func)
        return func

    def on_complete(self, func: HookFunc) -> HookFunc:
        self._on_complete.append(func)
        return func

    def on_error(self, func: HookFunc) -> HookFunc:
        self._on_error.append(func)
        return func

    async def fire(self, name: str, loop: AgentLoop) -> None:
        handlers = getattr(self, f"_{name}", [])
        for handler in handlers:
            try:
                await handler(loop)
            except Exception as e:
                print(f"[Hook 错误] {name}: {e}")

