# Codex `/goal` 功能深度分析

> 调研时间：2026-05-06 · Codex commit: `f9a907aebe`

## 一、功能概述

`/goal` 是 Codex 的持久化线程目标（Thread Goal）系统。用户通过 `/goal "objective"` 为当前线程设置一个具体目标，Codex 自动追踪进度、消耗资源（token + 时间），并在线程空闲时通过自主延续（autonomous continuation）驱使 Agent 朝目标推进，直到完成或预算耗尽。

核心价值：将"一次对话"升级为"一个目标驱动的自治 Agent"，用户只需设置目标并审核结果，中间的推进过程由系统自主管理。

## 二、四种状态及其流转

```
           /goal "obj"
              │
              ▼
          ┌───────┐    目标完成     ┌──────────┐
          │ Active │──────────────►│ Complete │
          └───┬───┘               └──────────┘
              │
     ┌───────┼───────┐
     │       │       │
     ▼       ▼       ▼
  Paused  Budget    (停留在 Active
 (中断)   Limited     当有空闲时自主继续)
```

| 状态 | 含义 | 触发方式 |
|------|------|---------|
| `Active` | 正在积极追求目标 | `/goal` 创建或 `/goal resume` |
| `Paused` | 暂停（中断时自动暂停） | Ctrl+C 中断或 `/goal pause` |
| `BudgetLimited` | Token 预算耗尽，不应继续实质性工作 | 系统检测 tokens_used >= token_budget |
| `Complete` | 目标达成 | Agent 调用 `update_goal` 或用户 `/goal complete` |

**关键设计原则：模型不能暂停/恢复/预算限制目标。** `update_goal` 工具只接受 `status: "complete"`，其他状态变更全部由系统或用户控制。这防止 Agent 在困难面前主动放弃。

## 三、架构分层

### 3.1 持久化层（`state/runtime/goals.rs`）

SQLite 表结构：
```sql
CREATE TABLE thread_goals (
    thread_id   TEXT PRIMARY KEY,  -- 每个线程最多一个目标
    goal_id     TEXT,              -- UUID，用于乐观锁
    objective   TEXT,              -- 用户设定的目标描述
    status      TEXT,              -- Active/Paused/BudgetLimited/Complete
    token_budget INTEGER,          -- 可选 token 预算上限
    tokens_used INTEGER,           -- 已消耗 token 数
    time_used_seconds INTEGER,     -- 已用时间（秒）
    created_at_ms INTEGER,
    updated_at_ms INTEGER
);
```

关键操作：
- `insert_thread_goal` — 首次创建，如果已有目标则失败（单例保证）
- `replace_thread_goal` — 替换目标（支持幂等：相同 objective + 非终态 → 只更新状态）
- `update_thread_goal` — 部分更新（状态、预算），支持 `expected_goal_id` 乐观锁
- `account_thread_goal_usage` — 核心：累加 token_delta + time_delta，自动判定是否 BudgetLimited
- `pause_active_thread_goal` — 将 Active 改为 Paused

### 3.2 协议层（`codex_protocol::protocol`）

```rust
pub struct ThreadGoal {
    pub thread_id: ThreadId,
    pub objective: String,
    pub status: ThreadGoalStatus,     // Active | Paused | BudgetLimited | Complete
    pub token_budget: Option<i64>,
    pub tokens_used: i64,
    pub time_used_seconds: i64,
    pub created_at: i64,
    pub updated_at: i64,
}

pub struct ThreadGoalUpdatedEvent {
    pub thread_id: ThreadId,
    pub turn_id: Option<String>,  // None = 外部变更，Some = 当前 turn 内触发
    pub goal: ThreadGoal,
}
```

每次目标状态变更（含用量累加）都通过 `ThreadGoalUpdatedEvent` 事件推送，TUI 监听此事件更新状态栏。

### 3.3 模型工具层（`tools/handlers/goal.rs`）

Agent 可用的三个工具：

| 工具 | 作用 | 限制 |
|------|------|------|
| `get_goal` | 读取当前目标（含预算、用量、剩余） | 无限制 |
| `create_goal` | 首次创建目标 | 已有目标时失败；只接受 `objective` + 可选 `token_budget` |
| `update_goal` | 标记目标完成 | **只能传 `status: "complete"`**；不能暂停/恢复/预算限制 |

