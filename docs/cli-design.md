# Agenda CLI 设计文档

> CLI 是给另一个 Agent 用的接口，不是给人类用的交互式工具。

---

## 1. 核心问题

Agenda 的定位是 **"给 Agent 调度 Agent 的 runtime"**。这意味着：

- **调用方是另一个 Agent**（不是人类）
- **调用方式是 shell 命令**（不是 Python API）
- **输出需要机器可解析**（JSON / NDJSON，不是人类友好的排版）
- **错误码需要语义明确**（Agent 需要根据 exit code 判断下一步）

当前 CLI 的问题是：
1. `--workspace` + `--dag` 的组合不够直观，Agent 更习惯直接给文件路径
2. 缺少 `--json` 输出模式
3. 缺少 `--dry-run` 预演模式
4. 缺少 `deploy` 命令来部署一个 agent group
5. 缺少环境变量默认配置

---

## 2. 设计原则

| 原则 | 说明 |
|------|------|
| **一切皆可路径** | DAG 定义、模型配置、工作区，都应该用文件系统路径指定 |
| **一切皆可 JSON** | 每个命令都有 `--json` 模式，输出机器可解析的结构 |
| **环境变量兜底** | `AGENDA_DAG`、`AGENDA_MODELS` 等环境变量提供默认值 |
| **退出码语义化** | 0=成功，1=参数错误，2=DAG 配置错误，3=执行失败，4=依赖失败 |
| **自描述帮助** | `--help` 输出足够详细，Agent 可以通过 help 理解用法 |

---

## 3. CLI 命令设计

### 3.1 DAG 管理

```bash
# 创建 DAG（从模板或空白）
agenda dag init ./my_book_dag.yaml --from-template research-book-studio

# 验证 DAG（检查拓扑、模型配置、输入文件是否存在）
agenda dag validate ./my_book_dag.yaml --json
# 输出：
# {
#   "valid": true,
#   "nodes": 12,
#   "max_depth": 5,
#   "warnings": ["ch09_compare 的 dep ch06_openclaw 不存在"],
#   "models": ["deepseek", "kimi", "claude"]
# }

# 查看 DAG 拓扑结构
agenda dag inspect ./my_book_dag.yaml --json
# 输出：
# {
#   "nodes": {
#     "ch01_intro": {"deps": [], "model": "deepseek", "status": "PENDING"},
#     "ch03_hermes": {"deps": ["ch01_intro"], "model": "kimi", "status": "PENDING"},
#     "ch09_compare": {"deps": ["ch03_hermes", "ch06_openclaw"], "model": "claude", "status": "PENDING"}
#   },
#   "critical_path": ["ch01_intro", "ch03_hermes", "ch09_compare"]
# }

# 运行整个 DAG
agenda dag run ./my_book_dag.yaml --models ~/.agenda/models.yaml --max-parallel 4

# 查看运行状态
agenda dag status ./my_book_dag.yaml --json
# 输出：
# {
#   "dag": "my_book",
#   "completed": 3,
#   "total": 12,
#   "running": ["ch04_hermes"],
#   "failed": ["ch06_openclaw"],
#   "pending": ["ch09_compare", "ch10_compare_exp"],
#   "estimated_remaining_seconds": 480
# }

# 实时监听状态变化（--watch 模式，SSE over stdout）
agenda dag status ./my_book_dag.yaml --watch --json
# 每秒钟输出一行 NDJSON：
# {"timestamp": "2026-04-24T12:00:01Z", "event": "node_started", "node": "ch04_hermes"}
# {"timestamp": "2026-04-24T12:02:15Z", "event": "node_completed", "node": "ch04_hermes"}
# {"timestamp": "2026-04-24T12:02:16Z", "event": "node_ready", "node": "ch05_hermes_limits"}

# 停止正在运行的 DAG
agenda dag stop ./my_book_dag.yaml
```

### 3.2 节点管理

```bash
# 运行单个节点（调试用）
agenda node run ./my_book_dag.yaml --node ch03_hermes --force

# 重置节点（删除产物，让它重新跑）
agenda node reset ./my_book_dag.yaml --node ch03_hermes

# 查看节点日志
agenda node logs ./my_book_dag.yaml --node ch03_hermes --tail 50

# 查看节点对话历史
agenda node history ./my_book_dag.yaml --node ch03_hermes --json

# 注入记忆到节点
agenda node inject ./my_book_dag.yaml --node ch03_hermes --memory "注意：Hermes 的学习循环是核心卖点"
```

### 3.3 模型管理

```bash
# 列出可用模型
agenda models list --config ~/.agenda/models.yaml --json
# 输出：
# [
#   {"name": "deepseek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "token_cap": 64000},
#   {"name": "kimi", "model": "moonshot-v1-8k", "base_url": "https://api.moonshot.cn/v1", "token_cap": 8000}
# ]

# 验证模型配置（测试 API 连通性）
agenda models validate --config ~/.agenda/models.yaml

# 添加模型（交互式或命令行）
agenda models add kimi \
  --base-url https://api.moonshot.cn/v1 \
  --api-key "${KIMI_API_KEY}" \
  --model moonshot-v1-8k \
  --token-cap 8000
```

### 3.4 Agent Group 部署

```bash
# 部署一个 agent group（把 DAG 包装成长期运行的服务）
agenda deploy ./my_book_dag.yaml \
  --name book_writing_group \
  --models ~/.agenda/models.yaml \
  --auto-restart \
  --log-dir ./logs

# 查看部署的 agent groups
agenda deploy list --json

# 停止一个 agent group
agenda deploy stop book_writing_group

# 查看 agent group 日志
agenda deploy logs book_writing_group --follow
```

