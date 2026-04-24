"""Agenda — DAG-native Agent Runtime with Multi-Agent support.

设计原则：
- 文件系统即状态
- 目录即 Session
- 双目录隔离
- DAG 原生
- Hook 即策略
- AI 自压缩记忆
- 子 Agent 嵌套

依赖：标准库 + pyyaml + openai
"""

__version__ = "0.0.6"

from .const import (
    EXIT_SUCCESS,
    EXIT_ARGS_ERROR,
    EXIT_DAG_CONFIG_ERROR,
    EXIT_EXECUTION_ERROR,
    EXIT_DEPENDENCY_ERROR,
)
from .models import ModelConfig, ModelRegistry
from .guardian import Guardian
from .session import Session
from .hooks import HookRegistry
from .tools import ToolRegistry, build_tools
from .agent import AgentLoop
from .subagent import SubAgentManager
from .scheduler import DAGScheduler
from .daemon import NodeWatcher
from .cli import cli

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
    "HookRegistry",
    "ToolRegistry",
    "build_tools",
    "AgentLoop",
    "SubAgentManager",
    "DAGScheduler",
    "NodeWatcher",
    "cli",
]
