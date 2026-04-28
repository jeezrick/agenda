# Subagent 调度机制调研报告

> 调研对象：Butterfly Agent、Claw Code、Kimi CLI、Claude Code、Codex  
> 调研目的：为 Agenda 的 DAG 层设计提供参考，明确"Subagent 一等公民"的实现路径

---

## 核心问题

现有框架如何处理以下问题：
1. 怎么触发 subagent？（调度入口）
2. 怎么传递任务？（task assignment）
3. 怎么传递输入文件/数据？（input routing）
4. 怎么传递 context？（context propagation）
5. 怎么回传结果？（result return）
6. 怎么隔离 workspace/session？（isolation）
7. 怎么限制嵌套深度？（depth control）

---

## 一、Butterfly Agent

### 1.1 架构概览

```
Parent Session (daemon)
  ├── Agent.run() → calls subagent_new tool
  │       └── SubAgentTool.execute()
  │             ├── _spawn_child()
  │             │     └── init_session()  [writes _sessions/<child>/manifest.json + context.jsonl]
  │             └── _wait_for_reply() ─────┐
  │                                        │
  │   ┌────────────────────────────────────┘
  │   │  (BridgeSession polls child context.jsonl)
  │   ▼
  Child Session (new daemon task)
  │   └── SessionWatcher discovers manifest
  │       └── Session.run_daemon_loop()
  │           └── reads user_input from context.jsonl
  │               └── Agent.run(task) → produces turn
  │                   └── assistant reply written to context.jsonl
  │
  └── Background path: SubAgentRunner + BackgroundTaskManager
        └── result delivered via event queue → _drain_background_events()
            └── appended as user_input notification to parent context.jsonl
```

Butterfly 采用**独立 daemon 进程**模型。每个 session 是一个独立的 asyncio Task，由 `SessionWatcher` 扫描 `_sessions/<id>/manifest.json` 后启动。

### 1.2 调度入口

**Tool：`subagent_new`**

```json
// toolhub/subagent_new/tool.json
{
  "name": "subagent_new",
  "backgroundable": true,
  "input_schema": {
    "required": ["name", "task", "mode"],
    "properties": {
      "name": { "type": "string" },
      "task": { "type": "string" },
      "mode": { "enum": ["explorer", "executor"] },
      "timeout_seconds": { "type": "integer" }
    }
  }
}
```

**调度流程**（`butterfly/tool_engine/sub_agent.py`）：

```python
class SubAgentTool:
    def execute(self, name, task, mode, timeout_seconds=...):
        # 1. 创建子 session 目录
        child_id = _spawn_child(name, task, mode)
        # 2. 将 task 包装为子 session 的第一条 user_input
        _compose_initial_message(task, mode) → 写入 context.jsonl
        # 3. 启动子 session daemon
        SessionWatcher.discover_and_start(child_id)
        # 4. 等待结果
        if not background:
            result = _wait_for_reply(child_id, timeout)
            return result
        else:
            return {"task_id": child_id, "status": "running"}
```

**深度限制**：`_MAX_SUB_AGENT_DEPTH = 2`，记录在 `manifest.json` 中，daemon 重启后仍然有效。

### 1.3 任务传递

任务通过 `task` 参数传递，成为子 session 的**第一条 user message**：

```python
# _compose_initial_message(task, mode)
# 包装为：{"role": "user", "content": task, "timestamp": ...}
# 写入 _sessions/<child_id>/context.jsonl
```

子 session 的 `run_daemon_loop()` 从 `context.jsonl` 中读取这条消息，开始执行。

### 1.4 输入文件/数据传递

Butterfly 采用**多层文件传递机制**：

| 机制 | 实现 | 代码位置 |
|-----|------|---------|
| **Playground 继承** | 子 session 的 `playground/parent` → symlink 到父 session playground | `session_init.py:310-328` |
| **Meta session seeding** | 从父 meta session 复制 `system.md`, `task.md`, `env.md`, `tools.md`, `skills.md`, `memory.md` 到子 session `core/` | `session_init.py` |
| **Memory seeding** | 主 memory + `core/memory/*.md` 复制到子 session | `session_init.py` |
| **Playground seeding** | Meta session playground 文件递归复制到子 session（不覆盖） | `session_init.py` |
| **独立 workspace** | 子 session 有自己的 `docs/`, `playground/`, `core/` | `session_init.py` |

```python
# session_init.py 第 310-328 行
if parent_session_id is not None:
    parent_playground = s_base / parent_session_id / "playground"
    if parent_playground.is_dir():
        link_target = session_dir / "playground" / "parent"
        if not link_target.exists() and not link_target.is_symlink():
            try:
                link_target.symlink_to(parent_playground.resolve(), target_is_directory=True)
            except OSError:
                pass
```

**关键设计**：子 agent 可以通过 symlink 读取父 playground，但不能写入（Guardian 拦截写入父目录）。

### 1.5 Context 传递

**消息历史：不传递。** 子 session 从全新的 `context.jsonl` 开始，只有一条初始任务消息。

**Context 传递清单**：

| Context 类型 | 传递方式 |
|-------------|---------|
| System prompt | 从 meta session 复制 `system.md` → 子 `core/system.md` |
| Mode prompt | 根据 mode 复制 `toolhub/subagent_new/<mode>.md` → `core/mode.md` |
| Env context | 复制 `env.md`，`{session_id}` 替换为子 ID |
| Task prompt | 复制 `task.md` |
| Memory | 复制 `memory.md` + `core/memory/*.md` |
| Skills | 复制 `skills.md` + `core/skills/` |
| Tools | 复制 `tools.md` + `core/tools/` |
| Config | 复制 `config.yaml` |
| 环境变量 | `BUTTERFLY_SESSION_ID` = child_id |
| Parent 关系 | `manifest.json` 记录 `parent_session_id`, `mode`, `sub_agent_depth` |

### 1.6 结果回传

**同步模式**（`run_in_background=false`）：

```python
# bridge.py
async def async_wait_for_reply(self, msg_id: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    offset = self._ipc.context_size()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        # 读取 context.jsonl 中新的 turn 事件
        # 匹配 user_input_id == msg_id
        # 返回 assistant content
```

Parent 通过 `BridgeSession` 轮询子 session 的 `context.jsonl`，提取匹配的 assistant 回复。

**后台模式**（`run_in_background=true`）：

1. 子 session 完成后，结果存入 `PanelEntry.meta["result"]`
2. `BackgroundTaskManager` 发射 `BackgroundEvent(kind="completed", ...)`
3. Parent 的 `_drain_background_events()` 消费队列，构建通知消息追加到 parent 的 `context.jsonl`
4. 通知消息包含子 agent 的**最终回复全文**（8000 字截断）

```python
# session.py _drain_background_events
if is_sub_agent:
    sub_result = (entry.meta or {}).get("result") or "(empty reply)"
    msg = (
        f"sub_agent{_sub_mode_str} {_sub_display} completed{duration}.\n\n"
        f"{sub_result}"
    )
```

**Cancel 传播**：parent cancel 时，`SubAgentTool.execute()` catch `CancelledError`，调用 `BridgeSession.send_interrupt()` 中断子 session。

### 1.7 Workspace/Session 隔离

**完整的多层隔离**：

