# Agenda

> 给 Agent 调度 Agent 的极简运行时。

Agenda 是一个原生支持 DAG（有向无环图）的 Agent 运行时。它不是给人类用的交互式工具（如 Claude Code），而是给 **Agent 编排 Agent** 的基础设施。

设计哲学：**文件即状态，目录即 Session，DAG 即编排，Hook 即策略。**

---

## 特性

- **DAG 原生**：YAML 定义节点和依赖，自动拓扑排序 + 并行调度
- **文件系统即状态**：没有数据库，没有 socket，所有状态在文件系统里
- **双目录隔离**：`.context/`（Agent 可见）和 `.system/`（系统私有）
- **AI 自压缩记忆**：Token 满时注入《紧急危机》prompt，让 Agent 自己归档
- **Hook 机制**：在 Agent 循环的关键节点注入策略，不改源码
- **单文件部署**：`agenda.py` 复制粘贴即可运行

---

## 快速开始

```bash
# 1. 复制文件
wget https://raw.githubusercontent.com/jeezrick/agenda/main/agenda.py

# 2. 设置环境变量
export AGENDA_API_KEY="sk-..."
export AGENDA_BASE_URL="https://api.deepseek.com/v1"
export AGENDA_MODEL="deepseek-chat"

# 3. 初始化 DAG 工作区
python agenda.py init --workspace ./workspace --dag my_book

# 4. 编辑 DAG 定义
# workspace/my_book/dag.yaml

# 5. 运行
python agenda.py run
```

---

## Python API

```python
import asyncio
from openai import AsyncOpenAI
from agenda import DAGScheduler, build_tools

async def main():
    client = AsyncOpenAI(api_key="sk-...")
    scheduler = DAGScheduler("workspace", "my_book").load()
    
    results = await scheduler.run(
        llm_client=client,
        tools_factory=lambda session: build_tools(session),
    )
    print(results)
    # {"ch01_intro": "COMPLETED", "ch03_hermes": "COMPLETED", ...}

asyncio.run(main())
```

---

## DAG 定义示例

```yaml
dag:
  name: "Hermes vs OpenClaw"
  max_parallel: 4

nodes:
  ch01_intro:
    prompt: "写第一章：Agent 爆发背景"
    inputs:
      - "meta/outline.md"
    output: "output/draft.md"

  ch03_hermes:
    prompt: "写第三章：Hermes Agent 深度解析"
    deps: [ch01_intro]
    inputs:
      - "meta/outline.md"
    dep_inputs:
      - from: "ch01_intro/output/draft.md"
        to: "input/deps/ch01_intro/draft.md"
    output: "output/draft.md"

  ch09_compare:
    prompt: "写第九章：架构对比"
    deps: [ch03_hermes, ch06_openclaw]
    dep_inputs:
      - from: "ch03_hermes/output/draft.md"
        to: "input/deps/ch03_hermes/draft.md"
      - from: "ch06_openclaw/output/draft.md"
        to: "input/deps/ch06_openclaw/draft.md"
    output: "output/draft.md"
```

---

## 文档

- [设计文档](docs/agenda-design.md) — 完整架构设计
- [架构决策](docs/multi-agent-runtime-ad.md) — Butterfly vs EVA 分析

---

## 设计来源

Agenda 吸取了以下两个优秀项目的精华：

- **Butterfly Agent**（https://github.com/dannyxiaocn/butterfly-agent）：双目录 Session、Meta Session、Hook 机制、文件系统 IPC
- **EVA**（https://github.com/usepr/eva）：极简主义、AI 自压缩记忆、LLM 安全审查、单文件部署

---

## License

MIT
