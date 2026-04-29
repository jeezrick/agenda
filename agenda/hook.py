from __future__ import annotations

"""Hook system — 可扩展事件钩子。

设计：
- 6 个核心钩子点（on_node_start/complete/error、on_turn_start、on_tool_call、on_compaction）
- 支持同步和异步回调
- 钩子失败不影响主循环
- 无优先级、无传播控制——保持极简
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

HookFunc = Callable[..., Any]


class HookRegistry:
    """极简钩子注册表。"""

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFunc]] = defaultdict(list)

    def register(self, event: str, fn: HookFunc) -> None:
        """注册钩子函数。fn 可以是同步或异步。"""
        self._hooks[event].append(fn)

    def remove(self, event: str, fn: HookFunc | None = None) -> None:
        """移除钩子。fn 为 None 时移除该事件所有钩子。"""
        if fn is None:
            self._hooks.pop(event, None)
        else:
            self._hooks[event] = [h for h in self._hooks.get(event, []) if h is not fn]

    async def emit(self, event: str, **kwargs: Any) -> None:
        """触发事件，执行所有注册的钩子。钩子异常被捕获不传播。"""
        for fn in self._hooks.get(event, []):
            try:
                result = fn(**kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # 钩子失败不影响主循环

    def has(self, event: str) -> bool:
        """检查是否有注册了该事件的钩子。"""
        return event in self._hooks and bool(self._hooks[event])