```
sessions/<id>/          ← agent 可见工作目录
  core/
  docs/
  playground/
  .venv/

_sessions/<id>/         ← 系统级目录（agent 不可见）
  manifest.json
  status.json
  context.jsonl         ← 独立消息历史
  events.jsonl          ← 独立运行时事件
  tool_results/
```

- **进程隔离**：每个 session 独立 `asyncio.Task`
- **消息历史隔离**：完全独立的 `context.jsonl`
- **Guardian 沙箱**：Explorer 模式限制写入只能在 `playground/` 内
- **深度限制**：`_MAX_SUB_AGENT_DEPTH = 2`
- **资源隔离**：每个 session 独立 Python venv

### 1.8 Butterfly 的关键缺陷

- **Subagent 是二等公民**：特殊 `subagent_new` tool、特殊 spawn 机制、特殊 `mode`（explorer/executor）
- **不能递归**：`MAX_DEPTH = 2`，超过拒绝
- **Context 传递复杂**：需要复制大量文件（system/task/env/skills/memory/tools/config）

---

## 二、Claw Code

### 2.1 架构概览

Claw Code 采用**同进程独立线程**模型。Subagent 在独立 OS 线程中运行，使用全新的 `Session::new()`，但共享文件系统。

```
Parent Thread
  ├── Agent.run() → calls Agent tool
  │       └── execute_agent_with_spawn()
  │             ├── make_agent_id()
  │             ├── build_agent_system_prompt(subagent_type)
  │             ├── allowed_tools_for_subagent(subagent_type)
  │             └── spawn_agent_job(job) → std::thread::spawn()
  │                   └── run_agent_job()
  │                         ├── build_agent_runtime()
  │                         │     └── Session::new()  ← 全新会话
  │                         └── ConversationRuntime::run_turn()
  │                               └── 完成后 persist_agent_terminal_state()
  │                                     └── 写入 .claw/agents/{id}.md + .json
```

### 2.2 调度入口

**Tool：`Agent`**（`tools/src/lib.rs:3477-3559`）

```rust
fn execute_agent_with_spawn<F>(input: AgentInput, spawn_fn: F) -> Result<AgentOutput, String> {
    let agent_id = make_agent_id();
    let system_prompt = build_agent_system_prompt(&normalized_subagent_type)?;
    let allowed_tools = allowed_tools_for_subagent(&normalized_subagent_type);
    let job = AgentJob {
        manifest,
        prompt: input.prompt,
        system_prompt,
        allowed_tools,
    };
    spawn_fn(job)?;
    Ok(manifest)
}

fn spawn_agent_job(job: AgentJob) -> Result<(), String> {
    std::thread::Builder::new()
        .name(format!("clawd-agent-{}", job.manifest.agent_id))
        .spawn(move || {
            let result = std::panic::catch_unwind(
                std::panic::AssertUnwindSafe(|| run_agent_job(&job))
            );
            // ...
        })
}
```

**输入参数**：
- `description`：任务描述
- `prompt`：具体指令
- `subagent_type`：代理类型（`Explore`, `Plan`, `Verification`, `claw-guide`, `statusline-setup`）
- `name`：自定义名称
- `model`：使用的模型（默认 `claude-opus-4-6`）

### 2.3 任务传递

任务通过 `description` + `prompt` 传递，`prompt` 写入 `.claw/agents/{agent_id}.md` 作为任务记录。

**Task Packet（结构化任务）**：
- `RunTaskPacket` 支持传递 `TaskPacket`，包含 `objective`, `scope`, `repo`, `worktree`, `branch_policy`, `acceptance_tests` 等
- 但仅注册到 `TaskRegistry`，**不自动执行**

### 2.4 输入文件/数据传递

**无显式文件传递机制**。Subagent 与 parent 共享同一个工作目录，通过工具（`read_file`, `glob_search` 等）直接访问文件。

### 2.5 Context 传递

**消息历史：不传递。** Subagent 使用全新 `Session::new()`：

```rust
fn build_agent_runtime(job: &AgentJob) -> Result<ConversationRuntime<...>, String> {
    Ok(ConversationRuntime::new(
        Session::new(),  // ← 全新会话
        api_client,
        tool_executor,
        permission_policy,
        job.system_prompt.clone(),
    ))
}
```

**System Prompt**：重新构建，不是 parent 的副本：

```
"You are a background sub-agent of type `{subagent_type}`. 
 Work only on the delegated task, use only the tools available to you, 
 do not ask the user questions, and finish with a concise result."
```

**工具限制**：`allowed_tools_for_subagent()` 按类型限制可用工具：
- `Explore`：只读工具（`read_file`, `glob_search`, `grep_search`）
- **所有 subagent 都排除 `Agent` 工具本身**（禁止递归）

### 2.6 结果回传

1. `run_agent_job()` 调用 `ConversationRuntime::run_turn()` 执行
2. 完成后提取最终文本：`final_assistant_text(&summary)`
3. `persist_agent_terminal_state()` 更新状态：
   - 追加结果到 `.claw/agents/{agent_id}.md` 的 `## Result` 部分
   - 更新 manifest JSON：状态、完成时间、错误信息

**Parent 获取结果**：通过读取 `.claw/agents/{id}.md` 和 `.claw/agents/{id}.json` 文件，**没有自动注入 parent 会话的机制**。

### 2.7 Workspace/Session 隔离

| 维度 | 实现 |
|-----|------|
| **Session 隔离** | ✅ 完全隔离（全新 `Session::new()`，不共享消息历史） |
| **Workspace 隔离** | ❌ 无隔离，共享文件系统 |
| **执行隔离** | ✅ 独立 OS 线程，`catch_unwind` 防止崩溃传播 |
| **工具隔离** | 白名单机制（按 subagent_type） |
| **嵌套限制** | 白名单排除 `Agent` 工具 |

### 2.8 Claw Code 的关键缺陷

- **Subagent 是二等公民**：特殊 `Agent` tool、特殊 `subagent_type`、特殊工具白名单
- **无 Workspace 隔离**：子 agent 可以直接修改 parent 的文件
- **禁止递归**：`Agent` 工具不在 subagent 白名单中
- **结果回传弱**：需要 parent 主动读取文件

---

## 三、Kimi CLI

### 3.1 架构概览

Kimi CLI 采用**同进程 async task** 模型。Subagent 与 parent 在同一个 asyncio event loop 中运行，通过 `await`（foreground）或 `asyncio.create_task()`（background）调度。

```
Parent Async Task
  ├── Agent.run() → calls Agent tool
  │       └── AgentTool.__call__()
  │             ├── if role != "root": return ToolError("Subagents cannot launch other subagents.")
  │             ├── generate agent_id (uuid.hex[:8])
  │             ├── build system prompt from agent spec (coder.yaml, explore.yaml, ...)
  │             ├── Runtime.copy_for_subagent()
  │             │     ├── 共享：config, oauth, llm, session, builtin_args, skills, approval_runtime
  │             │     └── 新建：DenwaRenji, background_tasks, role="subagent"
  │             └──
  │                 ├── Foreground: await ForegroundSubagentRunner.run(req)
  │                 └── Background: BackgroundTaskManager.create_agent_task()
  │
  └── BackgroundTaskManager 跟踪 live_agent_tasks
        └── 完成后发射 notification 到 parent wire
```

### 3.2 调度入口

**Tool：`Agent`**（`src/kimi_cli/tools/agent/__init__.py`）

