from __future__ import annotations

"""DAG 调度器（完善版）。"""

import asyncio
import json
import os
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    import sys
    print("[错误] 需要安装 PyYAML: pip install pyyaml")
    sys.exit(1)

from .const import DEFAULT_MAX_ITERATIONS, DEFAULT_NODE_TIMEOUT, DEFAULT_MAX_RETRIES
from .session import Session
from .models import ModelRegistry
from .agent import AgentLoop
from .subagent import SubAgentManager
from .tools import ToolRegistry, build_tools


class DAGScheduler:
    """
    DAG 调度器：
    - 解析 YAML DAG 定义
    - 拓扑排序 + 环检测（DFS）
    - Asyncio 并行调度
    - 文件系统状态机
    - 节点重试策略（最多 3 次）
    - 调度状态持久化（中断后可恢复）
    """

    def __init__(self, workspace: Path, dag_name: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.dag_dir = self.workspace / dag_name
        self.dag_file = self.dag_dir / "dag.yaml"
        self.nodes_dir = self.dag_dir / "nodes"
        self.state_file = self.dag_dir / ".system" / "scheduler_state.json"

        self.dag_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(exist_ok=True)
        (self.dag_dir / ".system").mkdir(parents=True, exist_ok=True)

        self.dag: dict = {}
        self.completed: set[str] = set()
        self.running: set[str] = set()
        self.failed: set[str] = set()
        self.retries: dict[str, int] = {}  # 节点 -> 已重试次数
        self._cancelled = False

        # 加载模型注册表
        self.model_registry = ModelRegistry().load(self.dag_dir)
        print(f"[模型] 可用模型: {', '.join(self.model_registry.list_models())}")

    def load(self) -> DAGScheduler:
        """从 dag.yaml 加载，或创建默认空 DAG。"""
        if self.dag_file.exists():
            self.dag = yaml.safe_load(self.dag_file.read_text(encoding="utf-8")) or {}
        else:
            self.dag = {"dag": {"name": "untitled", "max_parallel": 4}, "nodes": {}}
        return self

    def save(self) -> None:
        self.dag_file.write_text(yaml.safe_dump(self.dag, allow_unicode=True), encoding="utf-8")

    # --- 状态检查 ---

    def node_is_done(self, node_id: str) -> bool:
        """检查节点是否完成：output/draft.md 存在（或配置自定义完成文件）。"""
        session = Session(self.nodes_dir / node_id)
        config = self.dag.get("nodes", {}).get(node_id, {})
        done_file = config.get("done_file")
        return session.is_done(done_file)

    def node_is_failed(self, node_id: str) -> bool:
        return (self.nodes_dir / node_id / ".system" / "error.log").exists()

    def node_is_running(self, node_id: str) -> bool:
        state = Session(self.nodes_dir / node_id).get_state("status")
        return state == "running"

    # --- 调度状态持久化 ---

    def _save_scheduler_state(self) -> None:
        """保存调度器运行状态到文件（用于中断恢复）。"""
        state = {
            "completed": sorted(self.completed),
            "failed": sorted(self.failed),
            "running": sorted(self.running),
            "retries": self.retries,
            "saved_at": datetime.now().isoformat(),
        }
        self.state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_scheduler_state(self) -> None:
        """从文件恢复调度器状态。"""
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.completed = set(state.get("completed", []))
            self.failed = set(state.get("failed", []))
            self.running = set(state.get("running", []))
            self.retries = state.get("retries", {})
            print(f"[调度器] 从状态恢复: 已完成 {len(self.completed)}, 失败 {len(self.failed)}")
        except (json.JSONDecodeError, OSError):
            pass

    # --- 拓扑算法 ---

    def _detect_cycle(self) -> list[str] | None:
        """DFS 环检测。返回环中的节点列表，无环返回 None。"""
        nodes = self.dag.get("nodes", {})
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in nodes}
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            path.append(node)
            for dep in nodes.get(node, {}).get("deps", []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    # 发现环
                    cycle_start = path.index(dep)
                    return path[cycle_start:]
                if color[dep] == WHITE:
                    result = dfs(dep)
                    if result:
                        return result
            path.pop()
            color[node] = BLACK
            return None

        for node in nodes:
            if color[node] == WHITE:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    def topological_sort(self) -> list[str]:
        """返回拓扑排序后的节点列表。"""
        nodes = self.dag.get("nodes", {})
        in_degree = {n: 0 for n in nodes}
        adj = {n: [] for n in nodes}
        for n, cfg in nodes.items():
            for dep in cfg.get("deps", []):
                if dep in adj:
                    adj[dep].append(n)
                    in_degree[n] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        result = []
        while queue:
            n = queue.pop(0)
            result.append(n)
            for m in adj[n]:
                in_degree[m] -= 1
                if in_degree[m] == 0:
                    queue.append(m)
        return result

    def ready_nodes(self) -> list[str]:
        """返回所有依赖已满足且未运行的节点。"""
        ready = []
        for node_id, config in self.dag.get("nodes", {}).items():
            if node_id in self.completed or node_id in self.running or node_id in self.failed:
                continue
            deps = config.get("deps", [])
            if all(d in self.completed for d in deps):
                ready.append(node_id)
        return ready

    # --- 节点准备 ---

    def prepare_node(self, node_id: str) -> Session:
        """准备节点目录：复制 inputs、dep_inputs，恢复历史。"""
        config = self.dag["nodes"][node_id]
        node_dir = self.nodes_dir / node_id
        session = Session(node_dir)

        # 设置 running 状态
        session.set_state("status", "running")
        session.set_state("started_at", datetime.now().isoformat())

        # 1. 复制 meta inputs
        for src_pattern in config.get("inputs", []):
            self._copy_input(src_pattern, session.context_dir)

        # 2. 复制依赖产物
        for mapping in config.get("dep_inputs", []):
            src = self.dag_dir / mapping["from"]
            dst = session.context_dir / mapping["to"].lstrip("/")
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)

        # 3. 恢复历史（如果之前有中断）
        loaded = session.replay_history()
        if loaded:
            print(f"  [节点 {node_id}] 恢复 {len(loaded)} 条历史消息")

        # 4. 写 hints
        hints = f"""# DAG 任务: {node_id}
## 提示
{config.get('prompt', '')}
## 规则
- 用 read_file / write_file 工具操作文件
- 按需读取 input/ 下的内容，不要一次性加载所有
- 完成后写入 output/draft.md
- 如需创建子 Agent，使用 spawn_child 工具
"""
        session.write_system("hints.md", hints)
        return session

    # --- DAG 运行 ---

    async def run(
        self,
        tools_factory: Callable[[Session], ToolRegistry],
    ) -> dict[str, str]:
        """运行整个 DAG，返回每个节点的状态。"""
        max_parallel = self.dag.get("dag", {}).get("max_parallel", 4)
        node_ids = list(self.dag.get("nodes", {}).keys())

        if not node_ids:
            print("[DAG] 空 DAG，无节点可运行")
            return {}

        # 恢复之前的状态
        self._load_scheduler_state()

        # 扫描已完成节点（文件系统 + 持久化状态）
        for n in node_ids:
            if self.node_is_done(n):
                self.completed.add(n)
            elif self.node_is_failed(n):
                # 检查是否可重试
                retries = self.retries.get(n, 0)
                max_retry = self.dag["nodes"][n].get("retries", DEFAULT_MAX_RETRIES)
                if retries < max_retry:
                    print(f"[DAG] 节点 {n} 失败，将重试 ({retries + 1}/{max_retry})")
                    # 清除错误标记，允许重试
                    error_log = self.nodes_dir / n / ".system" / "error.log"
                    if error_log.exists():
                        error_log.unlink()
                else:
                    self.failed.add(n)

        # 崩溃后 running 状态无效，需要重新分类
        for n in list(self.running):
            if n in self.completed or n in self.failed:
                self.running.discard(n)
                continue
            if self.node_is_done(n):
                self.completed.add(n)
                self.running.discard(n)
            elif self.node_is_failed(n):
                retries = self.retries.get(n, 0)
                max_retry = self.dag["nodes"][n].get("retries", DEFAULT_MAX_RETRIES)
                if retries < max_retry:
                    print(f"[DAG] 节点 {n} 上次运行失败，将重试 ({retries + 1}/{max_retry})")
                    error_log = self.nodes_dir / n / ".system" / "error.log"
                    if error_log.exists():
                        error_log.unlink()
                    self.running.discard(n)
                else:
                    self.failed.add(n)
                    self.running.discard(n)
            else:
                # 既没有完成也没有失败，被中断了，重置为 pending
                self.running.discard(n)

        print(f"[DAG] 总节点: {len(node_ids)}, 已完成: {len(self.completed)}, 失败: {len(self.failed)}")

        pending_tasks: dict[str, asyncio.Task] = {}

        while len(self.completed) + len(self.failed) < len(node_ids):
            if self._cancelled:
                print("[DAG] 调度器被取消")
                break

            # 清理已完成的任务
            done_tasks = [n for n, t in pending_tasks.items() if t.done()]
            for n in done_tasks:
                del pending_tasks[n]
                self.running.discard(n)
                if self.node_is_done(n):
                    self.completed.add(n)
                    print(f"[节点] {n} 完成")
                elif self.node_is_failed(n):
                    retries = self.retries.get(n, 0)
                    max_retry = self.dag["nodes"][n].get("retries", DEFAULT_MAX_RETRIES)
                    if retries < max_retry:
                        self.retries[n] = retries + 1
                        # 不清除，等待下一轮 ready_nodes 重新调度
                        print(f"[节点] {n} 失败，将在下一轮重试 ({self.retries[n]}/{max_retry})")
                    else:
                        self.failed.add(n)
                        print(f"[节点] {n} 最终失败（重试耗尽）")
                self._save_scheduler_state()

            # 如果有节点失败且不可重试，终止 DAG（保守策略）
            if self.failed:
                # 检查是否还有依赖未满足的节点依赖于失败的节点
                for n in list(self.failed):
                    for other, cfg in self.dag.get("nodes", {}).items():
                        if n in cfg.get("deps", []) and other not in self.completed and other not in self.failed:
                            print(f"[DAG] 节点 {n} 失败导致下游 {other} 无法执行")
                # 如果所有 remaining 节点都依赖 failed 节点，则终止
                remaining = set(node_ids) - self.completed - self.failed
                blocked = {n for n in remaining if any(d in self.failed for d in self.dag["nodes"][n].get("deps", []))}
                if blocked == remaining:
                    print(f"[DAG] 所有剩余节点被失败节点阻塞，终止")
                    break

            # 死锁检测
            ready = self.ready_nodes()
            remaining = set(node_ids) - self.completed - self.failed - self.running
            if not ready and not self.running and remaining:
                print(f"[DAG] 死锁！剩余节点: {remaining}")
                break

            # 启动就绪节点（不超过 max_parallel）
            slots = max_parallel - len(self.running)
            for node_id in ready[:slots]:
                if self._cancelled:
                    break
                task = asyncio.create_task(
                    self._run_node(node_id, tools_factory),
                    name=f"node_{node_id}",
                )
                pending_tasks[node_id] = task
                self.running.add(node_id)
                self._save_scheduler_state()

            if pending_tasks:
                # 等待任意任务完成或 1 秒超时（用于轮询新就绪节点）
                try:
                    done, _ = await asyncio.wait(
                        pending_tasks.values(),
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=1.0,
                    )
                except asyncio.CancelledError:
                    self._cancelled = True
                    for t in pending_tasks.values():
                        t.cancel()
                    raise
            else:
                await asyncio.sleep(0.5)

        # 等待所有 pending 任务完成
        if pending_tasks:
            await asyncio.gather(*pending_tasks.values(), return_exceptions=True)
            for n, t in pending_tasks.items():
                self.running.discard(n)
                if self.node_is_done(n):
                    self.completed.add(n)
                elif self.node_is_failed(n):
                    self.failed.add(n)

        self._save_scheduler_state()
        return {
            n: ("COMPLETED" if n in self.completed else "FAILED" if n in self.failed else "PENDING")
            for n in node_ids
        }

    def cancel(self) -> None:
        """取消整个 DAG 运行。"""
        self._cancelled = True

    async def _run_node(
        self,
        node_id: str,
        tools_factory: Callable[[Session], ToolRegistry],
    ) -> None:
        """运行单个节点。"""
        self.running.add(node_id)
        config = self.dag["nodes"][node_id]
        model_alias = config.get("model")
        print(f"[节点] {node_id} 启动 (模型: {model_alias or 'default'})")

        try:
            session = self.prepare_node(node_id)

            tools = tools_factory(session)

            # 注入子 Agent 工具
            sub_manager = SubAgentManager(session, self.model_registry)

            @tools.register("spawn_child")
            async def spawn_child(task: str, name: str, model: str | None = None) -> str:
                """创建子 Agent 执行子任务。name 必须唯一。"""
                return await sub_manager.spawn_child(task=task, name=name, model=model)

            @tools.register("wait_for_child")
            async def wait_for_child(name: str, timeout: int = 300) -> str:
                """等待子 Agent 完成并返回结果。"""
                return await sub_manager.wait_for_child(name=name, timeout=float(timeout))

            @tools.register("list_children")
            def list_children() -> str:
                """列出所有子 Agent。"""
                return json.dumps(sub_manager.list_children(), ensure_ascii=False)

            # 构建 system prompt
            hints = session.read_system("hints.md")
            system_prompt = f"""你是一个智能体，正在执行 DAG 任务。

{hints}

# 可用工具
你可以调用以下工具来操作文件系统：
- read_file(path): 读取 .context/ 或 output/ 下的文件
- write_file(path, content): 写入 output/ 目录
- list_dir(path="."): 列出目录内容
- spawn_child(task, name, model?): 创建子 Agent 执行子任务
- wait_for_child(name, timeout?): 等待子 Agent 完成
- list_children(): 列出所有子 Agent

# 记忆线索
{session.read_system("hints.md")}
"""

            agent = AgentLoop(
                session=session,
                model_registry=self.model_registry,
                tools=tools,
                model=model_alias,
                max_iterations=config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
                timeout=config.get("timeout", DEFAULT_NODE_TIMEOUT),
            )

            result = await agent.run(system_prompt, config["prompt"])

            # 写入产物（如果 Agent 没有自己写）
            if not session.output_exists and result:
                session.write_output("draft.md", result)

            session.set_state("status", "completed")
            session.set_state("completed_at", datetime.now().isoformat())
            print(f"[节点] {node_id} 完成")

        except asyncio.CancelledError:
            session = Session(self.nodes_dir / node_id)
            session.set_state("status", "cancelled")
            print(f"[节点] {node_id} 被取消")
            raise
        except Exception as e:
            print(f"[节点] {node_id} 失败: {e}")
            session = Session(self.nodes_dir / node_id)
            session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            session.set_state("status", "failed")

        finally:
            self.running.discard(node_id)

    def _copy_input(self, src_pattern: str, dst_dir: Path) -> None:
        """复制 input 文件到节点 context。支持 #section 锚点。"""
        base = self.dag_dir
        if "#" in src_pattern:
            path, section = src_pattern.split("#", 1)
        else:
            path, section = src_pattern, None

        src = base / path.lstrip("/")
        if not src.exists():
            return

        dst = dst_dir / path.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)

        if section:
            text = src.read_text(encoding="utf-8")
            pattern = rf"##?\s*{re.escape(section)}.*?(?=\n##?\s|\Z)"
            match = re.search(pattern, text, re.DOTALL)
            if match:
                dst.write_text(match.group(0), encoding="utf-8")
            else:
                shutil.copy(src, dst)
        else:
            shutil.copy(src, dst)

