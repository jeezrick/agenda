"""Agenda — DAG-native Agent Runtime with Multi-Agent support.

设计原则：
- 文件系统即状态
- 目录即 Session
- 双目录隔离
- DAG 原生
- Hook 即策略
- AI 自压缩记忆
- 子 Agent 嵌套

核心 API:
    asyncio.run(agenda(dag_spec, workspace))

依赖：标准库 + pyyaml + jinja2 + openai
"""

__version__ = "0.0.6"

from pathlib import Path
from typing import Any

from .agent import AgentLoop
from .cli import cli
from .const import (
    EXIT_ARGS_ERROR,
    EXIT_DAG_CONFIG_ERROR,
    EXIT_DEPENDENCY_ERROR,
    EXIT_EXECUTION_ERROR,
    EXIT_SUCCESS,
)
from .daemon import NodeWatcher
from .guardian import Guardian
from .hook import HookRegistry
from .models import ModelConfig, ModelRegistry
from .scheduler import DAGScheduler
from .session import Session
from .tools import ToolRegistry, build_tools


async def agenda(
    dag_spec: dict,
    workspace: Path | str,
    inputs: dict | None = None,
    *,
    model_registry: ModelRegistry | None = None,
    tools_factory: Any = None,
    hooks: HookRegistry | None = None,
) -> dict[str, str]:
    """统一入口：执行 DAG，自动退化 Base Case。

    这是 Agenda 的核心函数。对 Agent 来说，调用 agenda() 和调用 read_file()
    没有区别——它就是一个普通工具。

    Base Case: 单节点 → 直接 AgentLoop.run()，零调度开销。
    Recursive Step: 多节点 → Scheduler.run() 并行调度。

    Args:
        dag_spec: DAG 定义字典（同 dag.yaml 解析后的结构）
        workspace: 工作目录路径
        inputs: 可选输入参数（预留）
        model_registry: 模型注册表（默认从 workspace 加载）
        tools_factory: 工具工厂函数（默认 build_tools）

    Returns:
        节点状态映射 {node_id: "COMPLETED"|"FAILED"|"PENDING"}

    Example:
        >>> dag = {
        ...     "dag": {"name": "example", "max_parallel": 4},
        ...     "nodes": {
        ...         "research": {"prompt": "调研..."},
        ...         "write": {"prompt": "写作...", "deps": ["research"]},
        ...     },
        ... }
        >>> results = asyncio.run(agenda(dag, "/tmp/work"))
    """
    from .agenda_api import run_sub_dag

    workspace = Path(workspace)
    if model_registry is None:
        model_registry = ModelRegistry().load(workspace)
    if tools_factory is None:
        tools_factory = build_tools

    return await run_sub_dag(
        dag_spec=dag_spec,
        workspace=workspace,
        model_registry=model_registry,
        tools_factory=tools_factory,
        depth=0,
        hooks=hooks,
    )


__all__ = [
    "EXIT_SUCCESS",
    "EXIT_ARGS_ERROR",
    "EXIT_DAG_CONFIG_ERROR",
    "EXIT_EXECUTION_ERROR",
    "EXIT_DEPENDENCY_ERROR",
    "ModelConfig",
    "ModelRegistry",
    "Guardian",
    "Session",
    "ToolRegistry",
    "build_tools",
    "AgentLoop",
    "DAGScheduler",
    "NodeWatcher",
    "HookRegistry",
    "agenda",
    "cli",
]
