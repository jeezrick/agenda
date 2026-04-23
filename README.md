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

# 2. 创建模型配置
mkdir -p ~/.agenda
cat > ~/.agenda/models.yaml << 'EOF'
models:
  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    model: "deepseek-chat"
    token_cap: 64000

  kimi:
    base_url: "https://api.moonshot.cn/v1"
    api_key: "${KIMI_API_KEY}"
    model: "moonshot-v1-8k"
    token_cap: 8000

  claude:
    base_url: "https://api.anthropic.com/v1"
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-3-5-sonnet"
    token_cap: 200000
EOF

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
from agenda import DAGScheduler, build_tools

async def main():
    scheduler = DAGScheduler("workspace", "my_book").load()
    
    results = await scheduler.run(
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
    model: "deepseek"          # 使用 deepseek 模型（在 models.yaml 中定义）
    prompt: "写第一章：Agent 爆发背景"
    inputs:
      - "meta/outline.md"
    output: "output/draft.md"

  ch03_hermes:
    model: "kimi"              # Hermes 章节用 Kimi
    prompt: "写第三章：Hermes Agent 深度解析"
    deps: [ch01_intro]
    inputs:
      - "meta/outline.md"
    dep_inputs:
      - from: "ch01_intro/output/draft.md"
        to: "input/deps/ch01_intro/draft.md"
    output: "output/draft.md"

  ch09_compare:
    model: "claude"            # 对比章节用 Claude（推理更强）
    prompt: "写第九章：架构对比"
    deps: [ch03_hermes, ch06_openclaw]
    dep_inputs:
      - from: "ch03_hermes/output/draft.md"
        to: "input/deps/ch03_hermes/draft.md"
      - from: "ch06_openclaw/output/draft.md"
        to: "input/deps/ch06_openclaw/draft.md"
    output: "output/draft.md"
```

### 多模型配置

在 `~/.agenda/models.yaml` 或 DAG 工作区的 `models.yaml` 中定义：

```yaml
models:
  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"   # 支持环境变量引用
    model: "deepseek-chat"
    token_cap: 64000

  kimi:
    base_url: "https://api.moonshot.cn/v1"
    api_key: "${KIMI_API_KEY}"
    model: "moonshot-v1-8k"
    token_cap: 8000

  claude:
    base_url: "https://api.anthropic.com/v1"
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-3-5-sonnet"
    token_cap: 200000
```

DAG 节点通过 `model: "alias"` 指定使用哪个模型。不同节点可以并行使用不同厂商的 API。

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