**关键设计**：`update_goal` 的 schema 定义为 `string_enum(vec!["complete"])`，这意味着模型的 schema 里只有 "complete" 一个选项，它不知道 "paused" 或 "budgetLimited" 的存在。Goal 的控制权牢牢掌握在系统和用户手中。

### 3.4 核心运行时（`core/goals.rs`，1640 行）

这是最复杂的模块，负责：

#### 3.4.1 用量核算（Goal Accounting）

双重核算体系：
- **Turn 级核算**（`GoalTurnAccountingSnapshot`）：按 turn 追踪 token delta
- **Wall-Clock 核算**（`GoalWallClockAccountingSnapshot`）：按实际时间追踪

工作流程：
1. Turn 开始时，记录 token 基线（`mark_thread_goal_turn_started`）
2. 每次工具执行（非 goal 工具）后，计算 token delta 并累加到目标（`account_thread_goal_progress`）
3. Turn 结束时做最终核算（`finish_thread_goal_turn`）
4. 外部变更（如用户修改目标）前先结算当前用量（`account_thread_goal_before_external_mutation`）

核算使用信号量（`accounting_lock: Semaphore`）保证线程安全。

#### 3.4.2 预算限制转向（Budget Limit Steering）

当累积 token 超过预算时，系统自动将状态改为 `BudgetLimited` 并注入一个隐藏的 developer prompt：
```
The active thread goal has reached its token budget.
...
The system has marked the goal as budget_limited, so do not start 
new substantive work for this goal. Wrap up this turn soon...
```

这个 prompt 通过 `inject_response_items` 注入到消息队列，模型在下一轮会看到它并知道应该收尾。

**关键细节**：同一个 goal_id 的预算限制只报告一次（`budget_limit_reported_goal_id` 去重），防止注入重复消息。

#### 3.4.3 自主延续（Autonomous Continuation）

这是 Goal 系统最核心的设计。当线程空闲时（当前 turn 结束、没有排队输入、没有 pending items），系统启动一个新的 agent turn：

```rust
// 简化版流程
goal_continuation_candidate_if_active() {
    if 计划模式 → 跳过
    if 已有活跃 turn → 跳过
    if 有排队输入 → 跳过
    if 没有目标 或 目标不是 Active → 跳过
    → 构建 developer prompt，启动新 turn
}
```

延续 prompt 告诉模型：
1. 当前目标是什么（objective）
2. 已用时间/预算/剩余
3. **详细完成审计清单**：要求模型重新审视目标、列出清单、逐项检查证据、不依赖代理信号和部分进展
4. **只在真正完成时调用 update_goal**

这个设计非常聪明——不是简单的 "继续工作" prompt，而是一个结构化的完成度审计流程。

#### 3.4.4 中断暂停 + 恢复（Interrupt Pause + Resume）

```
中断发生：
  TaskAborted(reason: Interrupted)
    → account_thread_goal_progress()  # 先结算用量
    → pause_active_thread_goal_for_interrupt()  # Active → Paused

恢复：
  ThreadResumed
    → restore_thread_goal_runtime_after_resume()
      → 如果 Paused/BudgetLimited/Complete → 清理运行时状态
      → 如果 Active → 恢复 wall_clock 追踪

用户使用 /goal resume：
  → 重新标记为 Active
  → 启动 maybe_continue_goal_if_idle
```

#### 3.4.5 Plan 模式的特殊处理

当 `collaboration_mode` 为 `Plan` 时，`should_ignore_goal_for_mode` 返回 true，Goal 系统完全跳过目标追踪。这意味着：
- Plan 模式（/plan）不会消耗目标预算
- Plan 模式不会触发目标延续
- 活动目标在 Plan 模式下暂停核算

## 四、完整流程图

