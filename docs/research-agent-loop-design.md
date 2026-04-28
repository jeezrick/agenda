# Agent Loop 与 Subagent Loop 设计调研报告

> 调研对象：Butterfly Agent、Claw Code、Kimi CLI、Claude Code
> 调研目的：为 Agenda 的 Agent Loop 层设计提供参考，明确"Subagent 一等公民"的实现路径

---

## 核心问题

1. **Prompt 组装**：Agent 的 system prompt 怎么组装？main/sub 有什么不同？
2. **Context 管理**：消息历史怎么存储、恢复、compaction？
3. **Tool 管理**：tool 怎么注册、调用、结果回传？subagent 的 tool 限制怎么做？
4. **唤醒/Reload**：中断后怎么恢复？从什么文件恢复？
5. **Agent vs Subagent**：是不是同一个类？代码路径有什么不同？有哪些特殊限制？

---

## 一、Prompt 组装对比

### 1.1 Butterfly Agent

**组装函数**：`_build_system_parts()`（`butterfly/core/agent.py:122-166`）

```python
def _build_system_parts(self) -> tuple[str, str]:
    """Return (static_prefix, dynamic_suffix) for cache-aware prompt building.
    static_prefix  — system.md + session context. Stable across activations;
                     eligible for Anthropic prompt caching.
    dynamic_suffix — memory + skills. Changes each activation; not cached.
    """
    static_parts = [self.system_prompt] if self.system_prompt else []
    if self.env_context:
        static_parts.append("\n\n---\n" + self.env_context)

    dynamic_parts: list[str] = []
    if self.memory:
        dynamic_parts.append("\n\n---\n## Session Memory\n\n" + self.memory)
    if self.app_notifications:
        # ... append app notifications
    
    # Agent-mode structured reply guidance
    if getattr(self, "caller_type", "human") == "agent":
        agent_guidance = (
            "\n\n---\n"
            "## Agent Collaboration Mode\n\n"
            "Your caller is another agent (not a human). ..."
            "- **[DONE]** — task completed successfully. ..."
            "- **[REVIEW]** — work finished but needs human review ..."
            "- **[BLOCKED]** — cannot proceed; explain what is needed."
            "- **[ERROR]** — an unrecoverable error occurred; ..."
        )
        dynamic_parts.append(agent_guidance)

    skills_block = build_skills_block(self.skills)
    if skills_block:
        dynamic_parts.append(skills_block)

    return "\n".join(static_parts), "\n".join(dynamic_parts)
```

**Mode Prompt 注入**：`Session._load_session_capabilities()`（`session.py:364-374`）

```python
system_md = self._read_core_text("system.md")
mode_md = self._read_core_text("mode.md")  # from toolhub/subagent_new/<mode>.md

if mode_md:
    self._agent.system_prompt = f"{system_md}\n\n---\n\n{mode_md}" if system_md else mode_md
else:
    self._agent.system_prompt = system_md
```

**Subagent 初始消息包装**（`sub_agent.py:99-107`）：

```python
def _compose_initial_message(task: str, mode: str) -> str:
    return (
        f"[Sub-agent task — mode: {mode}]\n\n"
        f"You are a sub-agent spawned by a parent session. ..."
        f"End your reply with [DONE], [REVIEW], [BLOCKED], or [ERROR].\n\n"
        f"## Task\n\n{task}"
    )
```

**Main vs Subagent Prompt 差异**：

| 维度 | Main Agent | Subagent |
|------|-----------|----------|
| static_prefix | system.md + env.md | system.md + mode.md + env.md |
| dynamic_suffix | memory + skills + app notifications | 同上 + Agent Collaboration Mode |
| 初始消息 | 用户原始输入 | `_compose_initial_message()` 包装 |
| caller_type | "human" | "agent" |

---

### 1.2 Claw Code

**Main Agent System Prompt**（`rust/crates/runtime/src/prompt.rs:144-166`）：

