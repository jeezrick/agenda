# Agenda 递归设计 — Mermaid 可视化

## 1. 递归调用树（每个节点 = agenda 调用）

```mermaid
graph TD
    subgraph "agenda(depth=0, 3 nodes)"
        direction TB
        A0["agenda(dag=[大纲,Ch1,Ch2], inputs)"] --> S0{"len(dag.nodes)==1?"}
        S0 -->|NO| Sch0["Scheduler.run()"]
        Sch0 --> AL0a["AgentLoop(大纲)<br/>Base Case → 叶子"]
        Sch0 --> AL0b["AgentLoop(Ch1)"]
        Sch0 --> AL0c["AgentLoop(Ch2)<br/>Base Case → 叶子"]
    end

    subgraph "agenda(depth=1, 2 nodes)"
        direction TB
        A1["agenda(dag=[1.1,1.2], inputs)"] --> S1{"len(dag.nodes)==1?"}
        S1 -->|NO| Sch1["Scheduler.run()"]
        Sch1 --> AL1a["AgentLoop(1.1)<br/>Base Case → 叶子"]
        Sch1 --> AL1b["AgentLoop(1.2)"]
    end

    subgraph "agenda(depth=2, 1 node)"
        direction TB
        A2["agenda(dag=[1.2.1], inputs)"] --> S2{"len(dag.nodes)==1?"}
        S2 -->|YES| BC2["AgentLoop(1.2.1)<br/>Base Case → 叶子"]
    end

    AL0b --> A1
    AL1b --> A2

    style A0 fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
    style A1 fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
    style A2 fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style Sch0 fill:#fff3e0,stroke:#ff9800,stroke-width:2px
    style Sch1 fill:#fff3e0,stroke:#ff9800,stroke-width:2px
    style AL0a fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style AL0c fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style AL1a fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style BC2 fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
```

**图例：**
- 🔵 蓝框 = `agenda()` 调用
- 🟠 橙框 = `Scheduler.run()`（仅在 nodes > 1 时出现）
- 🟢 绿框 = `AgentLoop.run()` Base Case（叶子节点）

**关键：** 每个 agenda 节点内部先判断 `len(dag.nodes)==1`。如果是 → 直接 AgentLoop（叶子）。如果不是 → 启动 Scheduler，Scheduler 启动多个 AgentLoop，某些 AgentLoop 可能继续调用 agenda()。

---

## 2. 单个 agenda 调用的内部结构

```mermaid
graph LR
    A["agenda(dag, inputs, depth)"] --> B{"len(dag.nodes) == 1?"}
    B -->|YES| C["Base Case:<br/>AgentLoop.run()<br/>→ 叶子"]
    B -->|NO| D["Recursive Step:<br/>Scheduler.run()"]
    D --> E["AgentLoop(node_1)"]
    D --> F["AgentLoop(node_2)"]
    D --> G["AgentLoop(node_3)"]
    E --> H{"调用 agenda()?"}
    F --> I{"调用 agenda()?"}
    G --> J{"调用 agenda()?"}
    H -->|YES| K["agenda(sub_dag, ...)<br/>depth+1"]
    H -->|NO| L["叶子"]
    I -->|NO| M["叶子"]
    J -->|NO| N["叶子"]

    style A fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
    style C fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style L fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style M fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style N fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    style K fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
```

---

## 3. Sequence Diagram（完整调用链）

```mermaid
sequenceDiagram
    participant M as Meta Agent
    participant A0 as agenda(depth=0)<br/>3 nodes
    participant S0 as Scheduler
    participant AL1 as AgentLoop(大纲)<br/>Base Case
    participant AL2 as AgentLoop(Ch1)
    participant A1 as agenda(depth=1)<br/>2 nodes
    participant S1 as Scheduler
    participant AL3 as AgentLoop(1.1)<br/>Base Case
    participant AL4 as AgentLoop(1.2)
    participant A2 as agenda(depth=2)<br/>1 node
    participant AL5 as AgentLoop(1.2.1)<br/>Base Case
    participant AL6 as AgentLoop(Ch2)<br/>Base Case

    M->>A0: agenda(root_dag, inputs)
    Note over A0: len(dag)==3 → Scheduler
    A0->>S0: run()

    S0->>AL1: AgentLoop.run()
    AL1-->>S0: return output/

    S0->>AL2: AgentLoop.run()
    Note over AL2: 构造 sub_dag=[1.1,1.2]<br/>调用 agenda()
    AL2->>A1: agenda(sub_dag, inputs)

    Note over A1: len(dag)==2 → Scheduler
    A1->>S1: run()

    S1->>AL3: AgentLoop.run()
    AL3-->>S1: return output/

    S1->>AL4: AgentLoop.run()
    Note over AL4: 构造 sub_dag=[1.2.1]<br/>调用 agenda()
    AL4->>A2: agenda(sub_dag, inputs)

    Note over A2: len(dag)==1 → Base Case
    A2->>AL5: AgentLoop.run()
    AL5-->>A2: return output/
    A2-->>AL4: return

    AL4-->>S1: return output/
    S1-->>A1: return merged
    A1-->>AL2: return

    AL2-->>S0: return output/

    S0->>AL6: AgentLoop.run()
    AL6-->>S0: return output/

    S0-->>A0: return merged
    A0-->>M: return final
```

---

## 4. 传统递归 vs Agenda 递归（同构性）

```mermaid
graph LR
    subgraph "传统递归: fibonacci(n)"
        F1["fib(5)"] --> F2["fib(4)"]
        F1 --> F3["fib(3)"]
        F2 --> F4["fib(3)"]
        F2 --> F5["fib(2) → Base Case"]
        F3 --> F6["fib(2) → Base Case"]
        F3 --> F7["fib(1) → Base Case"]
    end

    subgraph "Agenda 递归: agenda(dag)"
        A1["agenda(3 nodes)"] --> A2["agenda(2 nodes)"]
        A1 --> A3["AgentLoop → Base Case"]
        A2 --> A4["AgentLoop → Base Case"]
        A2 --> A5["agenda(1 node) → Base Case"]
    end
```

**同构性：** 两者都是树形递归。传统递归由函数参数驱动（n-1, n-2），Agenda 递归由 Agent 构造的 DAG 驱动。

---

## 5. Agent Loop 视角（位置透明性）

```mermaid
graph LR
    subgraph "Agent Loop 看到的世界"
        T["Task: 写第一章"]
        W["Workspace: input/ workspace/ output/"]
        TOOLS["可调用的函数:<br/>- read_file<br/>- write_file<br/>- agenda(dag, inputs)"]
    end

    T --> DECISION{"需要分解?"}
    DECISION -->|是| A["构造 sub_dag<br/>调用 agenda()"]
    DECISION -->|否| E["直接执行<br/>输出到 output/"]

    style A fill:#e3f2fd,stroke:#2196f3,stroke-width:2px
```

**Agent Loop 不知道：** 自己是 depth=0 还是 depth=5，外面还有没有更大的 DAG，调用 `agenda()` 会触发什么。它只看到一个 task、一个 workspace、一组可调用的函数（含 `agenda()`）。
