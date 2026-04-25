# Agenda

> **DAG + agent loop = agenda, 一个可以表达任意深度、任意数量的 Agent 协作的，可以看作一个递归函数的agent layer**

```python
async def agenda(dag, inputs, depth=0):
    if len(dag.nodes) == 1:
        return await AgentLoop(dag.nodes[0], inputs).run()      # Base Case
    else:
        return await Scheduler(dag, inputs, depth=depth).run()   # Recursive Step
```

agenda 作为递归函数：Agent Loop 与 DAG Runner 的解耦设计

## 核心出发点

### 1. Subagent 从二等公民变成一等公民

传统框架中，Subagent 是"被管理"的对象——需要特殊的 spawn 机制、特殊的通信协议、特殊的上下文传递。Main Agent 和 Subagent 的实现往往不同。

**本设计的核心决策：Main Agent 和 Subagent 共享完全相同的 Agent Loop。没有区别。**

- 同一个 `AgentLoop` 类
- 同一套 Prompt / Context / Tools / Compaction 机制
- 同一个 `agenda()` 函数入口
- Subagent 不是被"创建"出来的，而是被"调用"出来的——和调用 `read_file` 一样自然

### 2. Agent 的调度定义成 DAG，DAG 可退化为单节点

把多 Agent 协作建模为 DAG 执行。

关键洞察：**单节点 DAG = 无调度 = 直接 AgentLoop.run()**。这意味着 DAG 可以自然退化，不需要特殊分支逻辑。

当这两个出发点结合，`agenda()` 就自然成为一个递归函数：

- 多节点 DAG → Scheduler 并行调度多个 Agent
- 单节点 DAG → Base Case，直接 AgentLoop.run()
- 每个 Agent 都可以调用 `agenda()` 继续分解——形成递归

> **这个设计的本质价值：它证明了多 agent 系统可以用一个递归函数统一表达。**

### 心智模型的统一

现有框架的心智负担：

| 框架      | 需要理解的概念                                  |
| --------- | ----------------------------------------------- |
| AutoGen   | 群聊机制、角色注册、特殊 subagent API、对话管理 |
| LangGraph | 静态 DAG、状态机、节点/边定义、无动态分解       |
| CrewAI    | 角色定义、流程配置、任务分配、无递归            |
| MetaGPT   | 角色、动作、观察、复杂状态转换、特殊 agent 类型 |

**本设计把所有这些压成了一个函数：**

```python
agenda(dag, inputs, depth=0)
```

开发者只需要理解三件事：

1. **单 agent 怎么工作** — `AgentLoop.run()`：收到 task，用 tools 执行，输出到 output/
2. **多 agent 怎么编排** — `DAG`：定义节点和依赖，Scheduler 并行调度
3. **agent 可以调用 agenda() 继续分解** — 递归：单节点 DAG 退化为 Base Case，多节点走 Scheduler

**仅此三项，覆盖所有场景。**

### 通用性

**任意深度的任意数量的 Agent / Agent Group 都可以用 `agenda()` 表达：**

| 场景                           | `agenda()` 如何表达                                                                   |
| ------------------------------ | ------------------------------------------------------------------------------------- |
| 1 个 Agent                     | `agenda(DAG(1 node))` → Base Case → `AgentLoop.run()`                                 |
| 3 个 Agent 并行                | `agenda(DAG(3 nodes))` → Scheduler 并行启动                                           |
| 10 层嵌套                      | Agent 在 depth=0 调用 `agenda()` → depth=1 调用 `agenda()` → ... → depth=10 Base Case |
| 混合结构（某些分支深、某些浅） | 每个 Agent 独立决定自己的子 DAG，自然形成不均匀深度的树                               |
| 任意数量、任意深度             | 同一个函数 `agenda(dag, inputs, depth=0)`                                             |

**一个函数，表达所有可能的多 Agent 拓扑。**

---

## 关键原则：Main Agent 与 Subagent 没有区别

**所有 Agent 共享同一个 Agent Loop 实现。**

- Main Agent 的调用者是顶层 Scheduler
- Subagent 的调用者也是 Scheduler，只是位于更深的递归层
- Agent Loop 本身**不知道**自己是 Main Agent 还是 Subagent
- Agent Loop 只关心：Task、Workspace、Tools、Context

这意味着 `subagent.py` 是一个历史包袱——它暗示 Subagent 需要特殊管理。实际上，Subagent 就是**被 DAG Runner 启动的另一个 Agent Loop**。

---

### 递归三要素

| 要素               | 对应实现                                                                                 |
| ------------------ | ---------------------------------------------------------------------------------------- |
| **Base Case**      | 单节点 DAG → 直接 `AgentLoop.run()`，跳过 Scheduler                                      |
| **Recursive Step** | 多节点 DAG → Scheduler 启动多个 Agent Loop，某些 Agent Loop 通过调用 `agenda()` 触发递归 |
| **Convergence**    | `MAX_DEPTH` 硬限制 + 子 DAG 节点数 ≤ 父 DAG（软约束）                                    |

### 递归类型

这是**间接递归**（Indirect Recursion），不是直接递归：

```
agenda() ──► Scheduler ──► AgentLoop ──► agenda() ──► Scheduler ──► AgentLoop
         (depth=0)                    (depth=1)                    (depth=2)
```

Agent Loop 里的 Agent 可以调用 `agenda()`（和调用 `read_file`、`write_file` 一样自然）。它传入自己构造的 DAG，但**不知道**这个调用会触发递归调度。

---

## 为什么是递归函数

从外部视角看，`agenda(dag, inputs, depth=0)` 的行为完全符合递归函数的定义：