```rust
pub fn build(self) -> Vec<String> {
    let mut parts = vec![
        get_simple_intro_section(),
        // # Output Style
        // # System — 规则 bullet
        // # Doing tasks
        // # Executing actions with care
        "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__".to_string(),
        // # Environment context — CWD、日期、平台
        // # Project context — git status、diff、commits
        // # Claude instructions — CLAUDE.md
        // # Runtime config — .claw/settings.json
    ];
    parts
}
```

**Subagent System Prompt**（`rust/crates/tools/src/lib.rs:3619-3632`）：

```rust
fn build_agent_system_prompt(subagent_type: &str) -> Result<Vec<String>, String> {
    let mut prompt = load_system_prompt(
        cwd,
        "2026-03-31".to_string(),  // 固定日期
        std::env::consts::OS,
        "unknown",                  // OS 版本固定
    )?;
    prompt.push(format!(
        "You are a background sub-agent of type `{subagent_type}`. "
        "Work only on the delegated task, use only the tools available to you, "
        "do not ask the user questions, and finish with a concise result."
    ));
    Ok(prompt)
}
```

**Main vs Subagent Prompt 差异**：

| 维度 | Main Agent | Subagent |
|------|-----------|----------|
| 日期 | 实际当前日期 | 固定 `2026-03-31` |
| OS 版本 | 实际系统版本 | `"unknown"` |
| 末尾追加 | 无 | sub-agent 身份与行为约束 |

---

### 1.3 Kimi CLI

**System Prompt 加载**（`src/kimi_cli/soul/agent.py:484-509`）：

```python
def _load_system_prompt(path, args, builtin_args):
    system_prompt = path.read_text(encoding="utf-8").strip()
    env = JinjaEnvironment(
        loader=FileSystemLoader(path.parent),
        variable_start_string="${",
        variable_end_string="}",
        undefined=StrictUndefined,
    )
    template = env.from_string(system_prompt)
    return template.render(asdict(builtin_args), **args)
```

**基础模板**（`agents/default/system.md`）包含 `${...}` 占位符：

```markdown
${ROLE_ADDITIONAL}
...
You are running on **${KIMI_OS}**.
The current date and time in ISO format is `${KIMI_NOW}`.
...
${KIMI_WORK_DIR_LS}
...
${KIMI_AGENTS_MD}
...
${KIMI_SKILLS}
```

**Main Agent**（`agents/default/agent.yaml`）：

```yaml
agent:
  name: ""
  system_prompt_path: ./system.md
  system_prompt_args:
    ROLE_ADDITIONAL: ""
```

**Subagent**（`agents/default/coder.yaml`）：

```yaml
agent:
  extend: ./agent.yaml
  system_prompt_args:
    ROLE_ADDITIONAL: |
      You are now running as a subagent. All the user messages are sent by the main agent...
```

**恢复时复用**（`subagents/core.py:65-68`）：

```python
if context.system_prompt is not None:
    agent = replace(agent, system_prompt=context.system_prompt)
else:
    await context.write_system_prompt(agent.system_prompt)
```

---

### 1.4 Claude Code

**System Prompt 组装流水线**（多阶段，按顺序叠加）：

```
Stage A: 默认提示           src/constants/prompts.ts:getSystemPrompt()
   ├─ Intro: "You are Claude Code, Anthropic's official CLI..."
   ├─ System: 规则、hook、压缩说明
   ├─ Doing Tasks: 软件工程任务指令
   ├─ Actions: 可逆/不可逆操作指南
   ├─ Using Your Tools: 工具使用说明
   ├─ Tone/Style: 语气风格
   ├─ Output Efficiency
   ├─ SYSTEM_PROMPT_DYNAMIC_BOUNDARY  ← 缓存分界标记
   └─ Dynamic: 记忆、MCP 指令、草稿板等

Stage B: 系统上下文           src/context.ts:getSystemContext()
   ├─ git status --short
   ├─ 当前分支、最近 commit、git user
   └─ cache-breaker 注入

Stage C: 用户上下文           src/context.ts:getUserContext()
   ├─ CLAUDE.md（项目层次发现）
   └─ 当前日期

Stage D: API 层前缀          src/services/api/claude.ts
   ├─ attribution header
   ├─ CLI sysprompt prefix
   └─ advisor 指令（如启用）

Stage E: QueryEngine 组装    src/QueryEngine.ts:submitMessage()
   ├─ fetchSystemPromptParts()（并行获取 A+B+C）
   ├─ asSystemPrompt([default, memory, append])
   └─ appendSystemContext + prependUserContext 注入
```

