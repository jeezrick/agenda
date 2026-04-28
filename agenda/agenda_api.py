"""agenda() 顶层函数 — 递归调用的统一入口。

设计：
- Base Case: 单节点 DAG → 直接 AgentLoop.run()，跳过 Scheduler 开销
- Recursive Step: 多节点 DAG → Scheduler.run() 并行调度
- agenda() 是普通函数，Agent 调用它和调用 read_file 没有区别

对应 README 待实现项 #2、#3、#4。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import AgentLoop
from .const import DEFAULT_MAX_ITERATIONS, DEFAULT_NODE_TIMEOUT, MAX_SUB_AGENT_DEPTH
from .scheduler import DAGScheduler
from .session import Session
from .tools import ToolRegistry


async def run_agent_node(
    session: Session,
    node_config: dict,
    model_registry: Any,
    tools_factory: Callable[[Session], ToolRegistry],
    depth: int = 0,
) -> str:
    """运行单个 Agent 节点的核心逻辑。

    被 DAGScheduler._run_node() 和 run_sub_dag() Base Case 共用，
    消除重复代码（DRY）。

    Args:
        session: 已准备好的 Session（hints/inputs/history 就绪）
        node_config: 节点配置字典
        model_registry: 模型注册表
        tools_factory: 工具工厂函数
        depth: 当前递归深度

    Returns:
        Agent 的最终输出文本
    """
    tools = tools_factory(session)

    # ── 注入 agenda() 递归工具 ──────────────────────────────────
    @tools.register("agenda")  # type: ignore[arg-type]
    async def agenda_tool(
        dag_yaml: str,
        workspace: str | None = None,
        inputs_json: str = "{}",
    ) -> str:
        """启动子 DAG 实现递归分解。dag_yaml 为 DAG 的 YAML 定义。"""
        import yaml as _yaml

        dag_spec = _yaml.safe_load(dag_yaml)
        ws = Path(workspace) if workspace else session.workspace_dir / "subdags"
        ws.mkdir(parents=True, exist_ok=True)

        # 深度软约束
        if depth >= MAX_SUB_AGENT_DEPTH:
            return (
                f"[深度限制] 当前深度 {depth} 已达软上限 "
                f"{MAX_SUB_AGENT_DEPTH}。建议在当前层级完成任务，"
                f"或精简子 DAG 规模。"
            )

        results = await run_sub_dag(
            dag_spec=dag_spec,
            workspace=ws,
            model_registry=model_registry,
            tools_factory=tools_factory,
            depth=depth + 1,
        )
        return json.dumps(results, ensure_ascii=False)

    # 构建 system prompt（Jinja2 模板化）
    hints = session.read_system("hints.md")
    tools_description = tools.describe()
    system_prompt = DAGScheduler._render_system_prompt(hints, tools_description)

    # 创建并运行 AgentLoop
    agent = AgentLoop(
        session=session,
        model_registry=model_registry,
        tools=tools,
        model=node_config.get("model"),
        max_iterations=node_config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        timeout=node_config.get("timeout", DEFAULT_NODE_TIMEOUT),
        node_id=session.node_dir.name,
    )

    result = await agent.run(system_prompt, node_config.get("prompt", ""))

    # 写入产物（如果 Agent 没有自己写）
    if not session.output_exists and result:
        session.write_file("output/draft.md", result)

    return result


async def run_sub_dag(
    dag_spec: dict,
    workspace: Path,
    model_registry: Any,
    tools_factory: Callable[[Session], ToolRegistry],
    depth: int = 0,
) -> dict[str, str]:
    """运行子 DAG，自动退化 Base Case。

    Args:
        dag_spec: DAG 定义字典（同 dag.yaml 解析后的结构）
        workspace: 子 DAG 的工作目录
        model_registry: 模型注册表
        tools_factory: 工具工厂函数（接收 Session 返回 ToolRegistry）
        depth: 当前递归深度

    Returns:
        节点状态映射 {node_id: "COMPLETED"|"FAILED"|"PENDING"}
    """
    nodes = dag_spec.get("nodes", {})
    if not nodes:
        return {}

    # ── Base Case 优化 ───────────────────────────────────────────
    # 单节点 DAG 直接 AgentLoop.run()，不创建 Scheduler、不写 scheduler_state
    if len(nodes) == 1:
        node_id = list(nodes.keys())[0]
        node_cfg = nodes[node_id]
        node_dir = workspace / "nodes" / node_id
        session = Session(node_dir)

        # 构造 hints（复用 scheduler 的 hints 逻辑）
        available_files = []
        if session.input_dir.exists():
            for f in sorted(session.input_dir.rglob("*")):
                if f.is_file():
                    available_files.append(str(f.relative_to(session.input_dir)))

        files_section = ""
        if available_files:
            files_section = "\n## 可用输入文件\n"
            for p in available_files:
                files_section += f'- read_file("input/{p}")\n'

        hints = f"""# 任务: {node_id}
## 提示
{node_cfg.get("prompt", "")}{files_section}
## 规则
- 用 read_file / write_file 工具操作文件
- 按需读取 input/ 下的内容，不要一次性加载所有
- workspace/ 可放草稿和中间产物
- 完成后将最终产物写入 output/draft.md
- 如需继续分解任务，使用 agenda(dag_yaml) 工具
"""
        session.write_system("hints.md", hints)

        try:
            await run_agent_node(
                session=session,
                node_config=node_cfg,
                model_registry=model_registry,
                tools_factory=tools_factory,
                depth=depth,
            )
            session.set_state("status", "completed")
            return {node_id: "COMPLETED"}
        except asyncio.CancelledError:
            session.set_state("status", "cancelled")
            raise
        except Exception:
            session.set_state("status", "failed")
            return {node_id: "FAILED"}

    # ── Recursive Step ───────────────────────────────────────────
    scheduler = DAGScheduler(workspace, f"subdag_{depth}")
    scheduler.dag = dag_spec
    return await scheduler.run(tools_factory=tools_factory)
