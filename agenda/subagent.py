from __future__ import annotations

"""子 Agent 系统（简化版 Butterfly sub_agent）。"""

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

    设计：不实现 Butterfly 的完整 BridgeSession，而是用文件系统状态机：
    - 父 Agent 调用 spawn_child → 创建子 session + 启动子 Agent 任务
    - 子 Agent 运行完成后写 output/done.json
    - 父 Agent 轮询 wait_for_child 读取结果

    最大嵌套深度：MAX_SUB_AGENT_DEPTH（默认 2）
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
        # 向上追溯 children/ 目录层数
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
        # 如果已存在且已完成，拒绝覆盖
        if child_session.is_done("done.json"):
            return f"[错误] 子 Agent '{name}' 已存在且已完成"

        # 准备子 Agent 的 system prompt
        child_system = system_prompt or f"""你是一个子 Agent，被父 Agent 委派执行特定任务。

# 规则
- 你是独立的 Agent，有自己的 .context/ 和 output/
- 完成任务后，必须写入 output/draft.md 和 output/done.json
- done.json 格式: {{"status": "completed", "summary": "任务摘要"}}
- 如果失败，写入 output/done.json: {{"status": "failed", "error": "错误信息"}}

# 记忆线索
当前嵌套深度: {current_depth + 1}/{self.max_depth}
"""

        # 创建子 Agent 的工具集
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
                # 写完成标记
                child_session.write_output("done.json", json.dumps({
                    "status": "completed",
                    "summary": result[:500] if result else "",
                    "finished_at": datetime.now().isoformat(),
                }, ensure_ascii=False))
                if not child_session.output_exists:
                    child_session.write_output("draft.md", result)
            except Exception as e:
                child_session.write_output("done.json", json.dumps({
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "finished_at": datetime.now().isoformat(),
                }, ensure_ascii=False))
                child_session.write_system("error.log", traceback.format_exc())

        task_obj = asyncio.create_task(_run_child(), name=f"subagent_{name}")
        self._child_tasks[name] = task_obj
        return f"[系统] 子 Agent '{name}' 已启动（深度: {current_depth + 1}/{self.max_depth}）"

    async def wait_for_child(self, name: str, poll_interval: float = 1.0, timeout: float = 300.0) -> str:
        """轮询等待子 Agent 完成。返回结果摘要。"""
        child_session = self.parent_session.child_session(name)
        deadline = time.monotonic() + timeout
        done_path = child_session.output_dir / "done.json"

        while time.monotonic() < deadline:
            if done_path.exists():
                try:
                    done = json.loads(done_path.read_text(encoding="utf-8"))
                    status = done.get("status", "unknown")
                    if status == "completed":
                        draft = child_session.read_context("output/draft.md")
                        return f"[子 Agent '{name}' 完成]\n{draft[:2000]}"
                    else:
                        return f"[子 Agent '{name}' 失败] {done.get('error', '未知错误')}"
                except Exception as e:
                    return f"[错误] 读取子 Agent '{name}' 结果失败: {e}"
            await asyncio.sleep(poll_interval)

        return f"[超时] 等待子 Agent '{name}' 超过 {timeout} 秒"

    def list_children(self) -> list[str]:
        """列出所有子 Agent。"""
        if not self.parent_session.children_dir.exists():
            return []
        return [d.name for d in self.parent_session.children_dir.iterdir() if d.is_dir()]

    def kill_child(self, name: str) -> str:
        """取消子 Agent 任务。"""
        task = self._child_tasks.get(name)
        if task and not task.done():
            task.cancel()
            return f"[系统] 子 Agent '{name}' 已取消"
        return f"[系统] 子 Agent '{name}' 未运行"