---

## 4. 环境变量

```bash
# 默认 DAG 路径
export AGENDA_DAG="./workspace/my_book/dag.yaml"

# 默认模型配置路径
export AGENDA_MODELS="~/.agenda/models.yaml"

# 默认工作目录
export AGENDA_WORKSPACE="./workspace"

# 默认最大并行度
export AGENDA_MAX_PARALLEL="4"

# 设置后，命令可以简化为：
agenda dag run              # 使用 AGENDA_DAG
agenda dag status --json    # 使用 AGENDA_DAG
agenda models list          # 使用 AGENDA_MODELS
```

---

## 5. 输出格式

### 5.1 人类可读模式（默认）

```
$ agenda dag status ./my_book_dag.yaml

DAG: my_book
━━━━━━━━━━━━━━━━━━━━
总节点: 12
已完成: 3
运行中: 1 (ch04_hermes)
失败: 1 (ch06_openclaw)
等待中: 7
━━━━━━━━━━━━━━━━━━━━

运行中:
  ch04_hermes  ⏳ 模型: kimi  已运行: 2m15s

失败:
  ch06_openclaw  ❌ 模型: claude  错误: API 超时

等待中:
  ch09_compare      📋 模型: claude  依赖: ch03_hermes ✓, ch06_openclaw ✗
  ch10_compare_exp  📋 模型: deepseek
```

### 5.2 JSON 模式（`--json`）

```json
{
  "dag": "my_book",
  "path": "./workspace/my_book/dag.yaml",
  "completed": 3,
  "total": 12,
  "running": [
    {"node": "ch04_hermes", "model": "kimi", "started_at": "2026-04-24T12:00:00Z", "elapsed_seconds": 135}
  ],
  "failed": [
    {"node": "ch06_openclaw", "model": "claude", "error": "API timeout", "failed_at": "2026-04-24T11:58:00Z"}
  ],
  "pending": [
    {"node": "ch09_compare", "model": "claude", "ready": false, "missing_deps": ["ch06_openclaw"]},
    {"node": "ch10_compare_exp", "model": "deepseek", "ready": true}
  ]
}
```

### 5.3 Watch 模式（`--watch --json`）

NDJSON（每行一个 JSON 对象，适合流式解析）：

```ndjson
{"ts": "2026-04-24T12:00:01Z", "type": "node_started", "node": "ch04_hermes", "model": "kimi"}
{"ts": "2026-04-24T12:02:15Z", "type": "node_completed", "node": "ch04_hermes", "model": "kimi", "output_size": 15234}
{"ts": "2026-04-24T12:02:16Z", "type": "node_ready", "node": "ch05_hermes_limits", "model": "kimi"}
{"ts": "2026-04-24T12:02:17Z", "type": "dag_progress", "completed": 4, "total": 12, "percentage": 33.3}
```

---

## 6. 退出码

| 退出码 | 含义 | 示例 |
|--------|------|------|
| 0 | 成功 | `dag run` 全部完成 |
| 1 | 参数/命令错误 | `--node` 缺少值，文件不存在 |
| 2 | DAG 配置错误 | 循环依赖，模型别名未定义，输入文件缺失 |
| 3 | 节点执行失败 | LLM API 错误，tool 执行异常 |
| 4 | 依赖失败导致无法继续 | 上游节点失败，下游节点无法启动 |
| 130 | 用户中断 | Ctrl+C |

---

## 7. 给 Agent 用的 Shell 脚本示例

```bash
#!/bin/bash
# 这是一个 Agent 调用 Agenda 的示例脚本

set -e

DAG="./workspace/my_book/dag.yaml"
MODELS="~/.agenda/models.yaml"

# 1. 验证 DAG
echo "[1/4] 验证 DAG..."
agenda dag validate "$DAG" --json | jq -e '.valid == true' || exit 2

# 2. 检查模型
echo "[2/4] 检查模型配置..."
agenda models validate --config "$MODELS" || exit 2

# 3. 运行 DAG
echo "[3/4] 运行 DAG..."
agenda dag run "$DAG" --models "$MODELS" --max-parallel 4 || {
  CODE=$?
  echo "[错误] DAG 运行失败，退出码: $CODE"
  
  # 查看失败节点
  FAILED=$(agenda dag status "$DAG" --json | jq -r '.failed[].node')
  for node in $FAILED; do
    echo "--- $node 错误日志 ---"
    agenda node logs "$DAG" --node "$node" --tail 20
  done
  
  exit $CODE
}

# 4. 收集产物
echo "[4/4] 收集产物..."
agenda dag status "$DAG" --json | jq '.completed'
```

---

## 8. 关键决策

### 8.1 为什么不用 workspace + dag_name，而用 dag.yaml 路径？

Agent 更习惯直接操作文件路径。`./workspace/my_book/dag.yaml` 比 `--workspace ./workspace --dag my_book` 更直观，也更符合 Unix 哲学。

### 8.2 为什么每个命令都有 `--json`？

因为调用方是 Agent，不是人类。Agent 需要解析输出来做决策（比如"如果 failed 不为空，就重试失败节点"）。JSON 是机器最友好的格式。

### 8.3 为什么有 `deploy` 命令？

Agent Group 不是"跑一次就完"的 DAG，而是"长期运行的服务"。比如：
- 每天自动生成日报
- 监控代码仓库，PR 来时自动 review
- 24/7 运行的客服 agent

`deploy` 把 DAG 包装成 daemon，支持 auto-restart 和 log rotation。

---

*文档版本：v0.1*  
*讨论日期：2026-04-24*
