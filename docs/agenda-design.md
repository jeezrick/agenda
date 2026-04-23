# Agenda 设计文档

> 一个给 Agent 调度 Agent 的极简运行时。

---

## 1. 设计哲学

### 1.1 这不是给人用的

Claude Code、Cursor、Butterfly 都是**人机交互界面**。它们需要漂亮的 TUI、实时流式输出、打断机制、Web 面板。

Agenda 不是。Agenda 是**给 Agent 用的基础设施**。它的用户是另一个 AI Agent（或一个 Python 脚本），它的界面是文件系统和函数调用，不是终端。

### 1.2 核心原则

| 原则 | 来源 | 含义 |
|------|------|------|
| **文件即状态** | Butterfly | 没有数据库、没有消息队列、没有 socket。所有状态在文件系统里，git 友好、可调试、可恢复。 |
| **目录即 Session** | EVA | `cd` 进一个目录就是进入一个 Session。不需要显式创建、不需要 ID 分配。 |
| **双目录隔离** | Butterfly | `.context/`（Agent 可见）和 `.system/`（系统私有）。Agent 永远不会看到系统内部。 |
| **Hook 即策略** | Butterfly | Agent 循环的关键节点暴露 Hook，让上层注入策略，而不是改 runtime 源码。 |
| **单文件部署** | EVA | 核心运行时应该是一个可以复制粘贴的 Python 文件。没有 pip install，没有依赖地狱。 |
| **DAG 原生** | 新增 | Agent 的调度图是 first-class，不是外挂脚本。 |

---

## 2. 架构概览

```
┌─────────────────────────────────────────────┐
│              调用方（Agent / 脚本）            │
│         "帮我调度 12 个 writer agent"          │
└──────────────────┬──────────────────────────┘
                   │ Python API
                   ▼
┌─────────────────────────────────────────────┐
│              agenda.py（~400 行）             │
│  ┌─────────┐ ┌─────────┐ ┌───────────────┐ │
│  │  dag    │ │ session │ │  agent_loop   │ │
│  │ 调度器  │ │ 管理器  │ │  Agent 循环   │ │
│  └────┬────┘ └────┬────┘ └───────┬───────┘ │
│       └───────────┴──────────────┘          │
│                   │                         │
│  ┌────────────────▼────────────────┐        │
│  │        hook_registry            │        │
│  │  before_tool / after_tool       │        │
│  │  on_complete / on_error         │        │
│  └─────────────────────────────────┘        │
└──────────────────┬──────────────────────────┘
                   │ 文件系统
                   ▼
┌─────────────────────────────────────────────┐
│            workspace/{dag_name}/             │
│  ├── dag.yaml           ← DAG 定义          │
│  ├── meta/              ← 公共配置           │
│  └── nodes/             ← 节点目录           │
│       └── ch03_hermes/                       │
│           ├── .context/   ← Agent 可见       │
│           │   ├── outline.md                 │
│           │   ├── evidence/                  │
│           │   └── deps/                      │
│           ├── .system/    ← 系统私有         │
│           │   ├── session.jsonl              │
│           │   ├── memory/                    │
│           │   └── hints.md                   │
│           └── output/     ← 产物             │
│               └── draft.md                   │
└─────────────────────────────────────────────┘
```

---

## 3. 核心概念

### 3.1 DAG（有向无环图）

DAG 是 Agenda 的核心。一个 DAG 定义了一组节点和它们的依赖关系。

```yaml
# dag.yaml
dag:
  name: "写一本书"
  max_parallel: 4

nodes:
  ch03_hermes:
    prompt: "写第三章：Hermes Agent 深度解析"
    deps: []                       # 无依赖，可以立即启动
    inputs:
      - "meta/outline.md#ch03"    # 从 meta 复制
      - "meta/evidence/E-001.md"
    output: "output/draft.md"

  ch09_compare:
    prompt: "写第九章：架构对比"
    deps: [ch03_hermes, ch06_openclaw]  # 必须等这两个完成
    dep_inputs:
      - from: "ch03_hermes/output/draft.md"
        to: "input/deps/ch03_hermes/draft.md"
    output: "output/draft.md"
```