**关键设计：SYSTEM_PROMPT_DYNAMIC_BOUNDARY**

```typescript
// src/constants/prompts.ts
// 当全局缓存作用域启用时，在静态段和动态段之间插入此标记。
// 所有在标记之前的内容都有机会获得长缓存 TTL。
// 标记之后的内容在每次会话中变化，不参与缓存。
const SYSTEM_PROMPT_DYNAMIC_BOUNDARY = `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`
```

**Agent 类型与 Prompt 覆盖**（`src/utils/systemPrompt.ts:buildEffectiveSystemPrompt()`）：

```typescript
function buildEffectiveSystemPrompt(
  agentDefinition?: AgentDefinition | undefined,
): string[] {
  // 优先级:
  // 1. overrideSystemPrompt (API)
  // 2. coordinator prompt
  // 3. agent prompt (替换默认)
  // 4. customSystemPrompt
  // 5. defaultSystemPrompt
}
```

几种 agent type 的 prompt 差异：
- **Explore**: 只读 prompt，固定日期 "2026-03-31"
- **Plan**: 强调规划输出格式
- **General-purpose**: 使用默认 prompt
- **Fork**: 克隆 parent 的完整 system prompt（共享 cache）

### 1.5 Codex

**Prompt 组装**（通过 Session + TurnContext 构建 Prompt）：

```rust
// codex-rs/core/src/session/turn.rs:build_prompt()
pub(crate) fn build_prompt(input, router, turn_context, base_instructions) -> Prompt {
    Prompt {
        input,                        // 历史消息
        tools,                        // 模型可见的工具列表
        parallel_tool_calls,          // 是否支持并行工具调用
        base_instructions,            // 基础系统指令
        personality,                  // 个性化偏好（如 "concise", "friendly"）
        output_schema,                // 结构化输出 JSON schema
        output_schema_strict,         // 严格模式开关
    }
}
```

**Base Instructions 来源**（SessionConfiguration）：
```
base_instructions: String,       // 模型的核心指令（系统提示）
developer_instructions: Option,   // 开发者补充指令
user_instructions: Option,        // 用户自定义指令
compact_prompt: Option,          // 压缩提示覆盖
personality: Option<Personality>, // 人格偏好
```

**Subagent 的 Prompt 差异**：
- **Subagent 使用相同的 Prompt 结构**，但 `base_instructions` 相同
- 通过 `SessionSource::SubAgent` 区分主体/子体
- 角色通过 `agent_type` 参数选择：`apply_role_to_config()` 加载角色的 YAML 配置来修改 prompt
- `hierarchical_agents_message.md` 提供层次化代理的通用提示模板

**关键设计**：
- Prompt 是数据，不是字符串拼接 — `Prompt` struct 明确包含所有构建要素
- 所有指令通过 `SessionConfiguration` 管理，非全局状态
- 模型指令通过 `ModelProviderInfo::get_model_instructions(personality)` 动态获取