```python
class AgentTool:
    def __call__(self, description, prompt, subagent_type=None, model=None, 
                 resume=False, run_in_background=False, timeout=None):
        # 关键检查：禁止嵌套
        if self._runtime.role != "root":
            return ToolError(message="Subagents cannot launch other subagents.")
        
        agent_id = uuid.uuid4().hex[:8]
        # 从 agent spec (coder.yaml, explore.yaml) 加载 system prompt
        system_prompt = build_system_prompt(subagent_type)
        
        # 创建 subagent runtime
        sub_runtime = self._runtime.copy_for_subagent(agent_id, subagent_type)
        
        if run_in_background:
            task = BackgroundTaskManager.create_agent_task(req)
            return ToolOk(task_id=task.task_id, status="running")
        else:
            runner = ForegroundSubagentRunner()
            result = await runner.run(req)
            return ToolOk(agent_id=agent_id, status="completed", summary=result)
```

**Agent Type 注册表**（`src/kimi_cli/subagents/registry.py`）：
- `coder`：写代码
- `explore`：探索代码库
- `plan`：规划任务
- 定义在 `src/kimi_cli/agents/default/*.yaml`

### 3.3 任务传递

核心输入是 `prompt: str`，直接作为 subagent 的 user message。

对于 `explore` 类型，系统自动 prepend `<git-context>` 块：

```python
# src/kimi_cli/subagents/core.py:74
if subagent_type == "explore":
    prompt = f"<git-context>\n{git_status}\n</git-context>\n\n{prompt}"
```

Prompt 快照写入 `<subagent_dir>/prompt.txt` 用于调试。

### 3.4 输入文件/数据传递

**无显式文件附件传递**。Subagent 与 parent 共享同一个 `KIMI_WORK_DIR`，通过 `ReadFile`、`Glob`、`Shell` 等工具访问同样的文件系统。

### 3.5 Context 传递

**消息历史：不传递。** 每个 subagent 拥有独立的 `Context` 对象，后端文件是 `<session_dir>/subagents/<agent_id>/context.jsonl`。

**Runtime 复制**（`src/kimi_cli/soul/agent.py:329`）：

```python
def copy_for_subagent(self, agent_id, subagent_type):
    return Runtime(
        config=self.config,           # 共享
        oauth=self.oauth,             # 共享
        llm=self.llm,                 # 共享
        session=self.session,         # 共享同一个 Session 对象
        builtin_args=self.builtin_args,  # 共享（含工作目录）
        environment=self.environment,    # 共享
        skills=self.skills,              # 共享
        additional_dirs=self.additional_dirs,  # 共享
        subagent_store=self.subagent_store,    # 共享
        approval_runtime=self.approval_runtime,  # 共享
        root_wire_hub=self.root_wire_hub,       # 共享
        # 新建
        denwa_renji=DenwaRenji(),           # 独立留言系统
        background_tasks=BackgroundTaskManager(),  # 独立后台任务
        role="subagent",                    # 标记为 subagent
        subagent_id=agent_id,
        subagent_type=subagent_type,
    )
```

**System Prompt**：来自 agent spec（如 `coder.yaml`），不是继承 parent 的：

```yaml
# src/kimi_cli/agents/default/coder.yaml
system_prompt: |
  You are a coding assistant...
tools:
  mode: allowlist
  list:
    - ReadFile
    - StrReplaceFile
    - WriteFile
    - Bash
    # ... 注意：不包含 Agent 工具
```

**Hook 继承**：Foreground subagent 继承 parent 的 `hook_engine`：

```python
# src/kimi_cli/subagents/runner.py:246
runner = ForegroundSubagentRunner(hook_engine=parent.hook_engine)
```

### 3.6 结果回传

**Foreground 模式**：

```python
# runner.py:142
async def run_with_summary_continuation(req):
    soul = prepare_soul(req)
    await soul.run()
    final_response = soul.context.history[-1].extract_text(sep="\n")
    if len(final_response) < 200:
        # 结果太短，追加 continuation prompt 再跑一轮
        await soul.run_turn(continuation_prompt)
        final_response = soul.context.history[-1].extract_text(sep="\n")
    return final_response
```

Parent 收到的是 `ToolOk`：

```
agent_id: a1a2b3c4
resumed: false
actual_subagent_type: coder
status: completed

[summary]
<subagent 的最终回复文本>
```

**Background 模式**：

- 返回任务凭证：`task_id`, `status: running`
- 实际输出写入 `<subagent_dir>/output` 和 `<task_dir>/output.log`
- Parent 可通过 `TaskOutput` 工具查询，或等待 notification

**Wire 消息透传**：

- Subagent 的 wire 消息（`StepBegin`, `TextPart`, `ToolCall`）包装为 `SubagentEvent` 转发到 parent wire
- UI 能实时看到 subagent 执行进度
- `ApprovalRequest` / `QuestionRequest` 直接透传到 parent wire

### 3.7 Workspace/Session 隔离

**隔离粒度**：

```
<session_dir>/subagents/<agent_id>/
  ├── context.jsonl     ← 独立对话历史
  ├── wire.jsonl        ← wire 消息日志
  ├── meta.json         ← 元数据（状态、类型、描述）
  ├── prompt.txt        ← 本次 prompt 快照
  └── output            ← 可读执行 transcript
```

| 维度 | 实现 |
|-----|------|
| **Session** | 共享同一个 `Session` 对象，但独立 `context.jsonl` |
| **进程** | 同进程，`asyncio.create_task()` |
| **Workspace** | 共享文件系统（同一 `KIMI_WORK_DIR`） |
| **工具权限** | `tool_policy`：`mode="inherit"` 或 `"allowlist"` |
| **嵌套限制** | 直接检查 `role != "root"`，禁止嵌套 |

**状态管理**：`SubagentStore` 统一管理所有 subagent 状态：
- `idle`, `running_foreground`, `running_background`, `completed`, `failed`, `killed`

### 3.8 Kimi CLI 的关键缺陷

- **Subagent 是二等公民**：`role != "root"` 检查、`Agent` tool 不可用
- **禁止递归**：明确的 `"Subagents cannot launch other subagents"` 错误
- **Context 传递混合**：共享大量 runtime 对象（session, config, llm），但消息历史独立
- **Workspace 无隔离**：共享文件系统

---

## 四、Claude Code

### 4.1 架构概览

Claude Code 采用**同进程 async + 可选独立工作目录**模型。Subagent（称为 Agent Tool 或 Forked Agent）使用与主循环完全相同的 `query()` 函数，通过 `ToolUseContext` 实现隔离。

```
Parent Async Context
  ├── AgentTool.call()  →  AgentTool.tsx
  │     ├── resolveAgentType()        ← 决定 agent type / fork
  │     ├── runAgent()                ← 通用 subagent 执行
  │     │     ├── createSubagentContext()     ← 隔离的 ToolUseContext
  │     │     │     ├── readFileState: 克隆（独立）
  │     │     │     ├── abortController: 新建（继承 parent）
  │     │     │     ├── queryTracking: 新链（depth+1）
  │     │     │     └── agentId, agentType: subagent 标记
  │     │     ├── resolveAgentTools()         ← 按 agent type 过滤工具
  │     │     ├── getAgentSystemPrompt()      ← 整体替换 prompt
  │     │     └── query()                     ← 同一个 query 循环
  │     │           └── recordSidechainTranscript()
  │     │
  │     └── 异步模式: registerAsyncAgent() + runAsyncAgentLifecycle()
  │
  └── runForkedAgent()  →  forkedAgent.ts
        ├── 字节级缓存共享（CacheSafeParams 匹配主线程 cache key）
        ├── maxTurns=1 的受限 loop
        └── 用于: compact / session memory / promptSuggestion / /btw
```

