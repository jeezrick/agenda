# Claude Code `/loop` vs Codex `/goal` 对比分析

> 调研时间：2026-05-06 · 源码来源：Claude Code bundle `src/skills/bundled/loop.ts`、`src/utils/cronScheduler.ts`；Codex `codex-rs/core/src/goals.rs`

## 一、一句话区别

`/loop` 是**定时重复提醒**——"每 N 分钟再做一次"。`/goal` 是**目标驱动的自主 Agent**——"我盯着你，空闲了就推你继续，直到完成或预算耗尽"。

这两个机制解决的完全不同的问题，但容易混淆因为它们都让 Agent 自动化地"持续工作"。

## 二、Claude Code `/loop` 详解

### 2.1 机制：Cron 定时调度

`/loop` 是 Claude Code 的一个内置技能（skill），底层是 cron 调度器。用户说 `/loop 5m 检查构建状态` 时：
1. 解析间隔（`5m` → `/5 * * * *`）
2. 通过 `CronCreate` 工具注册一个重复任务到 `~/.claude/scheduled_tasks.json`
3. 调度器每 1 秒检查是否有任务到期
4. 如果有，以 `'later'` 优先级将 prompt 入队
5. REPL 空闲时消费入队的 prompt，启动新轮次

```
用户: /loop 5m 检查构建状态
        │
        ▼
┌──────────────────────────────────────────────┐
│ loop.ts 解析：间隔=5m，prompt="检查构建状态"   │
│   → CronCreate(cron="*/5 * * * *", prompt)   │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│ cronScheduler.ts：                            │
│   每 1 秒检查 scheduled_tasks.json            │
│   到期 → 以 'later' 优先级入队                 │
│   REPL 空闲 → 执行 prompt（新 Agent 轮次）     │
│   7 天后自动过期                               │
└──────────────────────────────────────────────┘
```

### 2.2 两种模式

| 模式 | 工具 | 触发方式 |
|------|------|---------|
| **fixed** | `/loop 5m <task>` | 固定间隔，cron 定时触发 |
| **dynamic** | `/loop` (无参数) | 模型自己通过 `ScheduleWakeup` 决定下次唤醒时间 |

**Fixed 模式**：适合轮询——"每 10 分钟检查 CI 状态"、"每小时拉一次日志"。

**Dynamic 模式**：模型在每轮结束时主动调用 `ScheduleWakeup(delaySeconds, reason, prompt)` 决定下次执行间隔。系统提示词建议避开 300 秒（prompt cache TTL），要么 60-270 秒（保持缓存），要么 1200 秒以上（分摊代价）。模型根据自己的判断选择间隔——"构建可能要 8 分钟，我睡 480 秒"。

### 2.3 关键设计细节

- **防惊群**：周期性任务加 10% jitter（最大 15 分钟），由 UUID 确定性计算
- **一次性任务防准点**：如果 `:00` 或 `:30`，提前最多 90 秒
- **文件锁**：同一目录下多个 Claude 进程不会重复触发同一个任务
- **持久化**：`durable: true` 写入文件，跨进程存活；默认仅会话级
- **停止**：`CronDelete`、`CLAUDE_CODE_DISABLE_CRON` 环境变量、GrowthBook 功能标记
- **无状态**：不追踪目标进度、不记录已消耗资源、不验证完成度

### 2.4 /loop 没有的东西

- 没有目标定义（只是重复执行同一个 prompt）
- 没有进度追踪（不知道任务完成了多少）
- 没有预算管理（可能无限消耗 token）
- 没有完成审计（什么时候停？用户手动取消或 7 天过期）
- 没有状态流转（就是一个循环定时器）

## 三、Codex `/goal` 详解（简要回顾）

详见 [research-codex-goal.md](./research-codex-goal.md)。

核心区别：

| 维度 | `/loop` | `/goal` |
|------|---------|---------|
| **驱动机制** | Cron 定时调度 | 线程空闲检测 → 自主延续 |
| **触发条件** | 时间到了就触发 | Agent 空闲 + 目标未完成 |
| **状态管理** | 无 | 4 种状态流转 |
| **预算追踪** | 无 | Token + 时间双重核算 |
| **完成审计** | 无 | 结构化审计清单注入 continuation prompt |
| **停止条件** | 手动取消 / 7 天过期 | 目标完成 / 预算耗尽 / 手动暂停 |
| **持久化** | JSON 文件或内存 | SQLite 数据库 |
| **提示语** | 固定（用户原始输入） | 动态生成（含用量、剩余预算、审计清单） |

## 四、为什么是两个完全不同的设计

`/loop` 的设计假设：**你知道你要什么，需要定时提醒**。它的核心能力是"别忘了我说的这件事"。

`/goal` 的设计假设：**你定义了终点，但不知道怎么到达**。它的核心能力是"持续逼近目标，直到你告诉我到了"。

所以 `/loop` 的 prompt 是**用户写的原话**——"检查构建状态"——每次一样。`/goal` 的 prompt 是**系统动态生成**——每次包含已用时间、剩余预算、完成审计清单——每次不同。

`/loop` 是外驱的（时间到了就做）。`/goal` 是内驱的（空闲了就做，直到完成）。

## 五、对 Agenda 的启示

Agenda 目前两者都没有。但它的架构对两种模式都有先天优势：

**对于 `/loop` 风格**：Agenda 已经有 `DAGScheduler.run()` 的持久化调度状态。可以很容易地加一个 `agenda loop` 命令——类似 daemon 模式，但按 cron 间隔重新执行同一个 DAG。

**对于 `/goal` 风格**：Agenda 的 `AgentLoop` 天然支持 "agent 完成一轮后检查是否有活跃目标"。核心需要新增：
1. 目标状态文件（`.system/goal.json`）→ 复用 Session 的文件系统模式
2. 空闲检测 → DAG 调度器已有（`while not stop_event.is_set()` 循环）
3. 动态 continuation prompt → Jinja2 模板（已有基础设施）
4. 预算追踪 → 已有 `TokenUsage` 数据（`agent.py` usage 日志）

**Agenda 的独特机会**：把 goal 和 DAG 结合起来。一个 Goal 可以自动生成一个 DAG——"分析这个代码库" → 拆成 5 个并行调研节点 + 1 个汇总节点。Goal 不仅仅是"继续工作"，而是"拆任务 → 调度子 DAG → 验证 → 继续"。这正是 Agenda 的核心能力所在。

## 六、总结

```
/loop = 定时器 + 固定 prompt + 无状态
/goal = 自治 Agent + 动态 prompt + 四状态 + 预算 + 审计

Agenda 的机会 = /goal 的目标驱动 + DAG 的任务分解
                = 不是"继续做同一件事"
                = 而是"为这个目标创建最优子 DAG，并行执行，汇总验证"
```

如果你要做，我建议先做 Goal 风格而非 Loop 风格。Loop 是一个 cron 表——功能简单但价值有限。Goal + DAG 是 Agenda 的独特组合——无人能轻易复制。