### 1.6 Prompt 组装对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** | **Codex** |
|-----|-----------|-----------|----------|-----------------|-----------|
| **模板引擎** | 字符串拼接 | 字符串拼接 | Jinja2 (`${VAR}`) | **多阶段流水线（拼接 + 注入）** | **结构化 Prompt struct** |
| **Static/Dynamic 分离** | `_build_system_parts()` | 无 | 无 | **`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记** | **SessionConfiguration 分层** |
| **Subagent 差异** | mode.md + Agent Collaboration Mode | 固定日期 + 身份声明 | ROLE_ADDITIONAL | **AgentDefinition.getSystemPrompt() 整体替换** | **agent_type 角色配置 + hierarchical_agents 模板** |
| **Prompt 缓存** | Anthropic prompt caching | 无 | 无 | **forkedAgent 共享主线程 cache（全缓存复用）** | 无 |
| **恢复时复用** | 重新加载文件 | 重新构建 | 复用持久化的 prompt | **上下文不变，仅切换 query 循环** | **从 state DB + rollout 恢复** |
| **Context 注入时机** | 运行时拼接 | 构建时 | 模板渲染时 | **API 调用时 appendSystemContext + prependUserContext** | **build_initial_context + record_context_updates** |

---

## 二、Context 管理对比

### 2.1 Butterfly Agent

**存储格式**：`context.jsonl`（JSON Lines）

```python
# event types
{"type": "user_input", "content": "...", "id": "...", "ts": "..."}
{"type": "turn", "triggered_by": "...", "messages": [...], "ts": "..."}
{"type": "task_wakeup", "card": "...", "prompt": "...", "ts": "..."}
```

**写入**：普通 append
**恢复**：`load_history()` 从 `context.jsonl` 读取所有 turn 的 messages
**Resume 定位**：`_initial_input_offset()` 找到最后一个 turn 之后的字节位置
**Compaction**：❌ 未实现（todo.md 列为待办）
**Token 估算**：provider 返回的真实 usage

---

### 2.2 Claw Code

**存储格式**：JSON Lines（原子写 + 日志轮转）

```rust
pub fn save_to_path(&self, path) -> Result<(), SessionError> {
    let snapshot = self.render_jsonl_snapshot()?;
    rotate_session_file_if_needed(path)?;  // 256KiB 阈值
    write_atomic(path, &snapshot)?;        // temp + rename
    cleanup_rotated_logs(path)?;           // 保留 3 个归档
    Ok(())
}
```

**Compaction**（`compact.rs`）：
- 保留最近 4 条消息
- 将更早历史打包成 System 角色的 summary
- **关键**：不拆散 assistant(ToolUse) / tool(ToolResult) 对

**Auto-compaction**：100K input tokens 阈值
**Token 估算**：`text.len() / 4 + 1`

---

### 2.3 Kimi CLI

**存储格式**：`context.jsonl`

```python
# role 类型
{"role": "_system_prompt", "content": "..."}
{"role": "_usage", "token_count": 1234}
{"role": "_checkpoint", "id": 0}
{"role": "user", "content": [...]}
{"role": "assistant", "content": [...]}
{"role": "tool", "content": [...], "tool_call_id": "..."}
```

**Compaction**（`SimpleCompaction`）：
- 保留最近 2 条消息
- 更早历史发送给 compaction LLM（`prompts/compact.md`）
- 清空并 rotate 旧文件

**回滚**：`revert_to(checkpoint_id)` — 保留 checkpoint 之前记录
**Token 估算**：`total_chars // 4`

---

### 2.4 Claude Code

**存储格式**：与 Anthropic API 一致的 `ContentBlock` 数组，序列化为 session JSONL：

```
~/.claude/projects/<project>/sessions/<sessionId>/
  ├── transcript.jsonl       ← 主会话消息（JSONL，含 parentUuid 链）
  ├── sidechain/              ← 子 Agent 独立转录
  │   └── <agentId>.jsonl
  └── metadata.json           ← session 元数据
```

每条消息包含 `uuid`、`parentUuid`（形成链式结构）、`type`、`message`（含 role/content/usage）。

**消息类型层次**（`src/types/message.ts`）：