**两种 subagent 路径**：

| 路径 | 入口 | 用途 | 缓存策略 |
|------|------|------|---------|
| **AgentTool** | `AgentTool.call()` | 用户触发的子代理（Explore/Plan/异步任务） | 独立 cache |
| **Forked Agent** | `runForkedAgent()` | 系统内部子任务（compact/session memory 等） | 共享主线程 cache |

### 4.2 调度入口

**主入口：`AgentTool`**（`src/tools/AgentTool/AgentTool.tsx:239`）

```typescript
class AgentTool {
  async call(input, context): Promise<ToolResult> {
    // 1. 解析 agent type
    //    - subagent_type 指定 → 对应 AgentDefinition
    //    - subagent_type 未指定 → fork 路径或 general-purpose
    
    // 2. 守卫检查
    //    - 递归 fork 检测（isInForkChild）
    //    - team 限制
    //    - MCP server 要求
    
    // 3. 异步/同步分支
    if (shouldRunAsync) {
      registerAsyncAgent(agentId, ...)
      return { status: 'async_launched', agentId }
    }
    
    // 4. 前台执行
    for await (const progress of runAgent(agentId, ...)) {
      trackProgress(progress)  // 可选的 onProgress 回调
    }
    
    // 5. 结果组装
    return finalizeAgentTool(agentId, messages)
  }
}
```

**Agent 类型注册**（声明式 `AgentDefinition`）：

```typescript
type AgentDefinition = {
  name: string
  description: string
  systemPrompt?: string | SystemPromptProducer
  tools?: string[]           // 白名单，'*' = 全部
  excludeTools?: string[]    // 黑名单
  model?: string | ModelDefinition
  permissionMode?: 'bubble' | 'restricted'
  supportsIsolation?: boolean
  cwd?: string
}
```

内置类型按权限模式分三类：
- **General-purpose**（默认）：inherit 权限，完整工具集
- **Explore**：受限只读工具（Read/Search/Glob/Grep）
- **Plan**：规划工具 + 只读工具

**Fork 路径**（`forkSubagent.ts`）：

```typescript
function isForkSubagentEnabled(): boolean {
  // 检查 feature flag + coordinator mode + interactive session
}

function buildForkedMessages(
  directive: string,
  assistantMessage: AssistantMessage,
): Message[] {
  // 克隆 parent 的最后一条 assistant 消息
  // 插入占位 tool_result（空内容）
  // 追加 fork 指令块
  // → 最大化缓存共享（字节级匹配）
}
```

### 4.3 任务传递

任务通过 `AgentInput.prompt` 传递，是 subagent 的**第一条 user message**：

```typescript
// AgentTool input schema
{
  description: string    // 任务描述（UI 显示用）
  prompt: string         // 具体指令（LLM 输入）
  subagent_type?: string // agent type
  model?: string         // 模型
  run_in_background?: boolean
  name: string           // 唯一标识
  mode?: string          // 权限模式
  isolation?: 'worktree' | 'remote'
  cwd?: string           // 工作目录
}
```

**Fork 路径的指令包装**（`forkSubagent.ts:buildChildMessage`）：

```typescript
// fork 子代理的指令包含:
const FORK_TAG = 'This is a forked conversation.'
// + 规则（仅文本输出、不得调用工具/返回 JSON）
// + XML 输出格式
```

### 4.4 输入文件/数据传递

**文件传递方式取决于 isolation 参数**：

| 模式 | 行为 |
|------|------|
| **默认** | 共享文件系统，subagent 可读写 parent 的 CWD |
| **worktree** | `EnterWorktree` 创建临时 git worktree，完成后自动清理 |
| **remote** | 通过 RemoteTrigger API 在独立会话执行 |

对于 `createSubagentContext()`，文件状态传递：

```typescript
function createSubagentContext(parent: ToolUseContext): ToolUseContext {
  return {
    readFileState: clone(parent.readFileState),     // 克隆当前文件缓存
    abortController: new AbortController(parent),    // 继承 parent（parent 取消 → 子取消）
    getAppState: wrap(parent.getAppState, {          // 避免权限提示
      shouldAvoidPermissionPrompts: true,
    }),
    setAppState: () => {},                           // 隔离（不修改 parent state）
    queryTracking: {                                 // 新链
      chainId: parent.queryTracking.chainId,
      depth: parent.queryTracking.depth + 1,
    },
    agentId, agentType,                              // 标记
    // 不传递 UI 相关:
    setInProgressToolUseIDs: () => {},
    setResponseLength: () => {},
    addNotification: undefined,
    setToolJSX: undefined,
    setSDKStatus: undefined,
  }
}
```

### 4.5 Context 传递

**消息历史：不传递。** Subagent 使用全新的 `mutableMessages` 数组，从空的消息历史开始，只有 prompt 作为第一条 user message。

**System Prompt**：根据 AgentDefinition 整体替换（`buildEffectiveSystemPrompt()`）：

```typescript
function buildEffectiveSystemPrompt(
  agentDefinition?: AgentDefinition,
): string[] {
  if (agentDefinition?.systemPrompt) {
    return [agentDefinition.systemPrompt]
  }
  return defaultSystemPrompt  // 或 agent type 的默认 prompt
}
```

**Fork 路径的特殊处理**（`CacheSafeParams`）：

```typescript
type CacheSafeParams = {
  // 这些参数必须与主线程匹配以共享 cache
  systemPromptParams: SystemPromptParams
  getUserContextPromise: Promise<UserContext>
  getSystemContextPromise: Promise<SystemContext>
  forkContextMessages?: Message[]  // 子 agent 的上下文消息
}
```

**Context 传递清单**：

| Context 项 | AgentTool 路径 | Fork 路径 |
|-----------|---------------|-----------|
| 消息历史 | ❌ 不传递 | ❌ 不传递（新数组） |
| System prompt | AgentDefinition 整体替换 | 共享主线程 prompt（字节级匹配） |
| Prompt | `input.prompt` 直接传递 | `directive` 参数 |
| 文件缓存 | `readFileState` 克隆 | 共享 |
| 工具集 | `resolveAgentTools()` 过滤 | `tools` 参数 |
| 权限 | agent mode / bubble | 继承 |
| 取消信号 | 新建（继承 parent） | 主线程 controller |
| Cache | 独立 | CacheSafeParams 共享 |
| MCP 工具 | subagent 可注册独立 server | N/A |
| 工作目录 | 共享/可选 worktree | 共享 |
| 状态 | 隔离 `ToolUseContext` | 隔离 `forkedAgentContext` |

### 4.6 结果回传

**同步模式**（`runAgent()` 生成器）：

```typescript
async function* runAgent(input, context): AsyncGenerator<AgentProgress> {
  // 迭代 query() 循环
  // yield progress 事件（StreamEvent、tool_use、agent_log 等）
  // 最终 yield finalizeAgentTool() 的完整结果
  // → 包含全部消息、工具调用数、耗时
}
```

**异步模式**（`registerAsyncAgent` + `runAsyncAgentLifecycle`）：

