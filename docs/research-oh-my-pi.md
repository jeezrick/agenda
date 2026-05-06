# oh-my-pi 调研报告

> 调研对象：[can1357/oh-my-pi](https://github.com/can1357/oh-my-pi)  
> Fork 自 [badlogic/pi-mono](https://github.com/badlogic/pi-mono) by @mariozechner  
> 调研目的：为 Agenda 的后续迭代提供设计参考，特别是 Subagent、Compaction、Hooks、TTSR 等方向

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构分析](#2-架构分析)
3. [Agent Loop 与 Session 管理](#3-agent-loop-与-session-管理)
4. [Context Compaction](#4-context-compaction)
5. [Task / Subagent 系统](#5-task--subagent-系统)
6. [Hooks 与扩展系统](#6-hooks-与扩展系统)
7. [TTSR：时间旅行流规则](#7-ttsr时间旅行流规则)
8. [Hashline Edits](#8-hashline-edits)
9. [其他值得关注的特性](#9-其他值得关注的特性)
10. [对 Agenda 的借鉴意义](#10-对-agenda-的借鉴意义)

---

## 1. 项目概述

**oh-my-pi** 是一个 AI coding agent for the terminal，定位与 Claude Code、Kimi CLI 类似，但功能覆盖更广、架构更复杂。核心特点：

- **Monorepo**：TypeScript（Bun runtime）+ Rust N-API 混合架构
- **TUI**：自研差分渲染终端 UI（`packages/tui`）
- **多模型支持**：40+ provider，支持 role-based model routing
- **丰富的工具生态**：LSP、Browser、Python REPL、SSH、Web Search、MCP 等
- **Subagent 系统**：6 个内置 agent + 自定义 agent 支持
- **Session 树结构**：支持 branching、forking、compaction

与 Agenda 的关键差异：oh-my-pi 是**人机交互终端**（TUI + 实时流式输出），Agenda 是**Agent-facing 运行时**（无 UI，文件系统即接口）。但两者在 Agent Loop、Compaction、Subagent、Hooks 等底层机制上有大量可借鉴之处。

---

## 2. 架构分析

### 2.1 Monorepo 结构

```
packages/
  ai/              # 多 provider LLM client（streaming、token counting）
  agent/           # Agent 运行时核心（tool calling、state machine）
  coding-agent/    # 主 CLI 应用（TUI、模式控制、session 管理）
  tui/             # 终端 UI 引擎（差分渲染、focus 路由）
  natives/         # Rust N-API 绑定层
  utils/           # 共享工具（logger、streams、temp files）
  swarm-extension/ # 多 agent swarm 扩展
crates/
  pi-natives/      # ~7,500 行 Rust，性能关键操作
```

### 2.2 Rust Native Engine（N-API）

~7,500 行 Rust 编译为平台标记的 N-API addon，提供性能关键操作：

| 模块 | 行数 | 功能 | 底层库 |
|------|------|------|--------|
| grep | ~1,300 | 并行/串行正则搜索、glob 过滤、模糊查找 | ripgrep internals |
| shell | ~1,025 | 嵌入式 bash 执行（持久 session、流式输出） | brush-shell |
| text | ~1,280 | ANSI-aware 文本宽度、截断、换行 | unicode-width |
| keys | ~1,300 | Kitty keyboard protocol 解析 | phf |
| highlight | ~475 | 语法高亮（30+ 语言） | syntect |
| glob | ~340 | 文件系统发现（.gitignore 尊重） | ignore/globset |
| task | ~350 | 阻塞工作调度（libuv thread pool） | tokio/napi |
| ps | ~290 | 跨平台进程树 kill | libc/libproc |
| prof | ~250 | 火焰图生成 | inferno |
| image | ~150 | PNG/JPEG/WebP/GIF 编解码 | image |
| clipboard | ~95 | 系统剪贴板 | arboard |
| html | ~50 | HTML-to-Markdown | html-to-markdown-rs |

**对 Agenda 的启示**：Agenda 目前纯 Python 实现，工具操作（grep、shell、文件遍历）依赖外部命令。当性能成为瓶颈时，可以考虑类似的 Rust N-API 方案。

---

## 3. Agent Loop 与 Session 管理

### 3.1 Session 存储模型

Session 文件是 **JSONL**，格式与 Agenda 完全一致：

```
~/.omp/agent/sessions/--<cwd-encoded>--/<timestamp>_<sessionId>.jsonl
```

- Line 1: Session Header（`type: "session"`）
- 其余: SessionEntry（append-only）

**关键差异**：oh-my-pi 的 session 是**树结构**（`id`/`parentId` + leaf pointer），支持 branching 和 forking。Agenda 的 session 是线性结构（`turns.jsonl`），目前不支持分支。

### 3.2 Entry 类型

oh-my-pi 定义了丰富的 entry 类型：

| 类型 | 说明 |
|------|------|
| `message` | 用户/助手消息 |
| `compaction` | 压缩摘要 |
| `branch_summary` | 分支导航摘要 |
| `custom_message` | 自定义消息 |
| `tool_call` | 工具调用 |
| `tool_result` | 工具结果 |

Agenda 目前只有 `turn` 和 `event`，可以借鉴 `compaction` 和 `branch_summary` 的类型设计。

### 3.3 Context 重建

`buildSessionContext()` 负责从 session entries 重建 LLM 输入：

1. 找到 active path 上最新的 `compaction` entry，转换为 `compactionSummary` message
2. 从 `firstKeptEntryId` 到 compaction point 的 entries 重新包含
3. 后续 entries append
4. `branch_summary` entries 转换为 `branchSummary` messages

这个设计与 Agenda 的 compaction 2.0（保留 4 条消息 + 压缩摘要）思路一致，但 oh-my-pi 更精细（支持 `firstKeptEntryId` 控制保留边界）。

---

## 4. Context Compaction

oh-my-pi 的 compaction 是**最复杂的 compaction 系统之一**，与 Agenda 的 compaction 2.0 形成鲜明对比。

### 4.1 触发机制（四种）

| 触发方式 | 说明 |
|----------|------|
| 手动 | `/compact [focus]` |
| 溢出恢复 | 模型返回 context overflow 错误后自动 compact + retry |
| 阈值维护 | 成功 turn 后 context 超过配置阈值 |
| 空闲维护 | `runIdleCompaction()` 后台执行 |

Agenda 目前只有**阈值触发**（token_count > trigger_ratio * token_cap），可以借鉴**溢出恢复**和**空闲维护**。

### 4.2 Compaction 流水线

```
Before:
  entry:  0     1     2     3      4     5     6      7      8     9
        ┌─────┬─────┬─────┬──────┬─────┬─────┬──────┬──────┬─────┬──────┐
        │ hdr │ usr │ ass │ tool │ usr │ ass │ tool │ tool │ ass │ tool │
        └─────┴─────┴─────┴──────┴─────┴─────┴──────┴──────┴─────┴──────┘
                └────────┬───────┘ └──────────────┬──────────────┘
               messagesToSummarize            kept messages
                                   ↑
                          firstKeptEntryId (entry 4)

After:
  entry:  0     1     2     3      4     5     6      7      8     9      10
        ┌─────┬─────┬─────┬──────┬─────┬─────┬──────┬──────┬─────┬──────┬─────┐
        │ hdr │ usr │ ass │ tool │ usr │ ass │ tool │ tool │ ass │ tool │ cmp │
        └─────┴─────┴─────┴──────┴─────┴─────┴──────┴─────┴─────┴──────┴─────┘
```

### 4.3 Compaction 产物结构

```ts
interface CompactionEntry {
  type: "compaction";
  summary: string;
  shortSummary?: string;
  firstKeptEntryId: string;  // 保留边界
  tokensBefore: number;
  details?: CompactionDetails;  // 文件操作追踪
  preserveData?: unknown;
  fromExtension?: boolean;
}
```

**关键创新**：
- **文件操作追踪**（`details.readFiles` / `details.modifiedFiles`）：compaction 时追踪哪些文件被读写，便于后续上下文重建
- **Short Summary**：双级摘要（详细摘要 + 一句话摘要），short summary 用于系统提示注入

### 4.4 与 Agenda Compaction 2.0 对比

| 维度 | oh-my-pi | Agenda |
|------|----------|--------|
| 触发 | 4 种（手动/溢出/阈值/空闲） | 1 种（阈值） |
| 摘要级别 | 双级（summary + shortSummary） | 单级 |
| 保留边界 | `firstKeptEntryId` 精确控制 | 固定保留 4 条消息 |
| 文件追踪 | 读写文件列表 | ❌ 无 |
| 验证 | 通过 token count 隐式验证 | 显式 `validate_compacted()` |
| 回退 | 无显式回退（依赖 LLM） | 截断回退（`truncate_messages`） |
| 专用模型 | ❌ 未提及 | ✅ `compact_model` |

**建议**：Agenda 可以借鉴 oh-my-pi 的**溢出恢复触发**和**文件操作追踪**，但 Agenda 的**截断回退**和**专用压缩模型**是 oh-my-pi 没有的，应保留。

---

## 5. Task / Subagent 系统

oh-my-pi 的 Task 工具是一个**完整的 subagent 执行框架**，与 Agenda 的递归 DAG 有相似之处。

### 5.1 Agent 定义

Agent 从 Markdown frontmatter 定义：

```yaml
---
name: explore
description: Explore a codebase to understand structure
tools: read_file, grep, glob
spawns: "*"
model: pi/smol
---
```

- `tools`: 该 agent 可用的工具列表
- `spawns`: 该 agent 可以 spawn 的其他 agent（`*` 表示全部）
- `model`: 该 agent 使用的模型

### 5.2 内置 Agent

| Agent | 用途 |
|-------|------|
| `explore` | 代码库探索 |
| `plan` | 架构规划 |
| `designer` | UI/设计 |
| `reviewer` | 代码审查 |
| `task` | 通用任务执行 |
| `quick_task` | 轻量任务 |

### 5.3 执行模式

1. **In-process**: 主线程执行，转发 AgentEvent 用于进度追踪
2. **Isolated**: 隔离执行，支持三种后端：
   - `worktree`: git worktree 隔离
   - `fuse-overlay`: Unix fuse-overlay 文件系统隔离
   - `fuse-projfs`: Windows ProjFS 隔离

3. **Async background**: 后台执行，支持最多 100 个并发 job，`poll` 工具阻塞等待结果

### 5.4 与 Agenda 递归 DAG 对比

| 维度 | oh-my-pi Task | Agenda `agenda()` |
|------|---------------|-------------------|
| 调度单位 | Agent 定义（frontmatter） | DAG 节点（YAML） |
| 隔离 | worktree/fuse-overlay/ProjFS | 目录隔离（Session） |
| 并发 | 最多 100 async jobs | `max_parallel` 并行节点 |
| 结果回传 | `agent://<id>` 资源 URL | `output/draft.md` + state |
| 深度限制 | `MAX_SUB_AGENT_DEPTH` | `MAX_SUB_AGENT_DEPTH` |
| 模型选择 | per-agent model override | per-node model |
| 产物追踪 | 实时 artifact streaming | 节点完成后读取 |

**关键差异**：oh-my-pi 的 subagent 是**对话式**（spawn → stream → poll），Agenda 的 subagent 是**DAG 节点式**（调度 → 执行 → 状态机）。两者可以互补：Agenda 的 DAG 调度能力 + oh-my-pi 的 agent 定义和隔离机制。

---

## 6. Hooks 与扩展系统

### 6.1 Hook 设计

Hook 是 TypeScript 模块，default-export 一个工厂函数：

```ts
import type { HookAPI } from "@oh-my-pi/pi-coding-agent/hooks";

export default function (pi: HookAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "bash" && /sudo/.test(event.input.command)) {
      const ok = await ctx.ui.confirm("Allow sudo?", event.input.command);
      if (!ok) return { block: true, reason: "Blocked by user" };
    }
  });
}
```

### 6.2 Hook 事件类型

| 事件 | 说明 |
|------|------|
| `tool_call` | 工具调用前拦截 |
| `tool_result` | 工具结果后处理 |
| `message_start` | 消息开始 |
| `message_update` | 消息更新（streaming） |
| `message_end` | 消息结束 |

### 6.3 与 Agenda HookRegistry 对比

| 维度 | oh-my-pi Hooks | Agenda HookRegistry |
|------|----------------|---------------------|
| 语言 | TypeScript | Python |
| 注册方式 | 模块文件（`~/.omp/hooks/*.ts`） | 代码 `register(event, fn)` |
| 事件类型 | 10+ 种（tool/session/message） | 6 种（node/turn/tool/compaction） |
| 拦截能力 | ✅ 可 block 工具调用 | ❌ 只观察，不拦截 |
| UI 交互 | ✅ 可弹出 confirm/dialog | ❌ 无 UI |
| 异步 | ✅ async/await | ✅ async/await |

**建议**：Agenda 的 HookRegistry 目前只有**观察**能力，可以借鉴 oh-my-pi 的**拦截**能力（如审批门）。

---

## 7. TTSR：时间旅行流规则

**TTSR（Time Traveling Streamed Rules）** 是 oh-my-pi 最独特的设计之一。

### 7.1 核心思想

传统规则系统：所有规则一次性注入 system prompt，消耗固定上下文。

TTSR：规则定义 regex trigger，**实时监视模型输出流**。当输出匹配 trigger 时：

1. **流被中断**
2. **规则注入为 system reminder**
3. **请求重试**
4. **One-shot**：每条规则每 session 只触发一次，防止循环

### 7.2 示例

```yaml
---
name: no-deprecated-api
description: Don't use deprecated APIs
condition: ["deprecated", "legacy", "obsolete"]  # regex triggers
---
```

当模型开始写 `deprecated` 相关代码时，流被中断，规则注入，模型重试。

### 7.3 对 Agenda 的启示

TTSR 的核心 insight：**上下文不是静态的，可以按需注入**。Agenda 目前的规则（DAG YAML 中的约束）是静态的，可以考虑：

- **动态规则注入**：根据运行时状态（如 token count、节点进度）动态调整 system prompt
- **流式监控**：在 streaming 模式下监控输出，发现问题时实时干预

---

## 8. 多模式编辑系统（Hashline DSL + Patch + Replace）

oh-my-pi 的文件编辑是其最创新的子系统之一。它不只是"一个编辑工具"，而是一套**分级编辑体系**——三种模式各有明确的适用场景，模型被引导优先使用最精确的模式。

### 8.1 三种编辑模式

| 模式 | 工具名 | 定位 | 适用场景 |
|------|--------|------|----------|
| **Hashline** | `edit` | 默认主编辑工具 | 多行编辑、精确锚定、已验证文件 |
| **Patch** | `patch` | 高级编辑 | 复杂重构、需要 diff 格式的场景 |
| **Replace** | `replace` | 回退方案 | 文件未以 hashline 模式读取时的兜底 |

每种模式有独立的 prompt 描述（`packages/coding-agent/src/prompts/tools/`），引导模型根据场景选择合适的工具。

### 8.2 Hashline DSL：正式的领域特定语言

Hashline 不是一个简单的锚点格式——它是一套有 formal Lark grammar（`hashline.lark`）的 DSL。

**语法（EBNF）**：
```lark
start: section+
section: file_header line_op*

file_header: "@" path LF

line_op: inline_before_op payload*
       | inline_after_op payload*
       | insert_before_op payload+
       | insert_after_op payload+
       | replace_op payload*
       | delete_op

inline_before_op: "<" LID HSEP line_text? LF     # 内联：前置文本
inline_after_op:  "+" LID HSEP line_text? LF     # 内联：后置文本
insert_before_op: "<" insert_target LF
insert_after_op:  "+" insert_target LF
replace_op: "=" range LF
delete_op: "-" range LF
payload: HSEP line_text? LF                      # 以 | 为前缀的负载行

insert_target: LID | "EOF" | "BOF"
range: LID (".." LID)?
LID: /[1-9][0-9]*HFMT/                           # 行号+哈希，如 160ab
```

**六种操作码**：

| 操作码 | 语法 | 说明 |
|--------|------|------|
| 行前插入 | `< 160ab` + `\|payload` | 在锚点行之前插入 |
| 行后插入 | `+ 160ab` + `\|payload` | 在锚点行之后插入 |
| 内联前置 | `< 160ab\|text` | 在锚点行前面追加文本（单行修改） |
| 内联后置 | `+ 160ab\|text` | 在锚点行后面追加文本（单行修改） |
| 删除范围 | `- 320cd..325ef` | 删除从行 320 到行 325 的范围 |
| 替换范围 | `= 480gh..490jk` + `\|payload` | 用负载替换整个范围 |

**完整编辑示例**：
```
@src/main.py
+ 160ab
|def new_function():
|    return process(data)

- 320cd..325ef

= 480gh..490jk
|def rewritten():
|    return True
```

### 8.3 四个关键机制

**机制 1：分块流式格式化**（`streamHashLinesFromUtf8`）

读取大文件时，hashline 前缀以块为单位输出（默认：200 行或 64 KiB）。这防止 5000 行文件淹没模型的上下文窗口。

**机制 2：自动去除显示前缀**（`write.ts:66-76`）

当模型从 `read_file` 输出中复制文本并在 `write_file` 中使用时，如果内容包含 hashline 前缀（如 `160ab|def process():`），工具自动去除它们。模型可以安全地复制/粘贴读取的内容，无需手动清理。

```
模型读取:  160ab|def process():
模型写入:  def process():          ← 自动去除
```

**机制 3：锚点重新定位**（`ANCHOR_REBASE_WINDOW = 5`）

如果文件在读取和编辑之间发生变化（锚点行被移动），系统在 5 行容差窗口内搜索匹配行。如果找到，透明地重新定位锚点，避免不必要的编辑失败。

**机制 4：紧凑差异预览**（`buildCompactHashlineDiffPreview`）

编辑应用前生成紧凑差异预览，模型可以看到实际效果。这减少了"编辑后不符合预期"的问题。

### 8.4 基准测试数据

16 个模型，180 个任务，3 次运行：

| 模型 | str_replace | Hashline |
|------|-------------|----------|
| Grok Code Fast 1 | 6.7% | 68.3% |
| Gemini 3 Flash | 基准 | +5pp |
| Grok 4 Fast | 基准 | -61% tokens |

Grok Code Fast 1 从 6.7% 提升到 68.3%（10x），这证明了锚点编辑的精确性对"不是最强但更便宜"的模型尤其关键。

### 8.5 与 Agenda 对比

| 维度 | oh-my-pi | Agenda |
|------|----------|--------|
| 编辑工具数 | 3 种（edit/patch/replace） | 1 种（write_file） |
| 锚点机制 | Hashline DSL（哈希 + 行号） | 无（全文件写入） |
| 正式语法 | ✅ Lark grammar | ❌ |
| 分块格式化 | ✅ 200 行/64 KiB 分块 | ❌ |
| 锚点重定位 | ✅ 5 行窗口内重定位 | N/A |
| 自动去除前缀 | ✅ write 工具自动去除 | N/A |
| AST 编辑 | ✅ `ast_edit` 工具 | ❌ |
| 差异预览 | ✅ | ❌ |

### 8.6 对 Agenda 的启示

Agenda 的全文件 `write_file` 策略在短文件场景下完全够用，但长文件和多文件编辑场景下存在明显劣势：

1. **引入 Hashline 或简化版**：至少添加行号锚点 + 内容哈希，让 Agent 可以精准编辑单行而非重写整个文件
2. **分级编辑策略**：像 oh-my-pi 一样提供多个工具，优先引导使用精确模式
3. **自动去除前缀**：如果 Agent 复制了带前缀的读取内容，自动清理——这是一个低成本、高体验的设计
4. **锚点重定位**：在容差窗口内搜索移动的行——容忍并发文件变更

---

## 9. 其他值得关注的特性

### 9.1 LSP 集成

11 种 LSP 操作（diagnostics、definition、hover、rename 等），40+ 语言开箱支持。Format-on-write 和 diagnostics-on-write 提供即时反馈。

Agenda 目前无 LSP 支持。对于代码生成场景，LSP 可以显著提升 Agent 的代码质量。

### 9.2 Python REPL

持久 IPython kernel，支持 streaming output、rich display（HTML/Markdown/图片）、Mermaid 图表渲染。

Agenda 目前无 REPL 支持。Python 代码执行是 Agent 场景的高频需求。

### 9.3 通用配置发现

从 8 个 AI coding 工具（Claude Code、Cursor、Windsurf、Gemini、Codex、Cline、Copilot、VS Code）自动发现配置：

- MCP servers、rules、skills、hooks、tools、slash commands、prompts
- 原生格式支持：Cursor MDC、Windsurf rules、Cline `.clinerules`

Agenda 目前只读取 `.omp/` 目录。可以考虑支持多工具配置发现，降低用户迁移成本。

### 9.4 Model Roles

5 个预定义角色：`default`、`smol`、`slow`、`plan`、`commit`。每个角色可配置不同模型，支持 CLI 参数和环境变量覆盖。

Agenda 目前只有 per-node model 配置。可以借鉴 role-based routing（如 `compact_model` 已经是类似概念）。

### 9.5 Retry 策略

非 compaction 错误的重试策略：

- 分类：transient transport、rate limit、server error（500/502/503/504）、network failure
- 指数退避：base delay + retry-after header 尊重
- fallback chain：配置角色级 fallback 模型链
- cooldown revert：fallback 模型冷却期满后自动切回主模型

Agenda 目前有简单的 fallback（`fallback_model`），但无 retry 策略和 fallback chain。

---

## 10. 对 Agenda 的借鉴意义

### 10.1 短期可借鉴（低侵入性）

| 特性 | 优先级 | 实现成本 | 价值 |
|------|--------|----------|------|
| **溢出恢复触发 compaction** | P1 | 低 | 高 — 自动处理 context overflow |
| **Hook 拦截能力** | P1 | 低 | 高 — 审批门、策略拦截 |
| **Retry 策略 + fallback chain** | P2 | 中 | 中 — 提升可靠性 |
| **文件操作追踪** | P2 | 低 | 中 — compaction 时保留文件上下文 |

### 10.2 中期可借鉴（中等侵入性）

| 特性 | 优先级 | 实现成本 | 价值 |
|------|--------|----------|------|
| **Session 树结构** | P2 | 高 | 高 — 支持 branching/forking |
| **TTSR 动态规则** | P3 | 高 | 高 — 零成本上下文规则 |
| **Hashline 编辑** | P3 | 中 | 高 — 精确文件编辑 |
| **多工具配置发现** | P3 | 低 | 中 — 降低迁移成本 |

### 10.3 长期可借鉴（高侵入性）

| 特性 | 优先级 | 实现成本 | 价值 |
|------|--------|----------|------|
| **Rust N-API 性能层** | P4 | 高 | 中 — 性能瓶颈时引入 |
| **LSP 集成** | P4 | 高 | 高 — 代码质量 |
| **Python REPL** | P4 | 中 | 中 — 代码执行 |

### 10.4 设计哲学差异（保持 Agenda 特色）

以下 oh-my-pi 的设计 Agenda **不应采纳**，因为与 Agenda 的定位冲突：

| oh-my-pi 设计 | Agenda 不应采纳的原因 |
|---------------|----------------------|
| TUI 交互 | Agenda 是 Agent-facing，无 UI 是设计目标 |
| Session 自动标题 | 不需要，Agent 不关心 |
| 主题系统 | 同上 |
| OAuth 登录 | Agenda 用 API key + models.yaml，更简洁 |
| 浏览器工具 | 需要 Puppeteer 依赖，与 Agenda 的零依赖哲学冲突 |

---

## 附录：关键文件索引

| 主题 | oh-my-pi 文件 | 行数 |
|------|---------------|------|
| Compaction | `packages/coding-agent/src/session/compaction/compaction.ts` | 1,401 |
| Task Executor | `packages/coding-agent/src/task/executor.ts` | 1,291 |
| Task Index | `packages/coding-agent/src/task/index.ts` | 1,272 |
| Agent Session | `packages/coding-agent/src/session/agent-session.ts` | ~2,000+ |
| Hook Types | `packages/coding-agent/src/extensibility/hooks/types.ts` | ~200 |
| TTSR | `packages/coding-agent/src/export/ttsr.ts` | ~300 |
| Rule Matching | `packages/coding-agent/src/capability/rule.ts` | ~400 |
| Natives | `crates/pi-natives/src/lib.rs` | ~7,500（Rust） |