**DAG 的状态机**：

```
PENDING → READY → RUNNING → COMPLETED
                    │
                    └──→ FAILED
```

状态转换的信号：**文件系统的变化**。
- `output/draft.md` 出现 = COMPLETED
- `.system/error.log` 出现 = FAILED
- `.system/session.jsonl` 出现 = RUNNING

### 3.2 Session（目录）

每个 DAG 节点就是一个 Session。Session 的目录结构是强制的：

```
nodes/{node_id}/
├── .context/       # Agent 的"工作区"，可以读也可以写
│   ├── outline.md      # 编排器注入的输入
│   ├── evidence/       # 证据卡
│   └── deps/           # 上游节点的产物（只读）
│       └── ch03_hermes/
│           └── draft.md
├── .system/        # 系统的"控制室"，Agent 不可见
│   ├── session.jsonl   # 对话历史（append-only）
│   ├── memory/         # AI 自压缩的归档
│   │   └── 20260423_001.md
│   ├── hints.md        # 检索线索
│   └── state.json      # 运行状态
└── output/         # Agent 的产物
    └── draft.md
```

**Agent 被限制在 `.context/` 和 `output/` 内。** 它不能读 `.system/`，也不能写 `nodes/` 下的其他目录。

### 3.3 Agent Loop（核心循环）

Agent Loop 是 Agenda 的心脏。它只做三件事：

```python
while True:
    1. 把 system_prompt + context + task 发给 LLM
    2. LLM 返回：要么是一段文字（完成），要么是一个 tool_call（继续）
    3. 如果是 tool_call：执行 tool，把结果塞回对话，回到 1
```

**极简到只有这三个步骤。** 没有 interrupt 队列、没有 background task、没有 SSE 流。

### 3.4 Tool（工具）

Tool 是一个 Python 函数，被注册到 Agent Loop 中。

```python
# 内置工具示例
def read_file(path: str) -> str:
    """读取 .context/ 或 output/ 下的文件"""
    ...

def write_file(path: str, content: str) -> str:
    """写入 output/ 目录"""
    ...

def list_dir(path: str = ".") -> str:
    """列出目录内容"""
    ...
```

**Agent 通过 tool 与文件系统交互。** 它不会直接 `open()`，而是通过 `read_file` 和 `write_file`。

### 3.5 Hook（钩子）

Hook 是"在关键节点插入策略"的机制。

```python
# 定义 Hook
@agenda.hooks.before_tool
def check_outline_alignment(ctx):
    """Agent 调用 write_file 之前，检查内容是否偏离大纲"""
    if ctx.tool_name == "write_file" and "outline" not in ctx.tool_args["content"]:
        raise AgendaError("偏离大纲，拒绝写入")

@agenda.hooks.after_loop
def save_state(ctx):
    """一轮循环结束后，保存对话历史到 .system/session.jsonl"""
    ...
```

**Hook 让上层 Agent 能控制下层 Agent 的行为，而不需要修改 runtime 源码。**

### 3.6 记忆压缩（自进化）

当对话 token 接近上限时，Agenda 不自动截断。它注入一个**系统级 prompt**，让 Agent 自己决定：

```markdown
《紧急危机》！！！记忆容量即将达到上限。

你需要做三件事：
1. 把当前对话中对你未来完成任务有用的内容，整理成 Markdown 文件，
   写入 .system/memory/YYYYMMDD_N.md
2. 提炼技能和知识，写入 .system/skills/
3. 更新 .system/hints.md，留下检索线索

完成后，调用 done_compact 工具通知系统。
```

**这是从 EVA 移植的核心机制。** 让 AI 自己管理记忆，比固定规则更优雅。

---

## 4. API 设计

### 4.1 给 Agent 用的 Python API

```python
import agenda

# 1. 定义一个 DAG
dag = agenda.DAG.from_yaml("workspace/my_book/dag.yaml")

# 2. 注册 Hook
@dag.hooks.before_tool
def my_policy(ctx):
    ...

# 3. 运行 DAG
results = await dag.run()
# results = {"ch03_hermes": "COMPLETED", "ch09_compare": "COMPLETED", ...}

# 4. 读取产物
draft = (dag.node_dir("ch09_compare") / "output" / "draft.md").read_text()
```