```typescript
function registerAsyncAgent(
  agentId: string,
  description: string,
  context: ToolUseContext,
): void {
  // 1. 注册到 appState.tasks
  // 2. dispatchEvent('async_agent_registered')
  // 3. Parent 可通过 TaskGet/TaskList 查询状态
}
```

**AgentTool 结果结构**（`finalizeAgentTool`）：

```typescript
{
  messages: Message[],           // 完整消息历史
  totalToolCalls: number,
  durationMs: number,
  output: string,                // 最终回复文本
  // 异步:
  agentId?: string,
  status?: 'running' | 'completed' | 'failed',
}
```

**Worktree 自动清理**：`isolation: 'worktree'` 的任务完成后，自动 `ExitWorktree`，可选择 keep/remove worktree。

### 4.7 Workspace/Session 隔离

| 维度 | 实现 |
|-----|------|
| **Session 隔离** | ✅ 全新 `mutableMessages[]` + `sidechain transcript` |
| **文件系统隔离** | ⚠️ 默认共享，可选 `isolation: 'worktree'` |
| **进程隔离** | ❌ 同进程（但 `runAsyncAgentLifecycle` 可创建独立进程） |
| **工具隔离** | ✅ `resolveAgentTools()` 按 AgentDefinition 过滤 |
| **MCP 隔离** | ✅ subagent 可注册独立 MCP server（additive） |
| **取消传播** | ✅ parent 取消 → subagent 的 AbortController 自动触发 |
| **嵌套限制** | ⚠️ ALL_AGENT_DISALLOWED_TOOLS 排除 AgentTool（禁止递归），但 fork 路径无限制 |

### 4.8 Claude Code 的关键设计点

1. **两种 subagent 路径**：AgentTool（用户触发的独立 agent）+ Forked Agent（系统内部子任务，缓存共享）。不同于前三家的单一模式。

2. **Fork 路径的缓存优化**：`CacheSafeParams` 保证 fork 的 system prompt + user context + system context 与主线程字节级一致，最大化 prompt cache 命中。

3. **AgentDefinition 声明式**：用声明式 YAML 定义 agent type 的工具集、prompt、permission mode。比白名单/黑名单更灵活。

4. **权限模式 `bubble`**：subagent 的权限决策可以"冒泡"到 parent（借用 parent 的已批准权限），避免重复询问。

5. **创建独立 MCP server 的能力**：subagent 可以注册自己的 MCP 工具，在任务完成后自动清理，不影响 parent 的 MCP 连接。

---

## 五、Codex

### 5.1 架构概览

Codex 有两套 subagent 机制：**V1 多代理（multi_agents）** 和 **V2 多代理（multi_agents_v2）**，均通过 `codex_delegate.rs` 的 `run_codex_thread_interactive()` / `run_codex_thread_one_shot()` 实现底层复用。

```
spawn agent tool
  ├─ [V1] multi_agents/spawn.rs     ← 传统工具路径
  └─ [V2] multi_agents_v2/spawn.rs  ← 新工具路径
       ├─ agent_control.spawn_agent_with_metadata()
       ├─ run_codex_thread_interactive() / run_codex_thread_one_shot()
       │    ├─ Codex::spawn()  ← 创建独立的 Codex + Session
       │    │    └─ SessionSource::SubAgent(subagent_source)
       │    ├─ forward_events()  ← 事件转发（审批路由到 parent）
       │    └─ forward_ops()     ← 操作转发
       └─ 返回 thread_id + agent_path
```

**核心文件**：
- `codex-rs/core/src/codex_delegate.rs` — Subagent 创建与事件转发
- `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs` — V2 spawn 工具
- `codex-rs/core/src/tools/handlers/multi_agents_v2/send_message.rs` — 消息传递
- `codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs` — 等待完成
- `codex-rs/core/src/tools/handlers/multi_agents_v2/close_agent.rs` — 关闭
- `codex-rs/core/src/tools/handlers/multi_agents_v2/list_agents.rs` — 列出
- `codex-rs/core/src/tools/handlers/multi_agents_common.rs` — 共享逻辑
- `codex-rs/core/src/agent/agent_resolver.rs` — Agent 角色解析
- `codex-rs/core/src/agent/control.rs` — Agent 控制（SpawnAgentOptions）

### 5.2 调度入口

V2 spawn Agent 工具通过 `spawn_agent_with_metadata()` 启动 subagent：

```rust
// spawn.rs
async fn handle(invocation: ToolInvocation) -> Result<SpawnAgentResult, FunctionCallError> {
    let args: SpawnAgentArgs = parse_arguments(&arguments)?;  // message, task_name, agent_type, ...
    let fork_mode = args.fork_mode()?;  // None / FullHistory / LastNTurns
    
    // 深度限制检查
    let child_depth = next_thread_spawn_depth(&session_source);
    if exceeds_thread_spawn_depth_limit(child_depth, max_depth) {
        return Err("Agent depth limit reached. Solve the task yourself.");
    }
    
    // 角色应用
    apply_role_to_config(&mut config, role_name)?;
    
    // 发送 spawn 开始事件
    CollabAgentSpawnBeginEvent { call_id, prompt, model, ... }
    
    // 执行 spawn
    let result = session.services.agent_control
        .spawn_agent_with_metadata(config, operation, spawn_source, SpawnAgentOptions {
            fork_parent_spawn_call_id, fork_mode, environments,
        }).await;
    
    // 发送 spawn 完成事件
    CollabAgentSpawnEndEvent { call_id, new_thread_id, new_agent_nickname, ... }
}
```

**SpawnAgentOptions**：
```rust
struct SpawnAgentOptions {
    fork_parent_spawn_call_id: Option<String>,  // fork 模式时的 call_id
    fork_mode: Option<SpawnAgentForkMode>,       // None / FullHistory / LastNTurns(N)
    environments: Option<Vec<...>>,              // 环境变量传递
}
```

### 5.3 任务传递

任务通过 `Op::UserInput` 或 `Op::InterAgentCommunication` 传递：

```rust
// 如果双方都有 agent_path，就用 InterAgentCommunication
Op::InterAgentCommunication {
    communication: InterAgentCommunication::new(
        sender_path,      // parent 的 agent_path
        recipient,        // child 的 agent_path
        Vec::new(),       // 附件
        prompt,           // 任务描述文本
        trigger_turn: true,
    ),
}
// 否则用普通的 UserInput
Op::UserInput { items, ... }
```

### 5.4 Context 传递

Codex 支持三种 Fork 模式：

| Fork 模式 | 行为 | 使用场景 |
|-----------|------|---------|
| `None` (默认) | 全新上下文，不继承历史 | 独立子任务 |
| `FullHistory` | 继承 parent 的完整历史 | 需要全部上下文的子任务 |
| `LastNTurns(N)` | 继承 parent 的最后 N 轮 | 只需近期上下文的子任务 |

```rust
enum SpawnAgentForkMode {
    FullHistory,           // 完整历史
    LastNTurns(usize),     // 最后 N 轮
}
```

**初始上下文注入**：
- `initial_history: Option<InitialHistory>` — 可选传递初始历史
- `environment_manager` — 从 parent 继承
- `skills_manager` / `plugins_manager` / `mcp_manager` — 从 parent 共享

### 5.5 结果回传

Codex 通过事件流回传结果：

