# Agenda 最优技术选型报告

> 基于三份调研文档的综合结论：
> - `research-agent-loop-design.md` — Agent Loop 与 Subagent Loop 设计调研
> - `research-context-compaction.md` — Context Compaction 实现方案对比
> - `research-subagent-scheduling.md` — Subagent 调度机制调研

---

## 一、Agent Loop 层选型

### 1.1 Prompt 组装：Jinja2 模板 + Static/Dynamic 分离

| 候选方案 | 评估 | 选型 |
|---------|------|------|
| Butterfly 字符串拼接 | 简单但僵硬 | ❌ |
| Claw Code 字符串拼接 | 同上 | ❌ |
| **Kimi CLI Jinja2 (`${VAR}`)** | 灵活、可配置、支持条件渲染 | ✅ **基础方案** |
| **Claude Code `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`** | 缓存优化：静态段长期缓存，动态段每次刷新 | ✅ **进阶优化** |

**最优组合**：以 Kimi CLI 的 Jinja2 模板化为基础，引入 Claude Code 的 **缓存分界标记** 思想，将 system prompt 分为：

- `static_prefix`（system.md + env.md + tool 描述）— 可缓存
- `dynamic_suffix`（memory + hints + 可用文件列表）— 每次激活刷新

**关键决策**：**不区分 main/sub 的 system prompt**。四家框架都在 prompt 层面给 subagent "打补丁"（mode.md、ROLE_ADDITIONAL、sub-agent 声明），Agenda 应彻底消除这种区分——同一个 Jinja2 模板，同一套变量注入。

---

### 1.2 Context 管理：JSON Lines + 链式结构 + Checkpoint

| 维度 | 最优选型 | 来源 |
|------|---------|------|
| **存储格式** | JSON Lines (`turns.jsonl`) | Kimi CLI + Butterfly 共识 |
| **消息链** | `parent_uuid` 形成有向链 | Claude Code（支持多级关系追踪） |
| **原子写入** | temp + rename | Claw Code |
| **Resume 粒度** | turn 级别回放 | Kimi CLI |
| **Checkpoint/回滚** | `revert_to(checkpoint_id)` | Kimi CLI（Agenda 可后期加入） |

**不采纳**：Claude Code 的 `sidechain/` 独立 transcript（Agenda 的 DAG 节点隔离已通过 workspace 实现，不需要 sidechain）。

---

### 1.3 Tool 管理：声明式注册 + 显式 Context 传递

| 维度 | 最优选型 | 来源 |
|------|---------|------|
| **注册方式** | `ToolRegistry` + 装饰器 + 动态 schema 推断 | Kimi CLI + Claude Code 简化版 |
| **Schema 生成** | `inspect.signature` + 类型标注 → JSON Schema | Claude Code（Zod 的 Python 等价） |
| **依赖注入** | `ToolUseContext` 显式参数传递 | **Claude Code** |
| **权限检查** | `check_permissions()` hook | Claude Code |
| **后台执行** | `asyncio.create_task()` + `BackgroundTaskManager` | Kimi CLI |

**关键决策**：**Tool 调用必须传入 Session 上下文**，禁止全局变量。这保证了同一个 AgentLoop 类可以在不同的 Session（main/sub）中安全复用。

---

### 1.4 Agent vs Subagent：同一类，零差异

| 框架 | 是否同一类 | 差异方式 |
|-----|-----------|---------|
| Butterfly | ✅ 同一 Agent 类 | `mode=explorer/executor` |
| Claw Code | ✅ 同一 `ConversationRuntime` | 泛型参数不同 |
| Kimi CLI | ✅ 同一 `KimiSoul` | `runtime.role` 区分 |
| Claude Code | ✅ 同一 `query()` 函数 | `ToolUseContext` 隔离 |
| **Agenda** | ✅ 同一 `AgentLoop` | **无任何区分** |

**Agenda 的核心创新**：`agenda()` 不是特殊工具，而是和 `read_file`、`write_file` 一样的普通函数。Agent Loop 调用 `agenda()` 时**不知道自己触发了递归**——这是"位置透明性"的来源。

---

## 二、Context Compaction 选型

### 2.1 基础策略：LLM 结构化摘要（Kimi CLI 模式）

Agenda 已移植 Kimi CLI 的 `SimpleCompaction`，这是**最小可用且质量合格**的方案：

