# Context Compaction 调研报告

> 调研六个项目中 Context Compaction（上下文压缩/记忆压缩）的实现方案对比。

---

## 目录

1. [Butterfly Agent](#1-butterfly-agent)
2. [Claw Code](#2-claw-code)
3. [Kimi CLI](#3-kimi-cli)
4. [Claude Code](#4-claude-code)
5. [Codex](#5-codex)
6. [Agenda](#6-agenda)
7. [横向对比](#7-横向对比)
8. [总结与建议](#8-总结与建议)

---

## 1. Butterfly Agent

### 状态：无真正的压缩

Butterfly 没有实现通用的 context compaction，而是用几个替代手段间接控制上下文长度。

### 采用的机制

| 机制 | 做法 | 文件 |
|------|------|------|
| Task compact markers | Task tick 执行前注入冗长的 Task 提示，执行完后回滚替换为 `[Task:name ts]` 标记 | `session.py:1217-1224, 1311-1315` |
| Tool output inline cap | 工具输出塞入 context 时截断到 8000 字节 | `session.py:58` |
| On-demand memory recall | 内存文件只显示索引，Agent 用 `memory_recall` 工具按需获取 | `memory_recall/executor.py` |
| **History compact** | **TODO — 计划中但未实现** | `docs/butterfly/todo.md` |

### 关键代码

```python
# Task 执行完成后替换为 compact marker
marker = f"[Task:{card.name} {trigger_ts}]"
new_msgs = [_Msg(role="user", content=marker), *new_msgs[1:]]
self._agent._history = history_snapshot + new_msgs
```

```python
# _reshape_history 识别并丢弃 orphaned task markers
if content.startswith(("Task activation:", "[Task:", "Task wakeup:", "Heartbeat activation:", "[Heartbeat ")):
    new_msgs.pop(0)  # 静默丢弃
```

### 结论

Butterfly 没有做压缩，靠的是"减少塞入"来控制上下文。核心思路是：
1. 不把全部记忆塞进 context（on-demand recall）
2. 执行完的任务替换为轻量标记
3. 工具输出截断到定长

---

## 2. Claw Code

### 状态：纯启发式，不用 LLM

Claw Code (Rust) 的压缩完全不需要 LLM 调用，全部是**启发式提取**。

### 核心文件

- `crates/runtime/src/compact.rs` — 主算法
- `crates/runtime/src/summary_compression.rs` — 二级压缩
- `crates/runtime/src/conversation.rs` — 自动触发集成

### 触发条件

```rust
pub struct CompactionConfig {
    pub preserve_recent_messages: usize,  // 默认 4
    pub max_estimated_tokens: usize,      // 默认 10,000
}
```

当可压缩的消息数超过 `preserve_recent_messages` **且**估计 token 数超过 `max_estimated_tokens` 时触发。

### 压缩算法

```
should_compact()
  ↓
compact_session()
  ├─ 1. 边界安全：从保留边界向前回退，防止孤儿 ToolResult
  ├─ 2. 分割：removed（压缩） / preserved（保留）
  ├─ 3. summarize_messages() — 纯启发式
  │    ├─ Scope: N 条消息统计
  │    ├─ Tools mentioned: 去重工具名
  │    ├─ Recent user requests: 最近 3 条用户消息（160 字符截断）
  │    ├─ Pending work: 关键词匹配（todo/next/pending）
  │    ├─ Key files: 正则提取文件路径
  │    ├─ Current work: 最后非空文本块
  │    └─ Key timeline: 逐条消息缩写
  ├─ 4. merge_compact_summaries() — 重压缩时合并新旧摘要
  └─ 5. 生成 continuation 消息 + 替换 session
```

### 二级压缩 (`summary_compression.rs`)

```rust
pub struct SummaryCompressionBudget {
    pub max_chars: usize,       // 默认 1,200
    pub max_lines: usize,       // 默认 24
    pub max_line_chars: usize,  // 默认 160
}
```

按行优先级贪心选择：
- Priority 0: 摘要头（Summary:、- Scope:、- Current work: 等）
- Priority 1: 章节标题（以 `:` 结尾的行）
- Priority 2: 要点（`- ` 开头）
- Priority 3: 其他

### 自动触发

在 `conversation.rs` 的 `maybe_auto_compact()` 中：每次 assistant turn 完成后检查 `cumulative_input_tokens >= 100_000` 阈值，触发压缩。压缩后执行 session health probe 验证工具执行器仍可用。

### 关键特性

- **不用 LLM** — 完全零成本
- **工具对边界安全** — 防止孤儿 ToolResult，兼容非 Anthropic 的 provider
- **重压缩友好** — 多层历史不会丢失
- **自动触发** — 默认 100K 累计输入 tokens

---

## 3. Kimi CLI

### 状态：LLM 生成结构化摘要

Kimi CLI 的压缩在 `soul/compaction.py`，是 Agenda 的直接前身。

### 核心文件

- `src/kimi_cli/soul/compaction.py` — SimpleCompaction
- `src/kimi_cli/prompts/compact.md` — 压缩提示模板
- `src/kimi_cli/soul/context.py` — 上下文持久化
- `src/kimi_cli/soul/kimisoul.py` — 主循环集成

### 触发条件

双策略 OR 触发：

```
token_count >= max_context_size * trigger_ratio     // 默认 85%
token_count + reserved_context_size >= max_context_size // 默认 50K 保留
```

### 压缩算法

```
prepare()
  ├─ 从尾部反向找最后 N 条 user/assistant 消息（默认 2）
  ├─ 前面全部标记为压缩对象
  └─ 拼成一条大 user 消息 + 追加 compact.md 提示词

compact()
  ├─ 调用 LLM（单独 API 调用）
  │     system: "You are a helpful assistant that compacts conversation context."
  │     input:  prepare 构造的压缩消息
  │     output: 结构化 XML
  ├─ 用 "Previous context has been compacted..." 包装 LLM 输出
  └─ 拼上保留的最后 N 条消息
```

### LLM 输出的 XML 结构

```xml
<current_focus>[当前工作]</current_focus>
<environment>[关键配置]</environment>
<completed_tasks>[已完成任务]</completed_tasks>
<active_issues>[活跃问题]</active_issues>
<code_state>
  <file>[文件名 + 摘要 + 关键代码]</file>
</code_state>
<important_context>[其他重要信息]</important_context>
```

### 上下文管理

- 压缩后清除整个 context 文件（轮换备份到 `.1`, `.2`）
- 重写 system prompt
- 支持 checkpoint/rollback
- 新压缩结果写回后重新估算 token 数

### 错误处理

指数退避 + 抖动，最多 3 次重试。

### 集成到主循环

每步执行前检查 `should_auto_compact()`。

### 可观测性

- 发送 `CompactionBegin`/`CompactionEnd` 线路事件（UI）
- 触发 `PreCompact`/`PostCompact` 钩子事件

---

## 4. Claude Code

### 状态：多策略分层体系

Claude Code (TypeScript) 是目前工程最完善的实现——4 层策略按优先级从低到高排列，每层都有对应的最优场景。

### 核心文件

```
src/services/compact/
├── compact.ts                        # 主 LLM 压缩（61KB）
├── autoCompact.ts                    # 自动触发 + Session Memory 试错
├── microCompact.ts                   # 无 API 调用的工具输出清理
├── sessionMemoryCompact.ts           # 复用已有记忆文件
├── prompt.ts                         # 压缩提示词模板
├── timeBasedMCConfig.ts              # 时间基 microcompact 配置
├── postCompactCleanup.ts             # 压缩后清理
├── reactiveCompact.ts                # API 413 被动触发
├── grouping.ts                       # 消息按 API round 分组
├── cachedMicrocompact.ts             # cache_edits API 路径
├── apiMicrocompact.ts                # API 层 microcompact
└── compactWarningState.ts            # 压缩警告状态
```

### 架构总览

```
autoCompactIfNeeded()              ← 每次 LLM 调用前
  ├─ 1. trySessionMemoryCompaction()  ← 零额外成本
  │     复用已在后台提取的 Session Memory
  │
  ├─ 2. compactConversation()         ← LLM 摘要 + 附件恢复
  │     forkedAgent 共享主线程 cache
  │     PTL 时 truncateHeadForPTLRetry（最多 3 次）
  │     streaming 失败后重试（最多 2 次）
  │     circuit breaker（连续 3 次失败停）
  │
  └─ 3. microcompactMessages()        ← 不调 API，纯操作工具输出
       ├─ maybeTimeBasedMicrocompact()  冷缓存时清空工具结果
       └─ cachedMicrocompactPath()      cache_edits API 服务端删除
```

外部还有 Reactive Compact（API 413 时触发）作为最后防线。

---

### 策略 1: Session Memory Compaction

**目的**：复用已有的记忆文件作为摘要，零额外 API 调用。

```
trySessionMemoryCompaction()
  ├─ 1. 检查 feature flag (tengu_session_memory + tengu_sm_compact)
  ├─ 2. 等待后台 Session Memory 提取完成
  ├─ 3. 找到 lastSummarizedMessageId
  ├─ 4. calculateMessagesToKeepIndex()
  │    ├─ 从标记位往后保留
  │    ├─ 保证 minTokens=10K, minTextBlockMessages=5, maxTokens=40K
  │    ├─ adjustIndexToPreserveAPIInvariants()
  │    │    ├─ tool_use/tool_result 配对
  │    │    └─ thinking block 按 message.id 合并
  │    └─ 过滤旧 compact boundary
  ├─ 5. 用 session memory 文本做摘要
  ├─ 6. truncateSessionMemoryForCompact() 长 section 截断
  └─ 7. 检查 postCompactTokenCount < autoCompactThreshold
```

**配置** (GrowthBook 可调):
```typescript
const DEFAULT_SM_COMPACT_CONFIG = {
  minTokens: 10_000,
  minTextBlockMessages: 5,
  maxTokens: 40_000,
}
```

---

### 策略 2: Full LLM Compaction

**目的**：用 Claude API 生成结构化摘要，并恢复附件。

#### Prompt 设计 (`prompt.ts`)

```
NO_TOOLS_PREAMBLE:
  "Do NOT call any tools. Tool calls will be REJECTED."

BASE_COMPACT_PROMPT — 9 个章节:
  1. Primary Request and Intent
  2. Key Technical Concepts
  3. Files and Code Sections（含完整代码片段）
  4. Errors and fixes（含用户反馈）
  5. Problem Solving
  6. All user messages（逐条列出）
  7. Pending Tasks
  8. Current Work（精确的最后状态）
  9. Optional Next Step（含用户原话引用，防漂移）

输出: <analysis>（草稿，后被 strip）+ <summary>（9 章节）
```

两外两个变体：
- `PARTIAL_COMPACT_PROMPT` — 仅压缩最近消息
- `PARTIAL_COMPACT_UP_TO_PROMPT` — 压缩前面，保留后面，含 "Context for Continuing Work" 章节

#### 实现特性

```typescript
async function compactConversation(): Promise<CompactionResult> {
  // 1. PreCompact hooks（支持注入自定义指令）
  // 2. 用 forkedAgent 路径（共享主线程 cache）
  //    失败时 fallback 到普通 streaming
  // 3. PTL 重试：truncateHeadForPTLRetry（最多 3 次）
  // 4. Streaming 重试（最多 2 次，指数退避）
  // 5. stripImagesFromMessages（防止 compaction 也 PTL）
  // 6. 构造 CompactionResult:
  //    - boundaryMarker（标识压缩边界）
  //    - summaryMessages（1 条 user 消息）
  //    - attachments（文件/Skill/Plan/Agent/工具增量）
  //    - hookResults（SessionStart hooks）
  // 7. PostCompact hooks
  // 8. 日志埋点（pre/post tokens, cache 命中, 上下文分析）
}
```

#### 附件恢复

压缩后重建的关键上下文：

| 附件类型 | 来源 | 预算 |
|----------|------|------|
| 最近读过的文件 | `readFileState`（按时间倒序） | 最多 5 个, 50K tokens |
| 每个文件上限 | 5K tokens | — |
| 已调用的 Skill | `getInvokedSkillsForAgent` | 25K tokens, 每个 5K |
| Plan 文件 | `getPlan()` | — |
| Plan mode 指令 | `appState.toolPermissionContext.mode` | — |
| 异步 Agent 状态 | `appState.tasks` | — |
| Deferred tools 增量 | `getDeferredToolsDeltaAttachment` | — |
| Agent listing 增量 | `getAgentListingDeltaAttachment` | — |
| MCP 指令增量 | `getMcpInstructionsDeltaAttachment` | — |

#### 边界安全

```typescript
// adjustIndexToPreserveAPIInvariants()
// 保证保留范围内的消息不违反 API 约束:
// 1. tool_use/tool_result 不能拆散
// 2. thinking block 和 tool_use 共享 message.id 时不能拆散
```

#### Partial Compact

两种方向：
- `'from'`（前缀保留）：压缩 pivot 之后的消息，保留之前的。前缀的 cache 命中不受影响。
- `'up_to'`（后缀保留）：压缩 pivot 之前的消息，保留之后的。cache 被突破。

---

### 策略 3: Microcompact

**目的**：不调用 LLM，通过操作工具输出来释放 token 空间。

#### 3a. Time-based Microcompact

```
条件：距离上次 assistant 超过 gapThresholdMinutes（通常数分钟）
效果：服务器缓存已冷，缩小 payload 无额外损失

步骤：
  1. collectCompactableToolIds() 收集可压缩工具调用
  2. 保留最近 keepRecent 个，前面的替换为 [Old tool result content cleared]
  3. resetMicrocompactState() + 通知 cache break detection
```

```typescript
const COMPACTABLE_TOOLS = new Set([
  FILE_READ_TOOL_NAME,        // Read
  ...SHELL_TOOL_NAMES,        // Bash
  GREP_TOOL_NAME,             // Grep
  GLOB_TOOL_NAME,             // Glob
  WEB_SEARCH_TOOL_NAME,       // WebSearch
  WEB_FETCH_TOOL_NAME,        // WebFetch
  FILE_EDIT_TOOL_NAME,        // FileEdit
  FILE_WRITE_TOOL_NAME,       // FileWrite
])
```

#### 3b. Cached Microcompact

利用 Anthropic API 的 `cache_edits` 机制实现**服务端删除**过时的 tool 结果。

```typescript
async function cachedMicrocompactPath(): Promise<MicrocompactResult> {
  // 1. 遍历消息，注册 tool_result 到 cachedMCState
  // 2. 触发阈值时，createCacheEditsBlock() 生成 cache_edits
  // 3. 本地消息数组不受影响
  // 4. 下一个 API 请求时，服务端缓存上删除对应的 tool_results
  // 5. Prompt cache 命中不受影响
}
```

**关键**：这是唯一不修改本地消息的压缩方式。删除发生在 API 层。

---

### 策略 4: Reactive Compact

当 API 返回 413 (prompt_too_long) 时被动触发。作为其他策略都无效时的最后防线。

---

### 自动触发与状态管理

```typescript
export function getAutoCompactThreshold(model: string): number {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  return effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS  // 减 13K
}
```

状态管理：
- `AutoCompactTrackingState`: compacted / turnCounter / turnId / consecutiveFailures
- Circuit breaker: 连续 3 次失败后停止尝试（防止永久死循环）
- 禁用手动开关: `DISABLE_COMPACT` / `DISABLE_AUTO_COMPACT` 环境变量 + `autoCompactEnabled` 配置
- Re-compaction 追踪: `RecompactionInfo`（isRecompactionInChain / turnsSincePreviousCompact / autoCompactThreshold）

---

## 5. Codex

### 状态：LLM 驱动 + 远程/本地双路径

Codex (Rust) 的压缩系统采用 LLM 驱动的"Memento"策略，支持本地和远程两种实现路径。Compaction 被设计为一个与 RegularTask 并列的独立 `SessionTask`。

### 核心文件

- `codex-rs/core/src/compact.rs` — 本地内联压缩主逻辑（~586 行）
- `codex-rs/core/src/compact_remote.rs` — 远程压缩路径
- `codex-rs/core/src/tasks/compact.rs` — CompactTask 任务分发器
- `codex-rs/core/templates/compact/prompt.md` — 压缩提示模板
- `codex-rs/core/templates/compact/summary_prefix.md` — 摘要前缀
- `codex-rs/core/src/context_manager/history.rs` — ContextManager 历史管理
- `codex-rs/core/src/session/turn.rs` — `run_turn()` 中集成的压缩触发点
- `codex-rs/codex-api/src/endpoint/compact.rs` — 远程压缩 API 端点

### 架构总览

```
CompactTask::run()
  ├─ should_use_remote_compact_task()?  ← 根据 provider 决定路径
  │
  ├─ [远程路径] run_remote_compact_task()
  │     └─ 调用服务端 API 执行压缩
  │
  └─ [本地路径] run_compact_task()
        ├─ 1. 发送 TurnStarted 事件
        ├─ 2. run_compact_task_inner()
        │    ├─ a. 发出 ContextCompactionItem
        │    ├─ b. 构建 prompt = compact_prompt + history
        │    ├─ c. drain_to_completed() — 调用 LLM（Responses API）
        │    │     └─ 重试循环: ContextWindowExceeded → remove_first_item()
        │    │                   stream error → backoff 重试（最多 max_retries）
        │    │                   Interrupted → 立即返回
        │    ├─ d. 从 LLM 输出提取 summary_text
        │    ├─ e. 收集 user_messages（去重，排除摘要消息）
        │    ├─ f. build_compacted_history() → 重建为摘要 + 精选用户消息
        │    ├─ g. 根据 initial_context_injection 决定是否注入 initial context
        │    └─ h. replace_compacted_history() + recompute_token_usage()
        ├─ 3. 发出 Warning（提示长期线程可能降低精度）
        └─ 4. CompactionAnalyticsAttempt 追踪
```

### 触发条件

两处触发点，都在 `run_turn()` 中：

**1. Pre-Sampling Compact（采样前）：**
```
total_usage_tokens >= auto_compact_limit
→ InitialContextInjection::DoNotInject（下一轮重新注入）
→ CompactionPhase::PreTurn
```

**2. Mid-Turn Compact（轮中）：**
```
token_limit_reached && needs_follow_up
→ InitialContextInjection::BeforeLastUserMessage（注入到摘要前）
→ CompactionPhase::MidTurn
```

额外触发：**Model Downshift** — 当切换到更小上下文窗口的模型时，用前一个模型做压缩。

### 压缩提示模板

```
prompt.md:
"You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary
 for another LLM that will resume the task.
 Include:
 - Current progress and key decisions made
 - Important context, constraints, or user preferences
 - What remains to be done (clear next steps)
 - Any critical data, examples, or references needed to continue"

summary_prefix.md:
"Another language model started to solve this problem and produced a summary
 of its thinking process. You also have access to the state of the tools that
 were used by that language model. Use this to build on the work that has
 already been done and avoid duplicating work. Here is the summary..."
```

### 压缩后的历史结构

```rust
fn build_compacted_history(initial_context, user_messages, summary_text):
    // 1. 精选用户消息（从后往前选，最多 20K tokens）
    // 2. 追加为 user-role ResponseItem::Message
    // 3. 追加压缩摘要作为最后一条 user-role 消息
    // → 返回 Vec<ResponseItem>
```

### InitialContextInjection 策略

```rust
enum InitialContextInjection {
    BeforeLastUserMessage,  // Mid-turn：注入到最后一个真实用户消息前
    DoNotInject,            // Pre-turn/manual：不注入，下一轮重新全量注入
}
```

注入时按优先级找插入点：最后一个真实用户消息 > 最后一个用户类消息(含摘要) > 最后一个 Compaction item > 末尾追加。

### 远程压缩

当 `provider.supports_remote_compaction()` 返回 true 时，压缩任务完全卸载到服务端。本地只做触发和结果处理。

### 关键特性

- **双路径** — 本地 LLM vs 远程服务端，根据 provider 能力自动选择
- **保留用户消息** — 在压缩后的历史中保留精选的用户消息（最多 20K tokens），不只是摘要
- **上下文重注入** — 压缩后根据阶段决定是否立即注入 initial context
- **逐步裁剪** — 遇到 ContextWindowExceeded 时从历史头部逐步移除旧消息
- **可观测性** — CompactionAnalyticsAttempt 追踪前后 token 数、耗时、状态
- **重压缩防护** — 用户消息收集时过滤已有的摘要消息（is_summary_message）

---

## 6. Agenda

### 状态：从 Kimi CLI 直接移植

Agenda 的 `compaction.py` 基本上是 Kimi CLI 的 `SimpleCompaction` 的直接移植。

### 核心文件

- `agenda/compaction.py` — SimpleCompaction（180 行）
- `agenda/agent.py` — `_compact_context()` 集成
- `agenda/prompts/compact.md` — 压缩提示模板

### 与 Kimi CLI 的差异

| 维度 | Kimi CLI | Agenda |
|------|----------|--------|
| 压缩策略 | `SimpleCompaction(max_preserved_messages=2)` | 完全相同 |
| 触发条件 | 85% + 50K 保留 | **75% + 2048 保留**（更早触发） |
| 重试机制 | 指数退避 + 抖动，最多 3 次 | **无重试** |
| 集成深度 | 主循环每步前自动检查 | 同，在轮前检查 |
| 后处理 | 清除 context + 重建 checkpoint | **rotate_turns + clear_turns** |
| 事件/钩子 | CompactionBegin/End + Pre/Post 钩子 | **无** |
| 自定义指令 | 支持 `/compact 保留数据库讨论` | **无** |
| token 更新 | 压缩后根据 LLM usage 更新 | **无** |
| 错误处理 | 3 次重试 + CompactionError | 直接向上抛 |

### 触发条件对比

```python
# Agenda（更保守）
DEFAULT_COMPACTION_TRIGGER_RATIO = 0.75
DEFAULT_COMPACTION_RESERVED = 2048

# Kimi CLI（更晚触发）
DEFAULT_COMPACTION_TRIGGER_RATIO = 0.85
DEFAULT_COMPACTION_RESERVED = 50_000
```

---

## 7. 横向对比

### 核心策略对比

| 维度 | Butterfly | Claw Code | Kimi CLI | Claude Code | Codex | Agenda |
|------|-----------|-----------|----------|-------------|-------|--------|
| **语言** | Python | Rust | Python | TypeScript | Rust | Python |
| **压缩方法** | 标记替换 | 启发式提取 | LLM 摘要 | **4 层混合** | LLM 摘要 | LLM 摘要 |
| **使用 LLM?** | ❌ | ❌ | ✅ | **可选** (SM/MC 不调) | ✅ | ✅ |
| **额外 token 成本** | 无 | 无 | 有 | **有/无 双路径** | 有 | 有 |
| **保留消息数** | 无 | 4 | 2 | **动态计算** | 2 + 精选用户消息 | 2 |
| **触发阈值** | 无 | 10K tokens | 85% + 50K | **contextWindow - 13K** | auto_compact_limit | 75% + 2K |
| **远程压缩** | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |

### 工程特性对比

| 特性 | Butterfly | Claw Code | Kimi CLI | Claude Code | Codex | Agenda |
|------|-----------|-----------|----------|-------------|-------|--------|
| 🌳 **Tool 边界安全** | ❌ | ✅ | ❌ | **✅ 最完善** | ✅ (normalize) | ❌ |
| 🔄 **重试机制** | ❌ | ❌ | 指数退避 3 次 | **PTL 3 + streaming 2 + 断路器** | 指数退避 + 逐步裁剪 | ❌ |
| 💰 **缓存利用** | ❌ | ❌ | ❌ | **forkedAgent cache 共享 + cache_edits API** | ❌ | ❌ |
| 📎 **附件恢复** | ❌ | ❌ | ❌ | **文件/Skill/Plan/Agent/工具增量** | 精选用户消息重放 | ❌ |
| 📊 **可观测性** | ❌ | ❌ | 线路事件 | **10+ 维度埋点** | Analytics Attempt (token 前后对比) | ❌ |
| 🎯 **自定义指令** | ❌ | ❌ | `/compact 保留...` | **PreCompact hook + 用户输入** | compact_prompt 配置覆盖 | ❌ |
| 🧩 **Partial compact** | ❌ | ❌ | ❌ | **两种方向 (from/up_to)** | ❌ | ❌ |
| 🏗 **二级压缩** | ❌ | ✅ 行优先级 | ❌ | **SM truncation + MC 清空** | ❌ | ❌ |
| 🛡 **重压缩防护** | ❌ | ✅ 合并摘要 | ❌ | **断路 + PTLA** | **is_summary_message 过滤** | ❌ |
| 📝 **后处理** | ❌ | ❌ | checkpoint 重建 | **附件恢复 + hooks** | replace_compacted_history + token 重算 | rotate 文件 |
| ☁️ **远程压缩** | ❌ | ❌ | ❌ | ❌ | **✅ 双路径** | ❌ |

### 代码量对比

| 项目 | 压缩相关代码量 | 语言 |
|------|---------------|------|
| Butterfly | ~100 行 (标记替换) | Python |
| Claw Code | ~600 行 (compact.rs + summary_compression.rs) | Rust |
| Kimi CLI | ~400 行 (compaction.py + compact.md) | Python |
| Claude Code | **~2000 行** (compact/ 目录 18 个文件) | TypeScript |
| Codex | ~1000 行 (compact.rs + compact_remote.rs + tasks/compact.rs + templates) | Rust |
| Agenda | ~200 行 (compaction.py + compact.md) | Python |

---

## 8. 总结与建议

### 各方案定位

| 方案 | 适合场景 | 原因 |
|------|---------|------|
| **Butterfly** | 有记忆系统、不需要压缩 | 靠 on-demand + 标记替换 |
| **Claw Code** | 重视成本、可接受信息损失 | 零成本启发式 |
| **Kimi CLI** | 有限 LLM 调用、需要高质量摘要 | LLM 驱动 + 结构化输出 |
| **Claude Code** | 生产级、多场景覆盖 | 4 层策略 + 完整工程防护 |
| **Codex** | 远程/本地双环境、大规模部署 | 双路径 + 精选用户消息保留 |
| **Agenda** | 极简核心、快速验证 | Kimi 子集 |

### Agenda 可改进的方向

基于本次调研，Agenda 的 `SimpleCompaction` 有以下可改进点：

1. **Tool 边界安全** — Claw Code / Claude Code / Codex 都有的功能，防止拆分 tool_use/tool_result 对
2. **重试机制** — Kimi CLI 有的指数退避重试，Codex 有完善的 ContextWindowExceeded 逐步裁剪重试，Agenda 缺失
3. **重压缩合并** — 多次压缩时旧摘要不丢失（Claw Code 的 merge + Claude Code 的 RecompactionInfo + Codex 的 is_summary_message 过滤）
4. **估算改进** — Agenda 和 Kimi 都用的 `len//4` 启发式，对中文偏差大；Codex 用 `approx_token_count` 基于字节的启发式
5. **自定义指令** — Kimi CLI 和 Claude Code 都支持，Codex 支持 compact_prompt 配置覆盖，Agenda 缺
6. **可观测性** — 至少可以加压缩前后 token 数的日志（Codex 的 CompactionAnalyticsAttempt 模式）
7. **精选用户消息保留** — Codex 在压缩后保留精选用户消息（最多 20K tokens），而非仅摘要，这提供了更多的上下文连续性

### 长远方向

Session Memory Compaction（Claude Code 的策略 1）是理论上最优方案——独立于压缩之外的记忆系统持续提取关键信息，压缩时直接复用。这避免了压缩时的额外 LLM 调用，同时记忆系统本身也是其他场景的有用基础设施。但实现成本高，适合项目成熟期考虑。

Codex 的远程压缩（将压缩卸载到服务端）和精选用户消息保留策略也是值得参考的方向——前者降低了客户端资源消耗，后者在压缩后提供了比纯摘要更丰富的上下文。