```
forward_events(codex, tx_sub, parent_session, parent_ctx, ...):
  loop {
    event = codex.next_event()
    match event:
      ExecApprovalRequest → 路由到 parent 审批
      ApplyPatchApprovalRequest → 路由到 parent 审批
      RequestPermissions → 路由到 parent 审批
      RequestUserInput → 路由到 parent 审批
      TurnComplete / TurnAborted → 转发给调用者
      other → 直接转发
  }
```

**关键设计**：所有审批请求都路由到 parent session，subagent 本身不做权限决策。

### 5.6 Workspace 隔离

- **共享文件系统**：Subagent 和 parent 在同一文件系统
- **TurnContext.cwd**：每个 agent 有自己的工作目录约束
- **独立 Session/Thread**：每个 subagent 有独立的 `CodexThread` 和 `Session`
- **状态持久化**：独立 thread 存储在 state DB 中，通过 `thread_spawn_edges` 追踪 spawn 关系

### 5.7 Codex 的关键设计点

1. **双版本并存**：V1 (multi_agents) 和 V2 (multi_agents_v2) 同时维护，V2 是改进方向

2. **Fork 模式递进**：None → LastNTurns → FullHistory，允许 caller 精确控制继承范围

3. **审批路由到 parent**：Subagent 不自己处理权限，全部路由到 parent session

4. **AgentPath 层次化**：通过 `AgentPath` 追踪 spawn 树结构，支持 root → child → grandchild 的多级层次

5. **InterAgentCommunication**：当双方都有 agent_path 时使用结构化消息，支持触发 turn 语义

---

## 六、综合对比

### 6.1 调度机制对比

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** | **Agenda 目标** |
|-----|-----------|-----------|----------|-----------------|-----------|----------------|
| **调度粒度** | 独立 daemon 进程 | 独立 OS 线程 | 同进程 async task | **同进程 async + 可选 worktree** | **同进程 async + 独立 CodexThread** | 同进程 async（轻量） |
| **触发方式** | `subagent_new` tool | `Agent` tool | `Agent` tool | **AgentTool + runForkedAgent()** | **spawn_agent + codex_delegate** | `agenda()` 函数（普通函数调用） |

### 6.2 Context 传递对比

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** | **Agenda 目标** |
|-----|-----------|-----------|----------|-----------------|-----------|----------------|
| **消息历史** | ❌ 不传递 | ❌ 不传递 | ❌ 不传递 | **❌ 不传递** | **可选（FullHistory / LastNTurns / None）** | ❌ 不传递（显式 inputs） |
| **System prompt** | 复制 system.md | 重建 | 从 spec 加载 | **AgentDefinition 整体替换** | **agent_type 角色 + apply_role_to_config()** | Agent Loop 统一 |
| **环境变量** | `BUTTERFLY_SESSION_ID` | 共享进程环境 | 共享 `builtin_args` | **通过 ToolUseContext 传递** | **TurnEnvironment::selection() 继承** | 通过 `inputs` 显式传递 |
| **文件访问** | Symlink 读父目录 | 共享文件系统 | 共享文件系统 | **默认共享，可选 worktree 隔离** | **共享文件系统 + TurnContext.cwd** | `dep_inputs` 路由 + Workspace 隔离 |
| **工具限制** | Guardian + mode | 白名单 | `tool_policy` allowlist | **AgentDefinition 声明式** | **agent_type 角色 + depth limit** | 无限制（`agenda()` 就是普通 tool） |

### 6.3 结果回传对比

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** | **Agenda 目标** |
|-----|-----------|-----------|----------|-----------------|-----------|----------------|
| **回传方式** | 轮询 context.jsonl | 写入文件 | 提取最后消息 | **完整消息 + onProgress 生成器** | **事件流 forward_events() + InterAgentCommunication** | `output/` 目录 + `dep_inputs` |
| **结构化** | 纯文本（8000字截断） | 纯文本（markdown） | 纯文本 | **ContentBlock[] + 工具调用计数 + 耗时** | **结构化事件 + AgentStatus** | 文件产物（任意格式） |
| **自动注入** | ✅ 后台模式自动注入 | ❌ 需主动读取 | ✅ Foreground 自动返回 | ✅ **Generator yield** | ✅ **事件流自动转发** | ✅ `dep_inputs` 自动路由 |

### 6.4 隔离与限制对比

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** | **Agenda 目标** |
|-----|-----------|-----------|----------|-----------------|-----------|----------------|
| **Workspace 隔离** | ✅ 完整目录隔离 | ❌ 无隔离 | ❌ 共享目录 | **⚡ 默认共享，可选 worktree 隔离** | **共享 + TurnContext.cwd 约束** | ✅ 独立 workspace |
| **Session 隔离** | ✅ 独立 context.jsonl | ✅ 独立 Session | ✅ 独立 context.jsonl | **✅ 独立 sidechain transcript** | **✅ 独立 CodexThread + Session** | ✅ 独立 session |
| **进程隔离** | ✅ 独立 daemon | ✅ 独立线程 | ❌ 同进程 | **❌ 同进程** | **❌ 同进程** | ❌ 同进程（轻量） |
| **嵌套深度** | `MAX_DEPTH = 2` | 禁止（白名单） | 禁止（role 检查） | **⚠️ AgentTool 禁止递归，fork 路径无限制** | **exceeds_thread_spawn_depth_limit + agent_max_depth** | `MAX_DEPTH` 软限制 |
| **Subagent 等级** | 二等公民 | 二等公民 | 二等公民 | **⚡ 双路径：AgentTool（二等）+ Fork（一等）** | **双版本：V1 + V2（改进中）** | **一等公民** |

---

## 七、输入传递机制详解

本章节专门分析三个库在启动 subagent 时，**输入（文件/数据）、任务、context 的具体传递机制**。这是 Agenda `inputs` 参数设计的核心参考。

### 7.1 Butterfly Agent 的输入传递

**任务传递：**

```python
# butterfly/tool_engine/sub_agent.py
class SubAgentTool:
    def execute(self, name, task, mode, timeout_seconds=...):
        child_id = _spawn_child(name, task, mode)
        # _spawn_child 内部调用 init_session(..., initial_message=task)
```

```python
# butterfly/session_engine/session_init.py (第 390-400 行)
if initial_message:
    import uuid
    event = {
        "type": "user_input",
        "content": initial_message,
        "id": initial_message_id or str(uuid.uuid4()),
        "ts": datetime.now().isoformat(),
    }
    with context_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
```

任务通过 `initial_message` 参数传递，成为子 session **第一条 user message**，直接写入 `context.jsonl`。

**文件/数据输入传递：**

```python
# session_init.py 中的文件种子逻辑
# 1. 复制 meta session 的 config.yaml
shutil.copy2(meta_config_path, session_config_path)

# 2. 复制 meta session 的 system.md, task.md, env.md, tools.md, skills.md, memory.md
for fname in ["system.md", "task.md", "env.md", "tools.md", "skills.md", "memory.md"]:
    shutil.copy2(meta_dir / "core" / fname, core_dir / fname)

# 3. 复制 memory 子文件
for src_file in sorted(memory_seed_dirs.glob("*.md")):
    shutil.copy2(src_file, session_memory_dir / src_file.name)

# 4. 复制 playground 文件（不覆盖已有）
for src_path in sorted(meta_playground_dir.rglob("*")):
    if not dst_path.exists():
        shutil.copy2(src_path, dst_path)

# 5. 创建 symlink 到父 playground
if parent_session_id is not None:
    parent_playground = s_base / parent_session_id / "playground"
    link_target = session_dir / "playground" / "parent"
    link_target.symlink_to(parent_playground.resolve(), target_is_directory=True)

# 6. 复制 mode 规则
mode_src = _TOOLHUB_DIR / "subagent_new" / f"{mode}.md"
shutil.copy2(mode_src, core_dir / "mode.md")
```

