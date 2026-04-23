from __future__ import annotations

"""子 Agent 系统（简化版 Butterfly sub_agent）。

改进（v0.0.5）：
- 用 IPC（events.jsonl）替代 done.json 轮询
- 支持实时 progress 事件
- 支持取消级联（父向子的 events.jsonl 写 interrupt）
- 支持父子消息传递

最大嵌套深度：MAX_SUB_AGENT_DEPTH（默认 2）
"""

import asyncio
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .const import DEFAULT_MAX_ITERATIONS, DEFAULT_NODE_TIMEOUT, MAX_SUB_AGENT_DEPTH
from .session import Session
from .models import ModelRegistry
from .hooks import HookRegistry
from .agent import AgentLoop
from .tools import build_tools


class SubAgentManager:
    """
    子 Agent 管理器。

    通信机制（文件系统 IPC）：
    - 子 Agent 运行中向自己的 events.jsonl 写 progress 事件
    - 子 Agent 完成后向 events.jsonl 写 completed/failed 事件
    - 父 Agent 轮询子 Agent 的 events.jsonl 获取结果
    - 父 Agent 取消时向子 Agent 的 events.jsonl 写 interrupt 事件
    """

    def __init__(self, parent_session: Session, model_registry: ModelRegistry,
                 max_depth: int = MAX_SUB_AGENT_DEPTH) -> None:
        self.parent_session = parent_session
        self.model_registry = model_registry
        self.max_depth = max_depth
        self._child_tasks: dict[str, asyncio.Task] = {}

    def _current_depth(self) -> int:
        """计算当前嵌套深度。"""
        depth = 0
        node_dir = self.parent_session.node_dir
        while node_dir.name == "children" or (node_dir.parent and node_dir.parent.name == "children"):
            depth += 1
            node_dir = node_dir.parent.parent if node_dir.parent else node_dir
        return depth

    async def spawn_child(
        self,
        task: str,
        name: str,
        model: str | None = None,
        system_prompt: str | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout: float = DEFAULT_NODE_TIMEOUT,
    ) -> str:
        """创建并启动子 Agent。返回 child_name。"""
        current_depth = self._current_depth()
        if current_depth >= self.max_depth:
            return f"[错误] 子 Agent 嵌套深度已达上限 {self.max_depth}"

        child_session = self.parent_session.child_session(name)

        # 准备子 Agent 的 system prompt
        child_system = system_prompt or f"""你是一个子 Agent，被父 Agent 委派执行特定任务。

# 规则
- 你是独立的 Agent，有自己的 .context/ 和 output/
- 完成任务后，向自己的 events.jsonl 发送 completed 事件
- 运行中可发送 progress 事件让父 Agent 知道进度
- 收到 interrupt 事件时应优雅停止

# 可用工具
和父 Agent 相同的文件系统工具 + IPC 工具

# 记忆线索
当前嵌套深度: {current_depth + 1}/{self.max_depth}
父 Agent 可通过 events.jsonl 与你通信
"""

        child_tools = build_tools(child_session, allow_shell=False)
        child_hooks = HookRegistry()

        async def _run_child() -> None:
            agent = AgentLoop(
                session=child_session,
                model_registry=self.model_registry,
                tools=child_tools,
                hooks=child_hooks,
                model=model,
                max_iterations=max_iterations,
                timeout=timeout,
            )
            try:
                result = await agent.run(child_system, task)
                child_session.append_event({
                    "type": "completed",
                    "result": result,
                    "finished_at": datetime.now().isoformat(),
                })
                if not child_session.output_exists:
                    child_session.write_output("draft.md", result)
            except asyncio.CancelledError:
                child_session.append_event({
                    "type": "cancelled",
                    "finished_at": datetime.now().isoformat(),
                })
                raise
            except Exception as e:
                child_session.append_event({
                    "type": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "finished_at": datetime.now().isoformat(),
                })
                child_session.write_system("error.log", traceback.format_exc())

        task_obj = asyncio.create_task(_run_child(), name=f"subagent_{name}")
        self._child_tasks[name] = task_obj
        return f"[系统] 子 Agent '{name}' 已启动（深度: {current_depth + 1}/{self.max_depth}）"

    async def wait_for_child(self, name: str, poll_interval: float = 1.0, timeout: float = 300.0) -> str:
        """轮询子 Agent 的 events.jsonl 等待完成。返回结果。"""
        child_session = self.parent_session.child_session(name)
        deadline = time.monotonic() + timeout
        offset = child_session.events_size()

        while time.monotonic() < deadline:
            events, offset = child_session.poll_events(offset)
            for event in events:
                etype = event.get("type")
                if etype == "completed":
                    result = event.get("result", "")
                    return f"[子 Agent '{name}' 完成]\n{result[:2000]}"
                elif etype == "failed":
                    error = event.get("error", "未知错误")
                    return f"[子 Agent '{name}' 失败] {error}"
                elif etype == "cancelled":
                    return f"[子 Agent '{name}' 已取消]"
                elif etype == "progress":
                    print(f"  [子 Agent '{name}' 进度] 迭代 {event.get('iteration')}")

            await asyncio.sleep(poll_interval)

        return f"[超时] 等待子 Agent '{name}' 超过 {timeout} 秒"

    def list_children(self) -> list[str]:
        """列出所有子 Agent。"""
        if not self.parent_session.children_dir.exists():
            return []
        return [d.name for d in self.parent_session.children_dir.iterdir() if d.is_dir()]

    def kill_child(self, name: str) -> str:
        """向子 Agent 发送中断信号。"""
        child_session = self.parent_session.child_session(name)
        child_session.send_interrupt(source="parent")

        task = self._child_tasks.get(name)
        if task and not task.done():
            task.cancel()
            return f"[系统] 子 Agent '{name}' 已发送中断信号并取消任务"
        return f"[系统] 子 Agent '{name}' 未运行，但已发送中断信号"

    async def send_message_to_child(self, name: str, content: str) -> str:
        """向子 Agent 发送消息。"""
        child_session = self.parent_session.child_session(name)
        child_session.send_message(content, source="parent")
        return f"[系统] 已向子 Agent '{name}' 发送消息"
