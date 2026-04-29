# Agenda — Agent 使用手册

> 一句话：Agenda 是一个给 **Agent 调度 Agent** 的 DAG 运行时。你把任务写成 DAG YAML，Agenda 自动并行调度、执行、传递产物。

---

## 安装

```bash
pip install -e .
```

安装后 `agenda` 命令可用：

```bash
agenda --version   # 0.0.6
```

---

## 环境变量

| 变量 | 用途 | 示例 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | `sk-...` |
| `AGENDA_DAG` | 默认 DAG 路径 | `./myproject/dag.yaml` |
| `AGENDA_MODELS` | 默认模型配置路径 | `./myproject/models.yaml` |
| `AGENDA_MAX_PARALLEL` | 默认最大并行度 | `4` |

模型配置默认读取 `~/.agenda/models.yaml`，也可以在 DAG 工作区放 `models.yaml` 覆盖。

---

## 常用命令速查

### 1. 初始化工作区

```bash
agenda dag init ./myproject
```

创建 `dag.yaml` + `models.yaml` 模板。

### 2. 验证 DAG

```bash
agenda dag validate ./myproject/dag.yaml
```

输出 JSON：
- `valid: true/false`
- `errors`: 配置错误列表（循环依赖、缺失节点等）
- `warnings`: 警告列表（输入文件不存在等）

### 3. 运行 DAG

```bash
agenda dag run ./myproject/dag.yaml
```

输出 JSON：
- `results: {node_id: "COMPLETED"|"FAILED"|"PENDING"}`

选项：
- `--models path/to/models.yaml` 指定模型配置
- `--max-parallel 4` 指定并行度
- `--dry-run` 只打印拓扑排序，不执行

### 4. 查看状态

```bash
agenda dag status ./myproject/dag.yaml
```

输出 JSON：
- `completed`, `failed`, `running`, `pending` 节点列表
- `progress`: 完成进度（如 `"1/3"`）

Watch 模式（每秒刷新）：
```bash
agenda dag status ./myproject/dag.yaml --watch
```

### 5. 运行单个节点

```bash
agenda node run ./myproject/dag.yaml --node outline
```

选项：
- `--force` 强制重置节点（删除历史重新运行）

### 6. 查看节点日志

```bash
agenda node logs ./myproject/dag.yaml --node outline
```

输出 JSON：
- `status`: running/completed/failed
- `error_log`: 错误日志内容（如果有）
- `output_exists`: output/draft.md 是否存在

### 7. 重置节点

```bash
agenda node reset ./myproject/dag.yaml --node outline
```

删除节点目录，下次运行时从头开始。

### 8. 模型管理

```bash
agenda models list              # 列出可用模型
agenda models validate          # 验证模型配置（检查 api_key 等）
agenda models list --config ./custom/models.yaml
```

### 9. 审批管理

当节点配置了 `approval_required: true` 时，工具调用需要人工批准：

```bash
agenda node approve ./myproject/dag.yaml --node outline
agenda node reject ./myproject/dag.yaml --node outline
```

---

## DAG YAML 格式

### 极简模板

```yaml
dag:
  name: "任务名称"
  max_parallel: 4

nodes:
  node_a:
    prompt: |
      你的任务描述。Agent 会用 tools 执行。
      完成后把结果写入 output/draft.md。
    model: "deepseek-flash"

  node_b:
    prompt: |
      读取 .context/deps/node_a.md 的内容，写一段评论。
      保存到 output/draft.md。
    model: "deepseek-flash"
    deps: [node_a]
    dep_inputs:
      - from: "nodes/node_a/output/draft.md"
        to: "deps/node_a.md"
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `dag.name` | 否 | DAG 名称 |
| `dag.max_parallel` | 否 | 最大并行节点数，默认 4 |
| `nodes.<id>.prompt` | **是** | 发给 Agent 的任务描述 |
| `nodes.<id>.model` | 否 | 模型别名（需在 models.yaml 中定义） |
| `nodes.<id>.deps` | 否 | 依赖节点 ID 列表 |
| `nodes.<id>.inputs` | 否 | 从 DAG 根目录复制到节点 input/ 的文件 |
| `nodes.<id>.dep_inputs` | 否 | 从上游节点复制产物（`from` 相对 DAG 根，`to` 相对节点 input/） |
| `nodes.<id>.max_iterations` | 否 | Agent 最大迭代轮数，默认 50 |
| `nodes.<id>.timeout` | 否 | 节点超时秒数，默认 600 |
| `nodes.<id>.retries` | 否 | 失败重试次数，默认 3 |
| `nodes.<id>.stream` | 否 | 是否启用流式输出，默认 true |
| `nodes.<id>.output_schema` | 否 | JSON Schema 定义输出格式，自动校验 |
| `nodes.<id>.approval_required` | 否 | 工具调用是否需要人工审批，默认 false |
| `nodes.<id>.approval_tools` | 否 | 需要审批的工具列表，默认所有工具 |
| `nodes.<id>.approval_timeout` | 否 | 审批超时秒数，默认 300 |

### 产物规则

- **完成标记**：Agent 写入 `output/draft.md` 即表示完成
- **自定义标记**：`done_file: "output/report.md"`
- **下游读取**：通过 `dep_inputs` 把上游 `output/` 复制到下游 `input/`

### 示例：结构化输出 + 审批

```yaml
dag:
  name: "数据分析"
  max_parallel: 2