```
Message (union)
  ├── UserMessage        ← 用户输入
  ├── AssistantMessage   ← API 回复
  ├── AttachmentMessage  ← 附件（文件/Skill/Plan/Agent 状态等）
  ├── SystemMessage      ← 系统提示
  ├── SystemCompactBoundaryMessage  ← 压缩边界标记
  ├── ProgressMessage    ← 进度事件（不写入 transcript）
  ├── TombstoneMessage   ← 已删除消息的占位
  └── ToolUseSummaryMessage  ← 工具调用的合并摘要
```

**Compaction**（详细见 `docs/research-context-compaction.md`）：4 层混合策略
1. Session Memory Compaction — 复用已提取的记忆，零成本
2. LLM Full Compaction — Claude API 生成结构化 9 章节摘要 + 附件恢复
3. Microcompact — time-based（清空冷缓存的工具输出） + cached（cache_edits API 服务端删除）
4. Reactive Compact — API 413 时被动触发

**Token 估算**：
```typescript
// src/services/tokenEstimation.ts
// 字符数/4 启发式 + content block 类型精确统计
// 按 block type 分别计算: text → len/4, image → 2000, 
// tool_result → 递归计算, thinking → len/4
// 最终 padding * 4/3 保守上浮
```

### 2.5 Context 管理对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** |
|-----|-----------|-----------|----------|-----------------|
| **存储格式** | JSON Lines | JSON Lines | JSON Lines | **JSON Lines + parentUuid 链式结构** |
| **原子写** | 普通 append | write_atomic | aiofiles append | **recordTranscript + flushSessionStorage** |
| **日志轮转** | 无 | 256KiB 阈值，3 个归档 | 无 | **sidechain 独立，session 无轮转** |
| **Compaction** | 未实现 | 保留 4 条，拆 pair 保护 | 保留 2 条，LLM 压缩 | **4 层混合（SM + LLM + MC + Reactive）** |
| **Auto-compaction** | 无 | 100K tokens | ratio + reserved | **contextWindow - 13K tokens + 断路器** |
| **Token 估算** | provider 真实 usage | len/4 | len/4 | **按 block type 精确估算 + 4/3 padding** |
| **Resume** | _initial_input_offset() | load_from_path() | restore() | **transcript JSONL 回放** |
| **Checkpoint/回滚** | 无 | 无 | revert_to() | **无显式 checkpoint** |
| **消息链** | 无 | 无 | 无 | **parentUuid 形成有向链** |

---

## 三、Tool 管理对比

### 3.1 Butterfly Agent

**注册**：`ToolLoader` 从 `tools.md` + `toolhub/<name>/` 动态加载 schema + executor
**调用**：`Agent.run()` 循环中 `provider.complete(messages, tools)` → `_execute_tools()`
**结果回传**：Anthropic-format `tool_result` block 追加到 messages
**Subagent 差异**：Guardian 限制 explorer mode 的写入范围

### 3.2 Claw Code

**注册**：`GlobalToolRegistry` 聚合 builtin + plugin + runtime (MCP)
**Main Agent Executor**：`CliToolExecutor`（交互式、MCP 桥接）
**Subagent Executor**：`SubagentToolExecutor`（白名单 `BTreeSet`，无交互）
**白名单**：
```rust
"Explore" => ["read_file", "glob_search", "grep_search", ...]
"Plan" => ["read_file", "TodoWrite", "SendUserMessage", ...]
// 所有 subagent 排除 "Agent" 工具
```

### 3.3 Kimi CLI

**注册**：`KimiToolset` + importlib 动态导入 + 依赖注入
**调用**：`KimiSoul._step()` → `kosong.step()` → `KimiToolset.handle()`
**结果回传**：`tool_result_to_message()` → `_grow_context()`
**Subagent Policy**：`ToolPolicy(mode="inherit" | "allowlist")`
**coder.yaml**：
```yaml
allowed_tools: [Shell, ReadFile, WriteFile, ...]
exclude_tools: [Agent, AskUserQuestion]
```

---

### 3.4 Claude Code

**Tool 接口**（`src/Tool.ts`）：