**Context 传递完整清单：**

| Context 项 | 传递方式 | 是否共享 |
|-----------|---------|---------|
| 消息历史 | ❌ 不传递（子 session 全新 context.jsonl） | 隔离 |
| System prompt | 复制 `system.md` | 共享初始内容 |
| Task prompt | 复制 `task.md` | 共享初始内容 |
| Env | 复制 `env.md`，`{session_id}` 替换为子 ID | 共享模板，独立值 |
| Mode rules | 复制 `toolhub/subagent_new/<mode>.md` | 按 mode 注入 |
| Memory | 复制 `memory.md` + `core/memory/*.md` | 共享 |
| Skills | 复制 `skills.md` + `core/skills/` | 共享 |
| Tools | 复制 `tools.md` + `core/tools/` | 共享 |
| Config | 复制 `config.yaml` | 共享 |
| Playground 文件 | 复制 + symlink `playground/parent/` | 只读共享 |
| 环境变量 | `BUTTERFLY_SESSION_ID` = child_id | 独立 |
| Parent 关系 | `manifest.json` 记录 `parent_session_id` | 元数据 |

---

### 7.2 Claw Code 的输入传递

**任务传递：**

```rust
// rust/crates/tools/src/lib.rs:3477-3481
fn execute_agent(input: AgentInput) -> Result<AgentOutput, String> {
    execute_agent_with_spawn(input, spawn_agent_job)
}

fn execute_agent_with_spawn<F>(input: AgentInput, spawn_fn: F) -> Result<AgentOutput, String> {
    let agent_id = make_agent_id();
    let system_prompt = build_agent_system_prompt(&normalized_subagent_type)?;
    let allowed_tools = allowed_tools_for_subagent(&normalized_subagent_type);
    let job = AgentJob {
        manifest,
        prompt: input.prompt,           // ← 任务直接传递
        system_prompt,
        allowed_tools,
    };
    spawn_fn(job)?;
    Ok(manifest)
}
```

任务通过 `AgentJob.prompt` 直接传递。

**文件/数据输入传递：**

**无显式机制**。Subagent 与 parent 共享同一个工作目录，通过工具自行读取。

**Context 传递完整清单：**

| Context 项 | 传递方式 | 是否共享 |
|-----------|---------|---------|
| 消息历史 | ❌ 不传递（`Session::new()`） | 隔离 |
| System prompt | 重建（非 parent 副本） | 独立 |
| Prompt | `input.prompt` 直接传递 | 独立 |
| 工作目录 | 共享文件系统 | 共享 |
| 环境变量 | 共享进程环境 | 共享 |
| 工具白名单 | `allowed_tools_for_subagent(type)` | 按类型限制 |
| 嵌套限制 | 白名单排除 `Agent` 工具 | 禁止递归 |

---

### 7.3 Kimi CLI 的输入传递

**任务传递：**

```python
# src/kimi_cli/subagents/core.py:60-82
async def prepare_soul(spec: SubagentRunSpec, runtime: Runtime, ...):
    # 1. Build agent from type definition
    agent = await builder.build_builtin_instance(...)
    
    # 2. Restore conversation context (独立)
    context = Context(store.context_path(spec.agent_id))
    await context.restore()
    
    # 3. System prompt
    if context.system_prompt is not None:
        agent = replace(agent, system_prompt=context.system_prompt)
    else:
        await context.write_system_prompt(agent.system_prompt)
    
    # 4. Prompt 处理
    prompt = spec.prompt
    if spec.type_def.name == "explore" and not spec.resumed:
        git_ctx = await collect_git_context(runtime.builtin_args.KIMI_WORK_DIR)
        if git_ctx:
            prompt = f"{git_ctx}\n\n{prompt}"   # ← 自动注入 git context
    
    # 5. 写入快照
    store.prompt_path(spec.agent_id).write_text(prompt, encoding="utf-8")
```

任务通过 `spec.prompt` 传递。对于 `explore` 类型，自动 prepend git context。

**文件/数据输入传递：**

**无显式机制**。Subagent 与 parent 共享 `KIMI_WORK_DIR`，通过工具访问。

**Context 传递完整清单：**

| Context 项 | 传递方式 | 是否共享 |
|-----------|---------|---------|
| 消息历史 | ❌ 不传递（独立 `context.jsonl`） | 隔离 |
| System prompt | 从 agent spec (coder.yaml) 加载 | 独立 |
| Prompt | `spec.prompt` 直接传递 | 独立 |
| Config | `self.config` | 共享引用 |
| OAuth | `self.oauth` | 共享引用 |
| LLM client | `self.llm` | 共享引用 |
| Session | `self.session` | 共享引用 |
| Work dir | `self.builtin_args.KIMI_WORK_DIR` | 共享 |
| Environment | `self.environment` | 共享引用 |
| Skills | `self.skills` | 共享引用 |
| DenwaRenji | `DenwaRenji()`（新建） | 隔离 |
| Background tasks | `copy_for_role("subagent")` | 隔离 |
| Role | `"subagent"` | 标记 |

---

### 7.4 Claude Code 的输入传递

**任务传递：**

任务通过 `AgentInput.prompt` 传递，是 subagent 的**第一条 user message**：

```typescript
// AgentTool input schema
{
  description: string    // 任务描述（UI 显示用）
  prompt: string         // 具体指令（LLM 输入）
  subagent_type?: string // agent type
  model?: string         // 模型
  run_in_background?: boolean
  name: string           // 唯一标识
  mode?: string          // 权限模式
  isolation?: 'worktree' | 'remote'
  cwd?: string           // 工作目录
}
```

**文件/数据输入传递：**

| 传递模式 | 行为 | 适用场景 |
|---------|------|---------|
| **共享文件系统** | subagent 可直接读写 parent 的 CWD | 默认，快速任务 |
| **worktree 隔离** | `EnterWorktree` 创建临时 git worktree，完成后自动清理 | 不影响主分支的任务 |
| **remote** | 通过 RemoteTrigger API 在独立会话执行 | 长时间运行的任务 |

对于 `createSubagentContext()`，通过克隆 `readFileState` 传递文件缓存状态。

**Fork 路径的输入传递**（`CacheSafeParams`）：

```typescript
type CacheSafeParams = {
  systemPromptParams: SystemPromptParams    // 字节级匹配主线程 cache key
  getUserContextPromise: Promise<UserContext>
  getSystemContextPromise: Promise<SystemContext>
  forkContextMessages?: Message[]           // 子 agent 上下文消息
}
```

Fork 路径可传递部分上下文消息（不含历史对话，仅包含当前的 directive）。

**Context 传递完整清单：**