nodes:
  analyze:
    prompt: "分析 input/data.csv，输出结构化结果"
    model: "deepseek-pro"
    output_schema:
      type: object
      properties:
        summary: {type: string}
        key_metrics: {type: array, items: {type: number}}
        recommendations: {type: array, items: {type: string}}

  deploy:
    prompt: "读取分析结果，执行部署脚本"
    model: "deepseek-flash"
    deps: [analyze]
    approval_required: true
    approval_tools: ["run_shell"]
    dep_inputs:
      - from: "nodes/analyze/output/draft.md"
        to: "analysis.json"
```

### 示例：并行 + 汇聚

```yaml
dag:
  name: "研究报告"
  max_parallel: 4

nodes:
  collect:
    prompt: "收集 5 个 AI Agent 信息源，写入 output/sources.md"
    model: "deepseek-flash"

  analyze_tech:
    prompt: "读取 .context/sources.md，分析技术趋势"
    model: "deepseek-pro"
    deps: [collect]
    dep_inputs:
      - from: "nodes/collect/output/sources.md"
        to: "sources.md"

  analyze_biz:
    prompt: "读取 .context/sources.md，分析商业机会"
    model: "deepseek-pro"
    deps: [collect]
    dep_inputs:
      - from: "nodes/collect/output/sources.md"
        to: "sources.md"

  report:
    prompt: "读取 trends.md 和 biz.md，写综合报告"
    model: "deepseek-flash"
    deps: [analyze_tech, analyze_biz]
    dep_inputs:
      - from: "nodes/analyze_tech/output/draft.md"
        to: "trends.md"
      - from: "nodes/analyze_biz/output/draft.md"
        to: "biz.md"
```

---

## Exit Code

| Code | 含义 | 何时出现 |
|---|---|---|
| `0` | 成功 | DAG 全部完成、validate 通过、status 查询成功 |
| `1` | 参数错误 | 缺少 DAG 路径、未知命令、参数格式错误 |
| `2` | DAG 配置错误 | validate 失败（循环依赖、缺失节点等） |
| `3` | 执行错误 | 节点运行失败（API 错误、tool 异常、超时） |
| `4` | 依赖失败 | 上游节点失败导致下游阻塞 |
| `130` | 用户中断 | Ctrl+C |

---

## 常见错误与解决

### 1. `未指定 DAG 路径`

```
{"error": "未指定 DAG 路径。提供路径或设置 AGENDA_DAG"}
```

**解决**：提供路径参数，或设置环境变量 `export AGENDA_DAG=./myproject/dag.yaml`

### 2. `DAG 文件不存在`

```
{"error": "DAG 文件不存在: ... (也不是包含 dag.yaml 的目录)"}
```

**解决**：确认路径正确。可以传目录（Agenda 会找 `dag.yaml`）或传文件路径。

### 3. Validate 失败：`循环依赖`

```
{"valid": false, "errors": ["循环依赖: a -> b -> a"]}
```

**解决**：检查 `deps` 是否形成环。DAG 必须是无环有向图。

### 4. Validate 失败：`节点 X 依赖不存在的节点`

```
{"valid": false, "errors": ["节点 X 依赖不存在的节点: ghost"]}
```

**解决**：检查 `deps` 列表中的 ID 是否在 `nodes` 中定义。

### 5. 节点 FAILED

```
{"results": {"node_a": "FAILED"}}
```

**排查**：
```bash
agenda node logs ./myproject/dag.yaml --node node_a
```

常见原因：
- API key 无效 → 检查 `models.yaml` 中 `api_key`
- 模型不存在 → 检查 `model` 别名是否在 `models.yaml` 中定义
- prompt 没告诉 Agent 输出文件 → prompt 中明确写 "写入 output/draft.md"

### 6. `节点运行但 output/draft.md 不存在`

**解决**：prompt 中必须明确告诉 Agent 把最终产物写入 `output/draft.md`。Agent 可用的工具有 `read_file`、`write_file`、`list_dir`。

---

## 工作流（Agent 推荐步骤）

1. **初始化**：`agenda dag init ./project`
2. **写 DAG**：编辑 `dag.yaml` 定义节点和依赖
3. **验证**：`agenda dag validate ./project/dag.yaml`
4. **运行**：`agenda dag run ./project/dag.yaml`
5. **查看状态**：`agenda dag status ./project/dag.yaml`
6. **排障**：`agenda node logs ./project/dag.yaml --node <id>`
