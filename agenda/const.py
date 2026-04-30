from __future__ import annotations

"""常量与默认值。

## 退出码

    0  — 成功
    1  — 参数错误（缺路径、未知命令）
    2  — DAG 配置错误（循环依赖、缺失节点）
    3  — 执行错误（API 错误、tool 异常、超时）
    4  — 依赖失败（上游失败导致下游阻塞）

## Agent 安全限制

    MAX_SUB_AGENT_DEPTH = 2      — 子 Agent 最大嵌套深度（防止无限递归）
    DEFAULT_MAX_ITERATIONS = 50  — Agent 最大迭代轮数
    DEFAULT_NODE_TIMEOUT = 600   — 节点超时秒数
    DEFAULT_MAX_RETRIES = 3      — 节点失败重试次数

## 记忆压缩

    DEFAULT_COMPACTION_TRIGGER_RATIO = 0.75  — 达到上下文的 75% 触发压缩
    DEFAULT_COMPACTION_RESERVED = 2048       — 预留空间触发压缩
    DEFAULT_COMPACTION_MAX_PRESERVED = 4     — 压缩时保留最近 4 条 user/assistant 消息

## 流式输出与并行

    DEFAULT_STREAM = True   — 默认启用流式输出
    MAX_PARALLEL_TOOLS = 10 — 单批最大并行工具调用数
"""


EXIT_SUCCESS = 0
EXIT_ARGS_ERROR = 1
EXIT_DAG_CONFIG_ERROR = 2
EXIT_EXECUTION_ERROR = 3
EXIT_DEPENDENCY_ERROR = 4

# 子 Agent 最大嵌套深度（防止无限 fork）
MAX_SUB_AGENT_DEPTH = 2

# Agent 默认安全限制
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_NODE_TIMEOUT = 600  # 秒
DEFAULT_MAX_RETRIES = 3

# 记忆压缩默认值
DEFAULT_COMPACTION_TRIGGER_RATIO = 0.75
DEFAULT_COMPACTION_RESERVED = 2048
DEFAULT_COMPACTION_MAX_PRESERVED = 4

# 流式输出
DEFAULT_STREAM = True
MAX_PARALLEL_TOOLS = 10
