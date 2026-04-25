# Agenda 作为递归函数的学术评估

## 1. 核心创新点

agenda 的独特组合：

| 特性 | 已有系统 | agenda |
|------|---------|--------|
| DAG 编排 | LangGraph, CrewAI, Prefect | ✅ |
| 递归调用 | AutoGen (nested chat) | ✅ |
| Workspace 隔离 | Butterfly (session) | ✅ |
| Agent-facing | AutoGen, CrewAI | ✅ |
| **四者结合** | **❌ 没有** | **✅ 首次** |

## 2. 与现有工作的对比

### 2.1 学术界

**Multi-Agent Systems:**
- **AutoGen** (Microsoft, 2023): 对话式 multi-agent，支持 nested chat（递归对话），但没有 DAG 编排，没有 workspace 隔离
- **MetaGPT**: SOP-based，角色分工，不递归，不隔离
- **CAMEL**: Role-playing，无 DAG，无递归
- **AgentVerse**: 群体协作，无 DAG 递归

**DAG Orchestration:**
- **LangGraph**: DAG + 循环 + 条件分支，但无递归（子 Graph 不能作为节点），state 共享不隔离
- **CrewAI**: 简单 task DAG，无递归，无隔离
- **Prefect/Airflow**: 传统工作流，非 Agent 专用

**Hierarchical Multi-Agent:**
- 有一些分层 multi-agent RL 的工作，但面向的是强化学习，不是 LLM Agent orchestration
- 没有"递归函数"视角的 DAG orchestrator

### 2.2 工业界

- **Kimi Code CLI**: 有 subagent（前台/后台），无 DAG
- **Claude Code**: 无内置 DAG/subagent
- **OpenAI Swarm**: 轻量级多 Agent，无 DAG
- **Dagster/Prefect**: 数据管道，非 Agent，无递归

## 3. 创新级别评估

### 3.1 工程架构 Insight（高价值）

- **递归心智模型**: 把 multi-agent orchestration 统一为函数递归，极大简化设计
- **Base Case 退化**: 单节点时零 overhead 退化为 Normal Agent，优雅
- **文件系统即状态**: workspace 隔离，调试友好，恢复简单

### 3.2 学术贡献（中等）

- **视角新颖**: "orchestrator as recursive function" 是新的 framing
- **但不够硬**: 缺少形式化定义、终止性证明、复杂度分析
- **缺少实验**: 没有与现有系统的对比评估

## 4. 投顶会可行性

### 4.1 直接投顶会（NeurIPS/ICML/ACL）— 难度：高 ❌

需要的补充：
1. **形式化框架**: 递归 orchestration 的数学定义
2. **理论分析**: 终止性证明、复杂度分析、正确性证明
3. **大规模实验**: 与 LangGraph/AutoGen/CrewAI 的对比（效率、正确性、可扩展性）
4. **应用场景**: 至少 3 个真实场景的评估（代码生成、数据分析、内容创作）

### 4.2 Workshop / Demo Track — 难度：中 ✅

- **NeurIPS/ICML Workshop on LLM Agents** — 有专门的 workshop
- **ACL Demo Track** — 演示新工具
- **arXiv + 开源** — 先建立影响力

### 4.3 系统会议 — 难度：中高 ⚠️

- **MLSys**: 机器学习系统，可能合适
- **ICSE/ESEC-FSE**: 软件工程，接受开发工具
- 但需要更多工程深度（分布式、性能优化、容错）

## 5. 建议路径

```
Phase 1: 工程实现（现在）
  ├── 完成 agenda v0.1
  ├── 写技术博客
  └── 开源 GitHub

Phase 2: 建立影响力（3-6 个月）
  ├── arXiv preprint
  ├── 社区反馈
  └── 与 LangGraph/AutoGen 做对比实验

Phase 3: 学术包装（6-12 个月）
  ├── 补充形式化定义
  ├── 大规模评估
  └── 投 Workshop / Demo Track

Phase 4: 扩展（可选）
  ├── 分布式调度
  ├── 形式化验证
  └── 投主会
```

## 6. 结论

| 维度 | 评估 |
|------|------|
| 创新性 | 中-高（组合创新，视角新颖） |
| 学术价值 | 中（更适合工程架构 insight） |
| 工程价值 | 高（心智模型简单，实现优雅） |
| 顶会可行性 | 低（需要大量补充工作） |
| 最佳出路 | **工程开源项目 + 技术博客** |

**这不是一个"投顶会"的 idea，而是一个"好工程"的 idea。**

它的价值在于：
1. 简化了 multi-agent orchestration 的心智模型
2. 提供了一种统一的递归视角
3. 文件系统即状态的设计非常实用

但如果要包装成论文，核心卖点应该是：
> "我们提出了递归 DAG orchestration 的框架，证明了其终止性和正确性，并在 X/Y/Z 场景下展示了相对于现有系统的优势"

这需要至少 3-6 个月的额外工作。
