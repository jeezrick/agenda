# Agent Loop 与 Subagent Loop 设计调研报告

> 调研对象：Butterfly Agent、Claw Code、Kimi CLI
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

### 1.4 Prompt 组装对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI |
|-----|-----------|-----------|----------|
| **模板引擎** | 字符串拼接 | 字符串拼接 | Jinja2 (`${VAR}`) |
| **Static/Dynamic 分离** | 有 `_build_system_parts()` | 无 | 无 |
| **Subagent 差异** | mode.md + Agent Collaboration Mode | 固定日期 + 身份声明 | ROLE_ADDITIONAL |
| **Prompt 缓存** | Anthropic prompt caching | 无 | 无 |
| **恢复时复用** | 重新加载文件 | 重新构建 | 复用持久化的 prompt |

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

### 2.4 Context 管理对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI |
|-----|-----------|-----------|----------|
| **存储格式** | JSON Lines | JSON Lines | JSON Lines |
| **原子写** | 普通 append | write_atomic | aiofiles append |
| **日志轮转** | 无 | 256KiB 阈值，3 个归档 | 无 |
| **Compaction** | 未实现 | 保留 4 条，拆 pair 保护 | 保留 2 条，LLM 压缩 |
| **Auto-compaction** | 无 | 100K tokens | ratio + reserved |
| **Token 估算** | provider 真实 usage | len/4 | len/4 |
| **Resume** | _initial_input_offset() | load_from_path() | restore() |
| **Checkpoint/回滚** | 无 | 无 | revert_to() |

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

### 3.4 Tool 管理对比总结

| 维度 | Butterfly | Claw Code | Kimi CLI |
|-----|-----------|-----------|----------|
| **注册方式** | ToolLoader 动态导入 | GlobalToolRegistry | KimiToolset + importlib |
| **依赖注入** | 按 name 注入 | 无 | 自动注入 __init__ 参数 |
| **Subagent 限制** | Guardian (explorer) | 白名单 BTreeSet | allowed_tools + exclude_tools |
| **禁止递归** | MAX_DEPTH=2 | 白名单排除 Agent | exclude_tools 排除 Agent |
| **后台执行** | BackgroundTaskManager | 无 | BackgroundTaskManager |
| **Hook** | 无 | 无 | PreToolUse / PostToolUse |

---

## 四、唤醒/Reload 对比

| 维度 | Butterfly | Claw Code | Kimi CLI |
|-----|-----------|-----------|----------|
| **持久化文件** | context.jsonl | .claw/sessions/<hash>/<id>.jsonl | context.jsonl |
| **恢复粒度** | turn 级别 | 完整 session | checkpoint 级别 |
| **隔离** | session ID | workspace fingerprint | subagent ID |
| **后台任务恢复** | sweep_restart | 无 | background task snapshot |
| **回滚** | 无 | 无 | revert_to() |

---

## 五、Agent vs Subagent 区别对比

### 5.1 是不是同一个类？

| 框架 | 是否同一个类 | 说明 |
|-----|-------------|------|
| **Butterfly** | 同一个 Agent 类 | init_session(mode=...) 区分 |
| **Claw Code** | 同一个 ConversationRuntime<C,T> | 泛型参数不同 |
| **Kimi CLI** | 同一个 KimiSoul | runtime.role 区分 |

**关键洞察：三家都使用同一个核心类，通过注入不同的依赖/配置来区分 main/sub。**

### 5.2 Subagent 的特殊限制

| 限制 | Butterfly | Claw Code | Kimi CLI |
|-----|-----------|-----------|----------|
| **深度限制** | MAX_DEPTH = 2 | 白名单排除 Agent | role != "root" |
| **Workspace 隔离** | 完整目录隔离 | 共享文件系统 | 共享 KIMI_WORK_DIR |
| **消息历史隔离** | 独立 context.jsonl | 独立 Session | 独立 context.jsonl |
| **Tool 白名单** | Guardian (explorer) | allowed_tools_for_subagent() | allowed_tools + exclude_tools |
| **结果可见性** | 只能返回 final reply | 写入文件，parent 读取 | 只能返回 summary |
| **中间步骤可见** | 不可见 | 不可见 | Wire 透传 |
| **MCP 工具** | 无 | 共享 | 不加载 |

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

1. **Context 不继承（学三家）**
   - 子 agent 不自动继承 parent 的消息历史
   - 通过 inputs 参数显式传递压缩后的上下文

2. **System prompt 统一（改进三家）**
   - Butterfly 的 mode.md、Claw 的 sub-agent 声明、Kimi 的 ROLE_ADDITIONAL 都是"补丁"
   - Agenda：同一个 system prompt 模板，不区分 main/sub

3. **Compaction（学 Kimi/Claw）**
   - Butterfly 未实现 compaction
   - Kimi 的 SimpleCompaction + prompts/compact.md 最成熟
   - Agenda：系统驱动 compaction（已由 Kimi 移植）

4. **Checkpoint/回滚（学 Kimi）**
   - Kimi 的 revert_to() 提供了断点回滚能力
   - Agenda 可借鉴用于错误恢复

5. **Prompt 模板化（学 Kimi）**
   - Kimi 的 Jinja2 + builtin_args 是最灵活的方案
   - Agenda：Jinja2 system prompt + 环境变量注入

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
| 同一个 AgentLoop 类 | 三家共识 | 无 special class/role/mode |
| 无 tool 白名单 | Agenda 创新 | agenda() 在 toolset 中可用 |
| Context 不继承 | 三家共识 | 通过 inputs 显式传递 |
| System prompt 统一 | 改进三家 | 同一个 Jinja2 模板 |
| 系统驱动 Compaction | 学 Kimi | SimpleCompaction + compact.md |
| 产物通过 output/ 传递 | 改进三家 | 结构化文件，非文本提取 |
| 同进程 async | 学 Kimi | 轻量、Python 友好 |