```python
# 保留最近 N 条消息（默认 2）
# 更早历史发送给 compaction LLM，输出 XML 结构化摘要
# rotate 旧文件 → 清空 → 重写 system prompt → 写入压缩结果
```

### 2.2 必须补充的工程防护

| 缺失能力 | 来源 | 优先级 |
|---------|------|--------|
| **Tool 边界安全** | Claw Code / Claude Code | 🔴 P0 — 防止拆分 tool_use/tool_result 对 |
| **重试机制** | Kimi CLI（3 次指数退避） | 🔴 P0 — Agenda 当前直接抛异常 |
| **Token 估算改进** | Claude Code（按 block type 精确估算） | 🟡 P1 — `len//4` 对中文偏差大 |
| **可观测性** | Claude Code（10+ 维度埋点） | 🟡 P1 — 至少记录压缩前后 token 数 |
| **自定义指令** | Kimi CLI / Claude Code | 🟢 P2 — `/compact 保留数据库讨论` |

### 2.3 长远方向：Session Memory Compaction（Claude Code 策略 1）

> 独立于压缩之外的记忆系统持续提取关键信息，压缩时直接复用。零额外 LLM 调用成本。

实现成本高，**项目成熟期**再考虑。

---

## 三、Subagent 调度选型

### 3.1 调度模型：同进程 async（Kimi CLI + Claude Code）

| 候选 | 评估 | 选型 |
|-----|------|------|
| Butterfly 独立 daemon | 太重，每个 session 独立进程 | ❌ |
| Claw Code 独立 OS 线程 | Rust 特有，Python 不适用 | ❌ |
| **Kimi CLI `asyncio.create_task()`** | 轻量、Python 友好 | ✅ |
| **Claude Code async generator** | 支持进度回调、结构化产物 | ✅ **进阶** |

**Agenda 选型**：同进程 `asyncio`，支持可选的 `on_progress` 回调（学 Claude Code 的 generator 模式）。

---

### 3.2 输入传递：显式 `inputs`，不自动继承

**四家共识**：子 agent **不继承 parent 的消息历史**。

Agenda 的 `inputs` 设计：

```python
@dataclass
class Inputs:
    """agenda() 的输入参数 —— 显式传递，不自动继承。"""

    workspace: Path           # 独立工作目录
    files: dict[str, Path]    # dep_inputs 显式路由
    context: str | None       # 压缩后的上下文摘要（非完整历史）
    metadata: Metadata        # depth, parent_node_id, call_chain
```

对比 Butterfly 的"复制 10+ 个文件"（system/task/env/skills/memory/tools/config/mode），Agenda 的显式传递更干净、更可预测。

---

### 3.3 Workspace 隔离：独立目录 + dep_inputs 路由

| 维度 | 选型 | 来源 |
|------|------|------|
| **默认隔离** | 每个节点独立 `input/`、`workspace/`、`output/` | Butterfly |
| **父子文件读取** | `dep_inputs` 结构化映射（非 symlink） | 改进 Butterfly |
| **可选共享模式** | 未来支持 `shared_workspace` 标志 | Claude Code `isolation` 启发 |

**不采纳**：Butterfly 的 `playground/parent/` symlink（Guardian 复杂度太高，Agenda 用文件复制更可控）。

---

### 3.4 结果回传：output/ 目录 + 结构化产物

| 框架 | 回传方式 | 评估 |
|-----|---------|------|
| Butterfly | 轮询 context.jsonl，提取最终文本（8000 字截断） | 弱 |
| Claw Code | 写入 `.claw/agents/{id}.md`，parent 主动读取 | 弱 |
| Kimi CLI | 提取最后消息文本 | 中等 |
| Claude Code | `ContentBlock[]` + 工具调用计数 + 耗时 | **强** |
| **Agenda** | `output/` 目录产物 + `dep_inputs` 自动路由 | **最优** |

Agenda 的优势：产物是**文件**（任意格式），不是文本提取。下游节点通过 `dep_inputs` 自动获取上游产物。

---

### 3.5 深度控制：`MAX_DEPTH` 软约束

