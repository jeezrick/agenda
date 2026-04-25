# Agenda as Recursive Function — 数学类比

## 核心观点

从递归函数的角度看，agenda 应该满足：

> **Base Case = Normal Agent**
> **Recursive Step = DAG Decomposition**
> **Convergence = 等秩收敛**

---

## 1. 传统递归函数的范式

### 1.1 数学归纳法结构

```
f(n):
  if n == 0:           ← Base Case（原子操作）
    return c
  return g(f(n-1))     ← Recursive Step（组合子问题解）
```

三个必要条件：
1. **Base Case**：n=0 时直接返回，不递归
2. **Recursive Step**：f(n) 用 f(n-1) 的结果
3. **Convergence**：n → n-1 → n-2 → ... → 0，单调递减

### 1.2 归并排序的类比

```
merge_sort(arr):
  if len(arr) <= 1:           ← Base Case：原子数组
    return arr
  
  mid = len(arr) // 2
  left = merge_sort(arr[:mid])   ← 递归：左半
  right = merge_sort(arr[mid:])  ← 递归：右半
  
  return merge(left, right)      ← 组合：合并两个有序数组
```

| 归并排序 | agenda 对应 |
|---------|-----------|
| `len(arr) <= 1` | DAG 只有 1 个节点 |
| `arr[:mid]` | 子 DAG（部分节点） |
| `merge(left, right)` | dep_inputs 产物合并 |

---

## 2. agenda 的递归范式

### 2.1 期望的函数签名

```python
def agenda(dag, inputs, depth=0) -> outputs:
    """
    递归执行 DAG。
    
    Base Case: dag 只有 1 个节点（或 depth >= MAX）
    Recursive Step: 节点调用 run_dag() 生成子 DAG
    Convergence: 子 DAG 节点数 <= 父 DAG 节点数（等秩或减秩）
    """
```

### 2.2 状态转移方程

```
agenda(DAG, I, d) = 

  ┌─────────────────────────────────────────────────────────┐
  │  if |DAG.nodes| == 0:                                   │
  │      return {}                                          │  ← Base Case 1: 空 DAG
  │                                                         │
  │  if |DAG.nodes| == 1 and (d >= MAX or not recursive):   │
  │      node = DAG.nodes[0]                                │  ← Base Case 2: 单节点
  │      return agent_run(node.prompt, node.inputs)         │     像一个普通 Agent
  │                                                         │
  │  for node in topological_sort(DAG):                     │  ← Recursive Step
  │      if node.needs_subtask:                             │
  │          sub_dag = node.generate_sub_dag()              │
  │          sub_outputs = agenda(sub_dag, node.inputs, d+1)│     递归调用！
  │          node.inputs.update(sub_outputs)                │
  │      run_node(node)                                     │
  │                                                         │
  │  return collect_outputs(DAG)                            │
  └─────────────────────────────────────────────────────────┘
```

---

## 3. Base Case = Normal Agent

### 3.1 什么是 "Normal Agent"

一个 Normal Agent 的行为：
```
输入：task + context
输出：result
状态：无（或只有当前对话的短期记忆）
并行：无（单线程执行）
调度：无（直接调用 LLM）
```

### 3.2 agenda 在 Base Case 时应退化为 Normal Agent

当 DAG 只有一个节点时，agenda 应该**完全退化为 Normal Agent**：

```
agenda(single_node_dag, inputs)
  │
  ├── ❌ 不需要拓扑排序（只有一个节点）
  ├── ❌ 不需要并行调度（只有一个节点）
  ├── ❌ 不需要 scheduler_state.json
  ├── ❌ 不需要 dep_inputs（没有依赖）
  │
  └── ✅ 直接：AgentLoop.run(system_prompt, task)
       ├── 读取 input/
       ├── 写入 workspace/
       ├── 写入 output/draft.md
       └── 返回结果
```

**关键洞察**：
> agenda 的 overhead（DAG 解析、拓扑排序、状态管理）只在 |nodes| > 1 时有价值。
> 当 |nodes| == 1 时，agenda 应该零 overhead 退化为 AgentLoop。

这意味着 agenda 的代码结构应该是：

```python
def agenda(dag, inputs, depth=0):
    if len(dag.nodes) <= 1:
        # 退化为 Normal Agent
        return agent_loop.run(dag.nodes[0].prompt, inputs)
    
    # DAG 调度逻辑
    return scheduler.run(dag)
```

---

## 4. Convergence = 等秩 or 降秩

### 4.1 降秩收敛（Merge Sort 模式）

每次递归，子问题的规模严格小于父问题：

```
父 DAG: 10 个节点
  ├─ 子 DAG A: 3 个节点
  ├─ 子 DAG B: 3 个节点
  └─ 子 DAG C: 4 个节点

子 DAG 节点数之和 ≈ 父 DAG 节点数
但每个子 DAG 的节点数 < 父 DAG
```

**性质**：
- 保证有限步内到达 Base Case
- 类似归并排序的 divide-and-conquer
- 适合"任务分解"场景

### 4.2 等秩收敛（Tree Traversal 模式）

每次递归，子问题的规模和父问题相同（秩不变），但深度递增：