### 4.2 给 Agent 用的命令行 API

```bash
# 初始化 DAG 工作区
agenda init --from-template research-book-studio --topic "Hermes vs OpenClaw"

# 运行整个 DAG
agenda run

# 只运行一个节点（调试）
agenda run --node ch03_hermes

# 查看 DAG 状态
agenda status

# 重置某个节点（让它重新跑）
agenda reset ch09_compare
```

---

## 5. 与 Butterfly / EVA 的对比

| 维度 | Butterfly | EVA | Agenda（本设计） |
|------|-----------|-----|----------------|
| **目标用户** | 人类开发者 | 人类用户 | **Agent / 脚本** |
| **交互模式** | TUI + Web UI | CLI 对话 | **文件系统 + API** |
| **Session 隔离** | 双目录 + meta session | 目录级 JSON | **双目录 + DAG 原生** |
| **并发安全** | JSONL append | JSON 快照（竞争） | **文件锁 + 原子写入** |
| **Hook** | 有 | 无 | **有（简化版）** |
| **记忆压缩** | 无 | AI 自驱动 | **AI 自驱动** |
| **安全审查** | 无 | LLM 审查 | **LLM 审查** |
| **代码量目标** | ~500KB | 27KB | **~400 行** |
| **部署方式** | pip install | 复制粘贴 | **复制粘贴** |
| **DAG** | 无 | 无 | **原生** |

---

## 6. 为什么不是 Butterfly

Butterfly 的设计目标和你不同：

- Butterfly 是 **Claude Code 的竞品** → 需要 PTY、Web UI、实时流、interrupt 队列
- Agenda 是 **Agent 的调度器** → 需要 DAG、批量执行、文件系统状态机

Butterfly 的 `session.py` 130KB 里，**80% 是交互式基础设施**（双队列输入分发、后台任务、SSE 桥接、thinking block 处理）。这些对 Agenda 来说是死重。

**Agenda 不是 Butterfly 的简化版。它是一个不同品类的工具。**

---

## 7. 为什么不是 EVA

EVA 的设计目标也和你不同：

- EVA 是 **个人自动化脚本** → 单文件、单 Session、交互式对话
- Agenda 是 **多 Agent 编排器** → 多 Session、DAG 依赖、批处理执行

EVA 没有：
- Session 间的上下文传递机制
- 并发安全（JSON 快照有竞争条件）
- Hook 系统
- DAG 调度

**Agenda 从 EVA 吸取的是"极简主义精神"，不是代码。**

---

## 8. 实施路线图

### Milestone 1：Agent Loop（第 1 天）

写一个能跑的 `AgentLoop` 类：
- 调用 LLM（OpenAI 兼容接口）
- 解析 tool_calls
- 执行 tool
- 循环直到完成

代码目标：**~150 行**。

### Milestone 2：Session + 文件系统（第 2 天）

- Dual directory 布局
- Tool 的 `read_file` / `write_file` 限制在 `.context/` 和 `output/`
- `session.jsonl` append-only 记录

代码目标：**+100 行**。

### Milestone 3：DAG 调度器（第 3 天）

- YAML DAG 解析
- 拓扑排序
- Asyncio 并行调度（`max_parallel`）
- 文件系统状态机（PENDING → READY → RUNNING → COMPLETED）

代码目标：**+100 行**。

### Milestone 4：Hook + 记忆压缩（第 4 天）

- Hook 注册和触发
- AI 自驱动的《紧急危机》记忆压缩
- LLM 安全审查（放行/禁止）

代码目标：**+80 行**。

### Milestone 5：集成 Research Book Studio（第 5 天）

- `book_writer` agent 模板
- 自动读取 evidence cards
- 自动组装 manuscript.md

代码目标：**+50 行**。

**总计：~480 行代码，5 天完成。**

---

## 9. 核心代码骨架

见同目录下的 `agenda.py`。

---

*文档版本：v0.1*  
*设计日期：2026-04-24*  
*设计目标：给 Agent 调度 Agent 的极简运行时*