| 框架 | 限制方式 | 评估 |
|-----|---------|------|
| Butterfly | `MAX_DEPTH = 2`，硬拒绝 | 过于严格 |
| Claw Code | 白名单排除 `Agent` 工具 | 禁止递归 |
| Kimi CLI | `role != "root"` 直接报错 | 禁止递归 |
| Claude Code | `ALL_AGENT_DISALLOWED_TOOLS` 排除 AgentTool | 禁止递归 |
| **Agenda** | `MAX_DEPTH` 软约束 + 子 DAG 节点数 ≤ 父 DAG | **允许递归，但收敛** |

Agenda 的哲学：**递归是特性，不是 bug**。通过数学约束（`depth` 硬上限 + 子 DAG 规模递减）保证收敛，而非禁止递归。

---

## 四、完整技术选型总表

| 维度 | 最优选型 | 核心来源 | Agenda 创新点 |
|------|---------|---------|--------------|
| **Prompt 模板** | Jinja2 + static/dynamic 分离 | Kimi CLI + Claude Code | 不区分 main/sub |
| **Context 存储** | JSON Lines + turn 级持久化 | Kimi CLI + Butterfly | `turns.jsonl` + `events.jsonl` 双轨 |
| **Context 压缩** | LLM 结构化摘要 + Tool 边界安全 | Kimi CLI + Claw Code | 更早触发（75%） |
| **消息链** | `parent_uuid` 有向链 | Claude Code | 追踪递归调用链 |
| **Tool 注册** | 装饰器 + 动态 schema 推断 | Claude Code 简化 | 无特殊 tool |
| **Tool 执行** | `asyncio` 同进程 + 显式 Session 传递 | Kimi CLI + Claude Code | Guardian 路径边界 |
| **调度模型** | `asyncio.create_task()` | Kimi CLI | `agenda()` 普通函数 |
| **Workspace** | 独立三目录（input/workspace/output） | Butterfly | `dep_inputs` 结构化路由 |
| **Context 传递** | 显式 `inputs`，不继承历史 | **四家共识** | `Inputs` dataclass |
| **结果回传** | `output/` 目录产物 | 改进四家 | `dep_inputs` 自动路由 |
| **深度控制** | `MAX_DEPTH` 软约束 + DAG 规模递减 | Agenda 原创 | 允许递归 |
| **Resume** | turn 级回放 + 孤儿 tool_call 补全 | Butterfly + Kimi | `state.json` 状态机 |
| **Cancel 传播** | `CancelledError` + 事件级联 | Butterfly + Claude Code | 父子双向中断 |

---

## 五、现状与差距

### 5.1 已实现

- ✅ 同进程 async 调度
- ✅ 独立 Workspace 隔离（input/workspace/output）
- ✅ LLM 结构化 Compaction（Kimi CLI 移植版）
- ✅ 文件系统 IPC（`turns.jsonl` + `events.jsonl`）
- ✅ 状态持久化 + 恢复（`state.json` + `scheduler_state.json`）
- ✅ 模型 fallback + 超时控制

### 5.2 缺失项（按优先级）

| 优先级 | 缺失能力 | 来源 | 影响 |
|--------|---------|------|------|
| 🔴 P0 | **Tool 边界安全**（compaction 不拆 tool_use/tool_result） | Claw Code / Claude Code | 压缩后可能导致 LLM 400 错误 |
| 🔴 P0 | **Compaction 重试机制** | Kimi CLI | 单次失败直接终止 session |
| 🔴 P0 | **`agenda()` 作为普通 tool 可用** | Agenda 设计核心 | README 描述的递归尚未落地 |
| 🟡 P1 | **Jinja2 Prompt 模板化** | Kimi CLI | 当前字符串拼接难以维护 |
| 🟡 P1 | **Token 估算改进** | Claude Code | `len//4` 中文偏差约 30% |
| 🟡 P1 | **可观测性埋点** | Claude Code | 无法量化压缩效果 |
| 🟢 P2 | **Checkpoint/回滚** | Kimi CLI | 错误恢复能力 |
| 🟢 P2 | **自定义 compaction 指令** | Kimi CLI / Claude Code | 用户可控压缩策略 |
| ⚪ P3 | **Session Memory Compaction** | Claude Code | 零成本压缩，成熟期实现 |

---

## 六、一句话总结

> **以 Kimi CLI 的 Pythonic 工程为基础，吸收 Claude Code 的架构深度（context 隔离、链式消息、声明式工具），用 Butterfly 的文件系统 IPC 做持久化，最终用 Agenda 的递归 DAG 统一所有多 Agent 场景。**