```typescript
interface Tool<Input, Output, P> {
  name: string
  aliases?: string[]
  searchHint?: string
  
  call(args: Input, context: ToolUseContext, canUseTool: CanUseToolFn,
       parentMessage: ParentMessage, onProgress?: OnProgressFn): Promise<ToolResult<Output>>
  description(input, options): Promise<string>  // LLM 可见的描述
  inputSchema: Input                            // Zod schema
  inputJSONSchema?: ToolInputJSONSchema
  
  isEnabled(): boolean
  isConcurrencySafe(): boolean
  isReadOnly(): boolean
  isDestructive(): boolean
  
  checkPermissions(input, context): Promise<PermissionResult>
  validateInput(input, context): Promise<ValidationResult>
  prompt(options): Promise<string>              // 生成 LLM-facing 工具描述
}
```

**注册**：`getAllBaseTools()`（`src/tools.ts:191`）→ `assembleToolPool()`（合并 MCP 工具）

**Schema 生成**（`src/utils/api.ts:toolToAPISchema()`）：
- `name` → tool.name
- `description` → tool.prompt()
- `input_schema` → 从 Zod 或 raw JSON 转换
- 可选：`strict`、`defer_loading`、`cache_control`、`eager_input_streaming`
- 结果缓存到 `getToolSchemaCache()`（session-stable）

**调用执行**（`src/query.ts`）：
- `StreamingToolExecutor` 或 `runTools()` 并行执行工具
- 结果通过 `mapToolResultToToolResultBlockParam()` 转换为 API 格式
- 支持 `FILE_UNCHANGED_STUB`（文件未更改时不重复传输）

**Subagent 工具限制**（`src/constants/tools.ts`）：

```typescript
// 所有 subagent 禁止使用的工具
const ALL_AGENT_DISALLOWED_TOOLS = [
  TaskOutput, ExitPlanMode, EnterPlanMode, 
  AgentTool,  // ← 禁止递归
  AskUserQuestion, TaskStop, WorkflowTool,
]

// 异步 agent 允许的工具（只读 + 通用）
const ASYNC_AGENT_ALLOWED_TOOLS = [
  Read, Write, Edit, Bash, Search, 
  Glob, Grep, WebFetch, WebSearch,
  NotebookEdit, Skill, TodoWrite, ...
]
```

由于 Claude Code 使用 agent definition（声明式 YAML）定义每个 agent type 的工具集，限制方式更灵活：

```typescript
// AgentDefinition 可以声明:
// - tools: ['Read', 'Write', 'Bash']  ← 白名单
// - excludeTools: ['AgentTool']         ← 黑名单
// - tools: ['*']                        ← 全部允许（fork agent）
```

### 3.5 Tool 管理对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** |
|-----|-----------|-----------|----------|-----------------|
| **注册方式** | ToolLoader 动态导入 | GlobalToolRegistry | KimiToolset + importlib | **getAllBaseTools() + MCP assembleToolPool()** |
| **依赖注入** | 按 name 注入 | 无 | 自动注入 __init__ 参数 | **ToolUseContext 参数显式传递** |
| **Subagent 限制** | Guardian (explorer) | 白名单 BTreeSet | allowed_tools + exclude_tools | **AgentDefinition 声明式 + ALL_AGENT_DISALLOWED_TOOLS** |
| **禁止递归** | MAX_DEPTH=2 | 白名单排除 Agent | exclude_tools 排除 Agent | **ALL_AGENT_DISALLOWED_TOOLS 排除 AgentTool** |
| **后台执行** | BackgroundTaskManager | 无 | BackgroundTaskManager | **registerAsyncAgent + runAsyncAgentLifecycle** |
| **Hook** | 无 | 无 | PreToolUse / PostToolUse | **checkPermissions → PermissionResult（可 HOOK）** |
| **Schema 缓存** | 无 | 无 | 无 | **getToolSchemaCache() session-stable** |
| **Streaming** | 无 | 无 | 无 | **StreamingToolExecutor 支持** |