```
父 DAG: 1 个节点（复杂任务）
  └─ 子 DAG: 1 个节点（子任务）
      └─ 孙 DAG: 1 个节点（子子任务）
          └─ ...

每个 DAG 都只有 1 个节点
但任务粒度越来越细
```

**性质**：
- 不保证有限步内终止（需要深度限制）
- 类似树的深度优先遍历
- 适合"任务细化"场景

### 4.3 混合模式（Practical 模式）

实际使用中，往往是混合：

```
父 DAG: 5 个节点（写书大纲）
  ├─ 节点 1: 写引言
  │     └─ run_dag() → 子 DAG: 3 个节点（研究+写作+润色） ← 降秩
  ├─ 节点 2: 写核心
  │     └─ run_dag() → 子 DAG: 1 个节点（直接写）        ← 等秩（到 Base Case）
  └─ 节点 3: 写结论
        └─ run_dag() → 子 DAG: 2 个节点（总结+润色）      ← 降秩
```

**收敛的保证**：
- 硬性：`depth >= MAX_DEPTH` 时强制退化为 Normal Agent
- 软性：Agent 生成的子 DAG 应该更小（靠 prompt 约束）

---

## 5. 正确性条件（类比递归函数终止性证明）

### 5.1 终止性（Termination）

要证明 `agenda(dag, inputs, d)` 一定终止，需要：

```
条件 1: 如果 d >= MAX_DEPTH，直接执行 AgentLoop（Base Case）✅
条件 2: 如果 |dag.nodes| == 0，返回空结果（Base Case）✅
条件 3: 如果 |dag.nodes| == 1，直接执行 AgentLoop（Base Case）✅
条件 4: 每次 run_dag 调用，d 严格递增（d → d+1）✅
条件 5: 每次 run_dag 调用，Agent 生成的子 DAG 不会更大（靠 prompt）⚠️
```

由条件 1 + 条件 4，即使条件 5 不满足，也能在 MAX_DEPTH 步后终止。

### 5.2 正确性（Correctness）

要证明 `agenda(dag)` 的输出是正确的，需要：

```
归纳假设：agenda(sub_dag) 对于所有 |sub_dag| < |dag| 都正确

归纳步骤：
  1. 拓扑排序保证依赖满足（先执行 dep，再执行 node）
  2. 每个节点在依赖完成后执行，input/ 包含所有前置产物
  3. 如果节点调用 run_dag，根据归纳假设，子 DAG 正确
  4. 所以当前 DAG 的所有节点都正确执行
  5. 收集 output/ 产物，返回正确结果

Base Case：
  |dag| == 1 时，直接执行 AgentLoop，假设 AgentLoop 正确
```

---

## 6. 对 agenda 设计的启示

### 6.1 单节点优化

当 DAG 只有一个节点时， agenda 应该**跳过调度器**：

```python
def agenda(dag, inputs, depth=0):
    if len(dag.nodes) <= 1:
        # 零 overhead 退化为 Normal Agent
        node = dag.nodes[0] if dag.nodes else None
        return run_agent(node, inputs)
    
    # 只有多节点时才启动 Scheduler
    scheduler = DAGScheduler(dag)
    return scheduler.run(inputs)
```

### 6.2 run_dag 的契约

run_dag 工具应该满足：

```
输入：dag_yaml（YAML 字符串）+ inputs（文件映射）
输出：产物内容（默认 output/draft.md 的文本）
约束：
  1. 子 DAG 的节点数 <= 父 DAG 的节点数（建议）
  2. 子 DAG 的任务粒度更细（建议）
  3. 深度超限时不允许调用（强制）
```

### 6.3 状态隔离

每个 agenda 调用是**纯函数**（输入 → 输出），没有副作用：

```
agenda(dag_1, inputs_1) → outputs_1
agenda(dag_2, inputs_2) → outputs_2

两个调用互不干扰（状态隔离在各自的 workspace/）
```

---

## 7. 总结

```
┌─────────────────────────────────────────────────────────────┐
│                    Agenda 递归函数                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Base Case:                                                 │
│    ├─ |nodes| == 0 → 返回 {}                                │
│    ├─ |nodes| == 1 → AgentLoop.run()（像 Normal Agent）      │
│    └─ depth >= MAX → AgentLoop.run()（强制终止递归）          │
│                                                             │
│  Recursive Step:                                            │
│    ├─ 拓扑排序                                              │
│    ├─ 并行调度                                              │
│    ├─ 节点执行中调用 run_dag()                               │
│    │       └─ agenda(sub_dag, sub_inputs, depth+1)          │
│    └─ 收集产物                                              │
│                                                             │
│  Convergence:                                               │
│    ├─ 硬性：depth 单调递增，MAX 强制终止                     │
│    └─ 软性：子 DAG 节点数 <= 父 DAG（Agent prompt 约束）     │
│                                                             │
│  正确性：                                                   │
│    ├─ 终止性：由 Base Case + depth 递增保证                  │
│    ├─ 正确性：由拓扑排序 + 归纳假设保证                       │
│    └─ 隔离性：每个 agenda 调用独立 workspace                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```