```python
async def agenda(dag: DAG, inputs: Inputs, depth: int = 0) -> Outputs:
    if len(dag.nodes) == 1:
        # Base Case: 单节点 = 直接 Agent 执行
        return await AgentLoop(dag.nodes[0], inputs).run()
    else:
        # Recursive Step: 并行调度多个 Agent
        return await Scheduler(dag, inputs, depth=depth).run()
```

### 树形结构

每次 `agenda()` 调用形成树的一个节点：

- **内部节点**：多节点 DAG → 包含 Scheduler + 多个 Agent Loop
- **叶子节点**：Base Case → 单个 AgentLoop.run()
- **分支因子**：当前 DAG 的节点数
- **深度**：`agenda()` 嵌套调用的层数

注意：虽然 DAG 本身可能有共享依赖，但每个 `agenda()` 调用创建独立的 Workspace，因此**整棵树没有共享子树**，是严格的树结构。

---

## Agent Loop 的视角

对任意一个 Agent Loop 来说，它的世界观非常简单：

```
我收到了一个 Task
我有一个 Workspace（input/ workspace/ output/）
我可以调用外部函数（read_file, write_file, ..., agenda）
我开始执行
完成时把结果写入 output/
```

Agent Loop **永远不自知**自己在递归中的位置。它不关心：

- 自己是 depth=0 还是 depth=5
- 调用自己是顶层 Scheduler 还是另一个 Agent 的 `agenda()`
- 外面还有没有更大的 DAG

这种**位置透明性**是架构优雅的关键。

---

## DAG Runner 的视角

DAG Runner 同样简单：

```
我收到了一个 DAG
我解析依赖关系
我并行启动就绪的节点
每个节点 = 一个 Agent Loop
我等待所有节点完成
我收集产物
```

DAG Runner **不关心** Agent Loop 内部做什么。它尤其不关心某个 Agent 是否调用了 `agenda()`。

---

## Base Case 的退化

当 DAG 只有 1 个节点时，DAG Runner 应该**完全退化**：

```python
if len(dag.nodes) == 1:
    # 不创建 Scheduler
    # 不创建 hints.md
    # 不创建 state.json
    # 不管理并行调度
    return await AgentLoop(node.task, node.workspace).run()
```

这保证了一层 agenda 调用的最小开销。**最简 DAG = 最简 Agent = AgentLoop.run()。**

---

## 对偶性

这个架构揭示了一个漂亮的对偶关系：

| 概念        | 最简形式           |
| ----------- | ------------------ |
| Agent Group | 单个 Agent         |
| Scheduler   | 直接调用 AgentLoop |
| DAG         | 单节点             |
| agenda()    | AgentLoop.run()    |

**四个不同的概念在最简情况下收敛到同一个东西。** 这就是为什么整个系统看起来像递归函数——每一层都是同一种结构，只是复杂度不同。

---

## 两层实现重点

### DAG 层

| 模块               | 实现重点                                                                     |
| ------------------ | ---------------------------------------------------------------------------- |
| **DAG 协议**       | DAG 的定义格式（JSON/YAML/Python API）、Schema 验证、循环依赖检测            |
| **DAG Scheduler**  | 拓扑排序、并行度控制（`max_parallel`）、就绪节点检测、崩溃恢复               |
| **Agent 间通信**   | 输入/产物路由（`dep_inputs`）、Workspace 隔离、文件传递协议、`#section` 锚点 |
| **Base Case 退化** | 单节点跳过 Scheduler，直接 `AgentLoop.run()`                                 |

### Agent Loop 层

| 模块             | 实现重点                                                                         |
| ---------------- | -------------------------------------------------------------------------------- |
| **Prompt 组装**  | System prompt（目录结构说明、Tool 描述）、Task prompt、Hints（可用文件自动注入） |
| **Context 管理** | 消息历史维护、Token 估算、Compaction 触发（Ratio + Reserved）、Compaction 策略   |
| **Tool 管理**    | Tool 注册表、Tool 调用解析、结果回传、错误处理（Tool 失败 → 重试/报错）          |
| **重新唤醒机制** | Reload 从 `state.json` 恢复、断点续执行、运行→挂起→恢复的状态流转                |

**两个层之间唯一的耦合点：**

- DAG 层启动 Agent Loop 时，传入 `task` + `workspace` + `tools`（含 `agenda()`）
- Agent Loop 完成时，产物写入 `output/`，DAG 层读取

---

## 与现有系统的对比

| 系统                | DAG   | 递归     | Agent Loop 统一     | Workspace 隔离 |
| ------------------- | ----- | -------- | ------------------- | -------------- |
| AutoGen             | ✗     | ✓ (群聊) | ✗ (不同 Agent 类型) | ✗              |
| LangGraph           | ✓     | ✗        | ✓                   | ✗              |
| CrewAI              | ✓     | ✗        | ✓                   | ✗              |
| MetaGPT             | ✓     | ✗        | ✗                   | ✓              |
| **Agenda (本设计)** | **✓** | **✓**    | **✓**               | **✓**          |

**四者同时满足是 Agenda 的独特之处。**

---

## 待实现项

1. **删除 `subagent.py`** — Subagent 没有特殊逻辑，递归通过 Agent 调用 `agenda()` 实现
2. **Agent 可调用 `agenda()`** — Agent Loop 里的 Agent 构造子 DAG 后直接调用 `agenda()` 实现递归
3. **Base Case 优化** — 单节点 DAG 跳过 Scheduler，直接 `AgentLoop.run()`
4. **`agenda()` 顶层函数** — 统一入口，替代当前的直接 Scheduler 调用

---

## 一句话总结

> **递归不在 Agent Loop 里，递归在 DAG 的嵌套里。Agent Loop 永远是叶子节点，永远是 Base Case。**