| Context 项 | AgentTool 路径 | Fork 路径 |
|-----------|---------------|-----------|
| 消息历史 | ❌ 不传递（全新 `mutableMessages[]`） | ⚠️ `forkContextMessages` 传递上下文 |
| System prompt | AgentDefinition 整体替换 | 共享主线程 prompt（字节级匹配） |
| Prompt | `input.prompt` | `directive` 参数 |
| 文件缓存 | `readFileState` 克隆 | 共享 |
| 工具集 | `resolveAgentTools()` 过滤 | `tools` 参数 |
| 权限 | agent mode / bubble / restricted | 继承 parent |
| 取消 | 新建 AbortController（继承 parent） | 主线程 controller |
| 缓存 | 独立 | CacheSafeParams 共享 |
| MCP | 可注册独立 server | N/A |
| 工作目录 | 共享 / worktree 隔离 | 共享 |
| 状态 | ToolUseContext 隔离 | forkedAgentContext |

### 7.5 共同模式总结

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** | 模式 |
|-----|-----------|-----------|----------|-----------------|-----------|------|
| **消息历史** | ❌ 不传递 | ❌ 不传递 | ❌ 不传递 | **❌ 不传递 / forkContextMessages 有限传递** | **可选 FullHistory / LastNTurns / None** | **共识：子 agent 不继承 parent 对话历史为主流** |
| **任务传递** | `initial_message` → context.jsonl | `prompt` → AgentJob | `prompt` → spec | **`prompt` → AgentTool input** | **Op::UserInput / InterAgentCommunication** | 都通过参数直接传递 |
| **文件传递** | 复制 + symlink | 无（共享目录） | 无（共享目录） | **共享 / worktree / remote 三种模式** | **共享文件系统** | Butterfly 最复杂，其他共享 |
| **System prompt** | 复制 system.md | 重建 | 从 spec 加载 | **AgentDefinition 替换 / 共享主线程（fork）** | **agent_type 角色 + apply_role_to_config()** | 都不直接继承 parent |
| **环境变量** | `BUTTERFLY_SESSION_ID` | 共享进程环境 | 共享 `builtin_args` | **ToolUseContext 透传** | **TurnEnvironment 选择继承** | 各不相同 |
| **缓存共享** | 无 | 无 | 无 | **fork 路径字节级匹配主线程 cache** | 无 | Claude Code 独有 |
| **Context 规模** | 大量文件复制 | 最小（仅 prompt） | 中等（共享 runtime 引用） | **中等（ToolUseContext 克隆 + CacheSafeParams）** | **中等（Session 共享 + Fork 模式控制）** | Butterfly 最重，Claw 最轻 |

---

## 八、对 Agenda DAG 层设计的启示

### 8.1 必须避免的设计

1. **不要特殊的 subagent API**
   - Butterfly 的 `subagent_new`、Claw 的 `Agent` tool、Kimi 的 `Agent` tool、Claude Code 的 AgentTool、Codex 的 multi_agents_v2/spawn 都是特殊入口
   - Agenda：`agenda()` 就是普通函数，Agent Loop 调用它和调用 `read_file` 没有区别

2. **不要 role/身份区分**
   - Kimi 的 `role != "root"` 检查、Codex 的 SessionSource::SubAgent 都明确区分身份
   - Agenda：没有 main/sub 之分，所有 Agent 共享同一个 AgentLoop

3. **不要工具白名单限制递归**
   - Claw 的 `allowed_tools_for_subagent()`、Claude Code 的 `ALL_AGENT_DISALLOWED_TOOLS` 都排除 Agent 工具
   - Codex 用 exceeds_thread_spawn_depth_limit 做深度限制，但不排除工具
   - Agenda：`agenda()` 在工具集中可用，Agent 可自由调用

4. **不要复杂的文件 seeding**
   - Butterfly 复制大量文件（system/task/env/skills/memory/tools/config/mode）
   - Agenda：通过 `inputs` 参数显式传递，不自动复制

### 8.2 应该借鉴的设计

1. **Workspace 隔离（学 Butterfly + Claude Code worktree）**
   - 每个 `agenda()` 调用创建独立 workspace
   - Agenda：默认 workspace 隔离 + 可选共享模式

2. **Context 不自动继承（学五家 + Codex 可选继承模式）**
   - 子 agent 不自动继承 parent 的消息历史
   - Codex 的 Fork 模式（None / FullHistory / LastNTurns）提供了灵活的可选继承
   - Agenda：通过 `inputs` 参数显式传递上下文

3. **结构化产物回传（改进五家）**
   - Codex 的 InterAgentCommunication + 事件流 forward_events() 模式值得参考
   - Agenda：通过 `output/` 目录产出文件，`dep_inputs` 结构化路由

4. **同进程轻量调度（学 Kimi + Claude Code + Codex）**
   - Codex 的 codex_delegate 也是同进程 async spawn
   - Agenda：同进程 async，支持进度回调

5. **审批路由（学 Codex + Claude Code）**
   - Codex 的 subagent 审批全部路由到 parent session，subagent 不做独立权限决策
   - Claude Code 的 `bubble` 权限模式类似
   - Agenda：子 agent 的权限决策可由 parent 控制

6. **深度限制（学 Codex）**
   - Codex 的 exceeds_thread_spawn_depth_limit + agent_max_depth 是最干净的实现
   - Agenda：MAX_DEPTH 软限制，可配置

### 8.3 Agenda 的 `inputs` 设计草案

基于以上调研，Agenda 的输入传递应该：

```python
@dataclass
class Inputs:
    """agenda() 的输入参数 —— 显式传递，不自动继承。"""
    
    workspace: Path           # 工作目录（独立，非共享）
    files: dict[str, Path]    # dep_inputs 路由的文件 {alias: path}
    context: str | None       # 压缩后的上下文摘要（非完整消息历史）
    metadata: Metadata        # depth, parent_node_id, call_chain 等
```

**关键决策：**

| 决策 | 来源 | 说明 |
|-----|------|------|
| ❌ 不传递消息历史 | 四家共识 | 避免 context 污染和 token 爆炸 |
| ❌ 不自动复制大量文件 | 反对 Butterfly | 通过 `files` 显式指定 |
| ✅ 通过 `dep_inputs` 路由文件 | 改进 Butterfly symlink | 结构化、可验证 |
| ✅ System prompt 统一 | Agenda 创新 | Agent Loop 不分 main/sub |
| ✅ 产物通过 `output/` 目录传递 | 改进"提取文本" | 支持任意格式 |
| ✅ 同进程 async 调度 | 学 Kimi + Claude Code | 轻量、Python 友好 |
| ✅ Context 对象显式传递 | 学 Claude Code | Session 参数，非全局变量 |
| ✅ 异步进度回调 | 学 Claude Code | 可选的 on_progress 生成器 |

### 8.4 Agenda DAG 层的实现要求

基于以上调研，DAG 层需要满足：

| 要求 | 来源 | 实现方式 |
|-----|------|---------|
| **Base Case 退化** | design | `len(dag.nodes) == 1` 时跳过 Scheduler |
| **Workspace 隔离** | Butterfly + Claude Code | 默认独立 workspace，可选共享模式 |
| **输入路由** | Butterfly symlink | `dep_inputs` 映射父目录产物到子 `input/` |
| **Context 显式传递** | 四家共识 | `inputs` 参数，不继承消息历史 |
| **结果结构化回传** | 改进 + Claude Code | `output/` 目录产物 + on_progress 回调 |
| **轻量调度** | Kimi + Claude Code | 同进程 async，非独立 daemon/线程 |
| **深度限制** | Butterfly | `MAX_DEPTH` 参数，软约束 |
| **无特殊身份** | Agenda 创新 | 无 `role`、无特殊 tool、无白名单 |
