# Agenda v0.0.6

> 给 Agent 调度 Agent 的极简 DAG 运行时。

Agenda 是一个原生支持 DAG（有向无环图）的 Agent 运行时。它不是给人类用的交互式工具（如 Claude Code），而是给 **Meta Agent 编排子 Agent** 的基础设施。

设计哲学：**文件即状态，目录即 Session，DAG 即编排，Hook 即策略，Guardian 即边界。**

---

## 特性

- **DAG 原生**：YAML/JSON 定义节点和依赖，自动拓扑排序 + 并行调度
- **多模型支持**：每个节点可指定不同模型（DeepSeek、Kimi、Claude、OpenAI 等）
- **文件系统即状态**：没有数据库，没有 socket，所有状态在文件系统里
- **Guardian 路径边界**：Agent 文件操作受硬边界保护，防路径遍历和 symlink 逃逸
- **双目录隔离**：`.context/`（Agent 可见）和 `.system/`（系统私有）
- **Turn 级持久化**：每轮 LLM 运行自动保存到 `turns.jsonl`，中断后可精确恢复
- **子 Agent 嵌套**：节点内可 `spawn_child` 创建子 Agent，最大深度 2 级
- **IPC 事件队列**：`events.jsonl` 支持父子 Agent 实时通信 + 级联取消
- **AI 自压缩记忆**：Token 满时注入紧急提示，让 Agent 自己归档历史
- **Daemon 模式**：长期驻留后台，自动扫描并恢复 DAG 节点
- **Agent 友好的 CLI**：每个命令支持 `--json` 输出，语义化退出码

---

## 核心场景

```
你给 Meta Agent 一个任务
      ↓
Meta Agent 拆解成 DAG（JSON）
      ↓
Agenda 自动调度并行执行
      ↓
每个节点 = 一个 Agent，可创建子 Agent
      ↓
Ctrl+C 中断 → 重新运行 → 自动从断点恢复
```

---

## 快速开始

### 1. 安装

```bash
pip install git+https://github.com/jeezrick/agenda.git
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

### 3. Meta Agent 创建 DAG

Meta Agent 生成 JSON（LLM 生成 JSON 比 YAML 更不容易出错）：

```json
{
  "dag": {
    "name": "research_report",
    "max_parallel": 3
  },
  "nodes": {
    "collect_sources": {
      "prompt": "收集 5 个关于 AI Agent 的信息源，写入 output/draft.md",
      "model": "gpt-4o"
    },
    "analyze_trends": {
      "prompt": "读取 .context/sources.md，分析技术趋势，写入 output/draft.md",
      "model": "claude",
      "deps": ["collect_sources"],
      "dep_inputs": [
        {"from": "collect_sources/output/draft.md", "to": "sources.md"}
      ]
    },
    "write_report": {
      "prompt": "读取 .context/sources.md 和 .context/trends.md，写综合报告到 output/draft.md",
      "model": "gpt-4o",
      "deps": ["collect_sources", "analyze_trends"],
      "dep_inputs": [
        {"from": "collect_sources/output/draft.md", "to": "sources.md"},
        {"from": "analyze_trends/output/draft.md", "to": "trends.md"}
      ]
    }
  }
}
```

转成 YAML 并验证：

```bash
# JSON → YAML
agenda dag create --from-json task.json -o ./report/dag.yaml

# 验证
agenda dag validate ./report/dag.yaml

# 查看拓扑
agenda dag inspect ./report/dag.yaml
```

> 详见 [`docs/DAG_FORMAT.md`](docs/DAG_FORMAT.md) — 给 Meta Agent 的完整 DAG 格式规范。

### 4. 运行

```bash
# 前台运行
agenda dag run ./report/dag.yaml --models ~/.agenda/models.yaml

# 或后台 Daemon（自动恢复、自动重试）
agenda daemon start ./report --foreground
```

### 5. 中断后恢复

```bash
# Ctrl+C 中断后，重新运行即可自动恢复
agenda dag run ./report/dag.yaml
```

恢复机制：
- `scheduler_state.json` → 知道哪些节点已完成/失败
- 每个节点的 `turns.jsonl` → 恢复对话历史，Agent 从断点继续
- 文件系统扫描 `output/draft.md` → 验证完成状态

### 6. 查看状态

```bash
# 单次查询
agenda dag status ./report/dag.yaml --json