---

## 四、唤醒/Reload 对比

| 维度 | Butterfly | Claw Code | Kimi CLI | **Claude Code** |
|-----|-----------|-----------|----------|-----------------|
| **持久化文件** | context.jsonl | .claw/sessions/<hash>/<id>.jsonl | context.jsonl | **transcript.jsonl + sidechain/<agentId>.jsonl** |
| **恢复粒度** | turn 级别 | 完整 session | checkpoint 级别 | **消息级别（parentUuid 链式回放）** |
| **隔离** | session ID | workspace fingerprint | subagent ID | **agentId + sessionId + project 多维** |
| **后台任务恢复** | sweep_restart | 无 | background task snapshot | **registerAsyncAgent 重新发现** |
| **回滚** | 无 | 无 | revert_to() | **--resume 命令行 / 无显式 checkpoint** |
| **Session 元数据** | manifest.json | metadata.json | meta.json | **metadata.json + reAppendSessionMetadata()** |
| **Fork 恢复** | 无 | 无 | 无 | **CacheSafeParams 缓存复用 + forkContextMessages** |

---

## 五、Agent vs Subagent 区别对比

### 5.1 是不是同一个类？

| 框架 | 是否同一个类 | 说明 |
|-----|-------------|------|
| **Butterfly** | 同一个 Agent 类 | init_session(mode=...) 区分 |
| **Claw Code** | 同一个 ConversationRuntime<C,T> | 泛型参数不同 |
| **Kimi CLI** | 同一个 KimiSoul | runtime.role 区分 |
| **Claude Code** | **同一个 query() 函数** | **隔离通过 ToolUseContext + agentId 实现，代码路径完全相同** |

**关键洞察：四家都使用同一个核心类/函数，通过注入不同的依赖/配置来区分 main/sub。** Claude Code 最彻底——subagent 就是调 query()，和主循环无区别。

### 5.2 Subagent 的特殊限制

| 限制 | Butterfly | Claw Code | Kimi CLI | **Claude Code** |
|-----|-----------|-----------|----------|-----------------|
| **深度限制** | MAX_DEPTH = 2 | 白名单排除 Agent | role != "root" | **Agent 工具不暴露（ALL_AGENT_DISALLOWED_TOOLS），但 fork 路径无限制** |
| **Workspace 隔离** | 完整目录隔离 | 共享文件系统 | 共享 KIMI_WORK_DIR | **共享文件系统，可选 worktree 隔离** |
| **消息历史隔离** | 独立 context.jsonl | 独立 Session | 独立 context.jsonl | **独立 sidechain transcript + createSubagentContext()** |
| **Tool 白名单** | Guardian (explorer) | allowed_tools_for_subagent() | allowed_tools + exclude_tools | **AgentDefinition.tools + excludeTools 声明式** |
| **结果可见性** | 只能返回 final reply | 写入文件，parent 读取 | 只能返回 summary | **完整消息 + 产物结构化回传** |
| **中间步骤可见** | 不可见 | 不可见 | Wire 透传 | **可选的 onProgress 回调** |
| **MCP 工具** | 无 | 共享 | 不加载 | **subagent 可注册独立 MCP server（additive）** |
| **Prompt 缓存** | 无 | 无 | 无 | **forkedAgent 字节级缓存共享** |
| **权限模式** | Guardian | permission_policy | tool_policy | **bubble（继承 parent）/ 独立 / 受限** |

---

## 六、对 Agenda Agent Loop 层设计的启示

### 6.1 必须避免的设计

1. **不要特殊的 subagent 类/运行时**
   - Butterfly 的 mode.md、Claw 的 SubagentToolExecutor、Kimi 的 role="subagent" 都是特殊化
   - Agenda：同一个 AgentLoop 类，无 mode/role/特殊 executor