```
用户输入：/goal "实现用户登录功能" [token_budget=50000]
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ TUI → AppEvent.SetThreadGoal                                    │
│   → App-Server → StateRuntime.replace_thread_goal()             │
│   → INSERT INTO thread_goals (status=Active)                    │
│   → 发送 Session 事件                                            │
└─────────────────────────────────────────────────────────────────┘
    │
    ▼ 模型收到 ThreadGoalUpdatedEvent（可观测到目标存在）
    │
    ▼ Agent 开始工作（正常 turn 执行）
    │
    ├── Turn Started    → 记录 token baseline, 标记 active goal
    ├── Tool Completed  → 计算 delta → account_progress()
    │                       ├── Token 未超预算 → 继续
    │                       └── Token 超预算   → BudgetLimited
    │                           └── inject budget_limit prompt
    ├── Turn Finished   → 最终核算
    │
    ├── [空闲] → maybe_continue_goal_if_idle()
    │   │
    │   ├── goal_continuation_candidate_if_active()
    │   │   └── 构建 continuation prompt（含完成审计清单）
    │   │
    │   └── start_task() → 新 turn 开始
    │       └── Agent 收到 "Continue working toward the active thread goal..."
    │           └── 执行工作 → 自检是否完成
    │               ├── 未完成 → 继续（产生更多轮次）
    │               └── 完成 → 调用 update_goal(status="complete")
    │
    └── Agent 调用 update_goal("complete")
        └── state.update_thread_goal(status=Complete)
            └── 更新 goal runtime state → clear accounting
            └── 发送事件（TUI 状态栏更新）
            └── 如果 TokenBudgeted → 汇报最终用量
```

## 五、关键技术设计决策

### 5.1 目标单例（Thread-Scoped Singleton）
每个线程最多一个目标。`create_goal` 在已有目标时失败，`replace_thread_goal` 支持同目标幂等更新。这简化了心智模型——一个线程 = 一个目标 = 一个焦点。

### 5.2 模型只能标记完成，不能暂停/放弃
`update_goal` 的 schema 只有 `"complete"`。Agent 不能主动暂停、不能放弃、不能预算限制。这防止了 Agent 在遇到困难时"放弃目标"——它唯一的选择是继续工作或标记完成。

### 5.3 双重核算（Token + Time）
不是只算 token。`time_used_seconds` 追踪实际耗时，用 `time.monotonic()` + `Instant` 保证不受系统时钟调整影响。核算使用 `saturating_sub` 防止溢出或负值。

### 5.4 乐观锁（Optimistic Concurrency）
`update_thread_goal` 接受可选的 `expected_goal_id`，如果实际 goal_id 不匹配则拒绝更新。这防止了竞态条件（如外部同时修改了目标）。

### 5.5 去重预算警告
`budget_limit_reported_goal_id` 记录已报告预算限制的目标 ID。如果目标被替换后再次超预算，才会重新报告。避免同一目标反复注入相同警告。

### 5.6 指标埋点
完整的可观测性：
- `goal.created` — 新建目标
- `goal.budget_limited` — 触及预算
- `goal.completed` — 标记完成
- `goal.token_count` — 直方图：目标消耗的 token 数
- `goal.duration_seconds` — 直方图：目标耗时

所有指标带 status tag，可区分 Completing vs BudgetLimit 场景。

### 5.7 防逃逸（XML Escaping）
目标描述通过 `<untrusted_objective>` XML 标签传递给模型，但内容做 XML 转义（`&` → `&amp;` 等），防止模型从目标描述中逃逸并注入指令。`escape_xml_text` 确保目标内容失去特殊含义。

## 六、对 Agenda 的启示

在 Agenda 做类似功能的可选方案：

1. **轻量版**：无需持久化数据库。利用 Agenda 的文件系统状态——在 `.system/goal.json` 存储目标信息，在 `turns.jsonl` 追踪用量。保持简单。

2. **自主延续**：Agenda 可以用现有的 `_compact_context` 和 `_call_llm` 实现类似的 "空闲继续" 逻辑。关键是 Agent 完成一个 turn 后在调度器层面检查是否有活跃目标，如果有且线程空闲，注入 continuation prompt 启动下一个 turn。可以复用 Agenda 已有的 "restart from turns.jsonl" 机制。

3. **预算限制**：Agenda 的 `token_cap` 是模型级的。Target-level 的 token_budget 可以作为一个 node 的 `max_tokens` 超集来实现——当目标消耗接近上限时注入 steering prompt。

4. **与 Agenda 的结构化输出集成**：Goal 的 objective 天然适合和 `output_schema` 配合——目标定义 "要做什么"，schema 定义 "产出什么格式"。
