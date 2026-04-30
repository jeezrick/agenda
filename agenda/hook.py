from __future__ import annotations

"""Hook System — 可扩展事件钩子。

## 设计理念

钩子系统是所有扩展功能（指标、通知、审批）的基础设施。
设计原则是极简：不设优先级、不设传播控制、不设超时。

## 六个核心钩子点

    on_node_start     — 节点准备完成，即将运行
    on_node_complete  — 节点成功结束
    on_node_error     — 节点异常（含异常对象）
    on_turn_start     — 每轮 LLM 调用前（含 iteration、token_count）
    on_tool_call      — 每个工具执行前（含 tool_name、args）
    on_compaction     — 压缩完成/失败（含 pre/post tokens、success）

## 同步和异步

同一个钩子点可以注册同步和异步回调。
emit 自动检测：iscoroutine(result) → await；否则直接继续。

## 异常隔离

钩子回调的异常被 emit 捕获并丢弃。
一个钩子的失败不影响其他钩子，更不影响主 Agent 循环。
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