2. **不要 tool 白名单限制递归**
   - Claw 的 allowed_tools_for_subagent()、Kimi 的 exclude_tools 都排除 Agent
   - Agenda：agenda() 就是普通 tool，无限制

3. **不要特殊的结果回传机制**
   - Butterfly 的 [DONE]/[REVIEW]/[BLOCKED]/[ERROR] 前缀
   - Kimi 的 summary continuation
   - Agenda：产物写入 output/ 目录，结构化传递

### 6.2 应该借鉴的设计

1. **Context 不继承（学四家）**
   - 子 agent 不自动继承 parent 的消息历史
   - 通过 inputs 参数显式传递压缩后的上下文

2. **System prompt 统一（改进四家）**
   - Butterfly 的 mode.md、Claw 的 sub-agent 声明、Kimi 的 ROLE_ADDITIONAL 都是"补丁"
   - Claude Code 的 AgentDefinition 整体替换，最干净
   - Agenda：同一个 system prompt 模板，不区分 main/sub

3. **Compaction（学 Kimi + Claude Code）**
   - Butterfly 未实现 compaction
   - Kimi 的 SimpleCompaction + prompts/compact.md 最成熟
   - Claude Code 的 4 层混合提供了完整的方向指引
   - Agenda：系统驱动 compaction（从 Kimi 移植，需加工程防护）

4. **Checkpoint/回滚（学 Kimi）**
   - Kimi 的 revert_to() 提供了断点回滚能力
   - Agenda 可借鉴用于错误恢复

5. **Prompt 模板化（学 Kimi）**
   - Kimi 的 Jinja2 + builtin_args 是最灵活的方案
   - Agenda：Jinja2 system prompt + 环境变量注入

6. **Context 对象隔离（学 Claude Code）**
   - Claude Code 的 ToolUseContext 是显式参数，非全局变量
   - subagent 调用 createSubagentContext() 创建隔离上下文
   - Agenda：Session 作为上下文容器，显式传递

7. **消息链式结构（学 Claude Code）**
   - Claude Code 的 parentUuid 链支持多级关系追踪
   - Agenda：通过 parentUuid 链追踪消息来源

### 6.3 Agenda Agent Loop 的设计草案

```python
class AgentLoop:
    def __init__(self, task: str, workspace: Path, tools: ToolRegistry, model_cfg: ModelConfig):
        self.task = task
        self.workspace = workspace        # input/ workspace/ output/
        self.tools = tools                # 含 agenda() 的普通 toolset
        self.model_cfg = model_cfg
        self.session = Session(workspace) # 独立 context.jsonl
        self.messages: list[Message] = []

    async def run(self) -> Outputs:
        system_prompt = self._build_system_prompt()
        while not self._is_done():
            response = await self._llm_call(system_prompt, self.messages)
            if response.tool_calls:
                results = await self._execute_tools(response.tool_calls)
                self.messages.extend(results)
            else:
                self._write_output(response.content)
                return Outputs(files=self._collect_outputs())

    def _build_system_prompt(self) -> str:
        # Jinja2 模板，注入 builtin_args（KIMI_WORK_DIR_LS 等）
        # 无 main/sub 区分，同一个模板
        template = load_template("system.md")
        return template.render(**self._builtin_args)
```

**关键决策：**

| 决策 | 来源 | 说明 |
|-----|------|------|
| 同一个 AgentLoop 类 | 四家共识 | 无 special class/role/mode |
| 无 tool 白名单 | Agenda 创新 | agenda() 在 toolset 中可用 |
| Context 不继承 | 四家共识 | 通过 inputs 显式传递 |
| System prompt 统一 | 改进四家 | 同一个 Jinja2 模板 |
| 系统驱动 Compaction | 学 Kimi | SimpleCompaction + compact.md |
| 产物通过 output/ 传递 | 改进四家 | 结构化文件，非文本提取 |
| 同进程 async | 学 Kimi | 轻量、Python 友好 |
| 隔离通过上下文对象 | 学 Claude Code | Session 显式传递，非全局变量 |
