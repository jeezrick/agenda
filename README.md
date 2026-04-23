# Agenda

> 给 Agent 调度 Agent 的极简 DAG 运行时。

Agenda 是一个原生支持 DAG（有向无环图）的 Agent 运行时。它不是给人类用的交互式工具（如 Claude Code），而是给 **Agent 编排 Agent** 的基础设施。

设计哲学：**文件即状态，目录即 Session，DAG 即编排，Hook 即策略。**

---

## 特性

- **DAG 原生**：YAML 定义节点和依赖，自动拓扑排序 + 并行调度
- **多模型支持**：每个节点可指定不同模型（DeepSeek、Kimi、Claude、OpenAI 等）
- **文件系统即状态**：没有数据库，没有 socket，所有状态在文件系统里
- **双目录隔离**：`.context/`（Agent 可见）和 `.system/`（系统私有）
- **AI 自压缩记忆**：Token 满时注入《紧急危机》prompt，让 Agent 自己归档
- **Hook 机制**：在 Agent 循环的关键节点注入策略，不改源码
- **Agent 友好的 CLI**：每个命令支持 `--json` 输出，语义化退出码

---

## 快速开始

### 1. 安装

```bash
# 方式 1：pip
pip install git+https://github.com/jeezrick/agenda.git

# 方式 2：复制单文件
wget https://raw.githubusercontent.com/jeezrick/agenda/main/agenda.py
```

### 2. 配置模型

```bash
mkdir -p ~/.agenda
cat > ~/.agenda/models.yaml << 'EOF'
models:
  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    model: "deepseek-chat"
    token_cap: 64000

  kimi:
    base_url: "https://api.moonshot.cn/v1"
    api_key: "${KIMI_API_KEY}"
    model: "moonshot-v1-8k"
    token_cap: 8000

  claude:
    base_url: "https://api.anthropic.com/v1"
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-3-5-sonnet"
    token_cap: 200000
EOF
```

### 3. 初始化 DAG

```bash
agenda dag init ./my_book/dag.yaml
```

### 4. 定义 DAG

编辑 `./my_book/dag.yaml`：

```yaml
dag:
  name: "Hermes vs OpenClaw"
  max_parallel: 4

nodes:
  ch01_intro:
    model: "deepseek"
    prompt: "写第一章：Agent 爆发背景"
    output: "output/draft.md"

  ch03_hermes:
    model: "kimi"
    prompt: "写第三章：Hermes Agent 深度解析"
    deps: [ch01_intro]
    dep_inputs:
      - from: "ch01_intro/output/draft.md"
        to: "input/deps/ch01_intro/draft.md"
    output: "output/draft.md"

  ch09_compare:
    model: "claude"
    prompt: "写第九章：架构对比"
    deps: [ch03_hermes, ch06_openclaw]
    dep_inputs:
      - from: "ch03_hermes/output/draft.md"
        to: "input/deps/ch03_hermes/draft.md"
      - from: "ch06_openclaw/output/draft.md"
        to: "input/deps/ch06_openclaw/draft.md"
    output: "output/draft.md"
```

### 5. 验证并运行

```bash
# 验证 DAG 配置
agenda dag validate ./my_book/dag.yaml --json

# 运行 DAG
agenda dag run ./my_book/dag.yaml --models ~/.agenda/models.yaml

# 查看状态（实时监听）
agenda dag status ./my_book/dag.yaml --watch --json
```

---

## CLI 命令参考

### DAG 管理

```bash
# 初始化 DAG 工作区
agenda dag init <path>

# 验证 DAG 配置
agenda dag validate <path> [--json]

# 查看 DAG 拓扑结构
agenda dag inspect <path> [--json]

# 运行 DAG
agenda dag run <path> [--models <path>] [--max-parallel N] [--dry-run]

# 查看运行状态
agenda dag status <path> [--json] [--watch]

# 停止 DAG
agenda dag stop <path>
```

### 节点管理

```bash
# 运行单个节点（调试）
agenda node run <path> --node <node_id> [--force]

# 重置节点
agenda node reset <path> --node <node_id>

# 查看节点日志
agenda node logs <path> --node <node_id> [--tail N]

# 查看节点对话历史
agenda node history <path> --node <node_id> [--json]
```

### 模型管理

```bash
# 列出可用模型
agenda models list [--config <path>] [--json]

# 验证模型配置
agenda models validate [--config <path>]
```

### 环境变量

```bash
export AGENDA_DAG="./my_book/dag.yaml"        # 默认 DAG 路径
export AGENDA_MODELS="~/.agenda/models.yaml"  # 默认模型配置
export AGENDA_MAX_PARALLEL="4"                 # 默认最大并行度
```

设置后命令可以简化为：
```bash
agenda dag run
agenda dag status --json
agenda models list
```

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 参数/命令错误 |
| 2 | DAG 配置错误 |
| 3 | 节点执行失败 |
| 4 | 依赖失败导致无法继续 |
| 130 | 用户中断 (Ctrl+C) |

---

## Python API

```python
import asyncio
from agenda import DAGScheduler, build_tools

async def main():
    scheduler = DAGScheduler(".", "my_book").load()
    
    results = await scheduler.run(
        tools_factory=lambda session: build_tools(session),
    )
    print(results)
    # {"ch01_intro": "COMPLETED", "ch03_hermes": "COMPLETED", ...}

asyncio.run(main())
```

---

## 设计来源

Agenda 吸取了以下两个优秀项目的精华：

- **Butterfly Agent**（https://github.com/dannyxiaocn/butterfly-agent）：双目录 Session、Meta Session、Hook 机制、文件系统 IPC
- **EVA**（https://github.com/usepr/eva）：极简主义、AI 自压缩记忆、LLM 安全审查、单文件部署

---

## 文档

- [CLI 设计文档](docs/cli-design.md) — CLI 的完整设计思路
- [核心设计文档](docs/agenda-design.md) — 运行时架构设计
- [架构决策](docs/multi-agent-runtime-ad.md) — Butterfly vs EVA 分析

---

## License

MIT