# 实时监听
agenda dag status ./report/dag.yaml --watch --json

# 查看某个节点的对话历史
agenda node history ./report/dag.yaml --node=write_report
```

---

## CLI 命令参考

### DAG 管理

```bash
agenda dag init <path>                          # 初始化 DAG 工作区
agenda dag create --from-json <file> -o <yaml>  # JSON → YAML（Meta Agent 推荐）
agenda dag validate <path> [--json]             # 验证 DAG 配置
agenda dag inspect <path> [--json]              # 查看拓扑结构
agenda dag run <path> [--models <path>] [--max-parallel N] [--dry-run]
agenda dag status <path> [--json] [--watch]
agenda dag stop <path>
```

### 节点管理

```bash
agenda node run <path> --node <id> [--force]    # 运行/重跑单个节点
agenda node reset <path> --node <id>            # 重置节点（清空目录）
agenda node logs <path> --node <id> [--tail N]  # 查看错误日志
agenda node history <path> --node <id> [--json] # 查看对话历史（turns.jsonl）
```

### Daemon 管理

```bash
agenda daemon start <path> [--foreground]       # 启动后台调度器
agenda daemon stop <path>                       # 停止
agenda daemon status <path>                     # 查看状态
```

### 模型管理

```bash
agenda models list [--config <path>] [--json]
agenda models validate [--config <path>]
```

### 环境变量

```bash
export AGENDA_DAG="./report/dag.yaml"           # 默认 DAG 路径
export AGENDA_MODELS="~/.agenda/models.yaml"    # 默认模型配置
export AGENDA_MAX_PARALLEL="4"                  # 默认最大并行度
```

---

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 参数/命令错误 |
| 2 | DAG 配置错误（环、缺失输入等） |
| 3 | 节点执行失败 |
| 4 | 依赖失败导致无法继续 |
| 130 | 用户中断 (Ctrl+C) |

---

## Python API

```python
import asyncio
from agenda import DAGScheduler, build_tools

async def main():
    scheduler = DAGScheduler(".", "report").load()
    results = await scheduler.run(
        tools_factory=lambda session: build_tools(session),
    )
    print(results)
    # {"collect_sources": "COMPLETED", "analyze_trends": "COMPLETED", ...}

asyncio.run(main())
```

---

## 目录结构

```
report/                         # DAG 根目录
├── dag.yaml                    # DAG 定义
├── .system/
│   └── scheduler_state.json    # 调度器运行状态（用于恢复）
└── nodes/
    ├── collect_sources/        # 节点 = Session 目录
    │   ├── .context/           # Agent 可见（读/写）
    │   ├── .system/
    │   │   ├── turns.jsonl     # 对话历史（turn 级持久化）
    │   │   ├── events.jsonl    # IPC 事件队列
    │   │   └── state.json      # 节点状态
    │   ├── output/
    │   │   └── draft.md        # 完成标记
    │   └── children/           # 子 Agent 目录
    └── analyze_trends/
        └── ...
```

---

## 安全

- **Guardian 硬边界**：Agent 只能访问自己 `node_dir` 内的 `.context/` 和 `output/`
- **路径遍历防护**：`../etc/passwd` 会被 `resolve()` + `relative_to()` 拦截
- **Symlink 逃逸防护**：通过 `resolve()` 自动跟随并检测
- **写限制**：Agent 只能写入 `output/` 目录

---

## 设计来源

- **Butterfly Agent**（https://github.com/dannyxiaocn/butterfly-agent）：双目录 Session、Meta Session、Hook 机制、文件系统 IPC、Guardian 边界
- **EVA**（https://github.com/usepr/eva）：极简主义、AI 自压缩记忆、LLM 安全审查

---

## 文档

- [`docs/DAG_FORMAT.md`](docs/DAG_FORMAT.md) — Meta Agent 的 DAG 格式规范
- [`docs/cli-design.md`](docs/cli-design.md) — CLI 设计思路
- [`docs/agenda-design.md`](docs/agenda-design.md) — 运行时架构设计
- [`docs/multi-agent-runtime-ad.md`](docs/multi-agent-runtime-ad.md) — 架构决策记录

---

## License

MIT
