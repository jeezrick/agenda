# Agenda DAG 格式规范（给 Meta Agent）

> Meta Agent 用此格式描述任务依赖图，Agenda 自动调度执行。

## 核心原则

- **一个文件 = 一个 DAG**：`dag.yaml`
- **一个节点 = 一个 Agent Session**：独立的 `.context/` 和 `output/`
- **依赖自动传递产物**：上游 `output/` 自动映射到下游 `.context/`
- **YAML 或 JSON 均可**：Meta Agent 推荐生成 JSON，更不容易出错

---

## 极简格式

```yaml
dag:
  name: "任务名称"          # 可选，默认 "untitled"
  max_parallel: 4           # 可选，默认 4

nodes:
  node_id_1:
    prompt: |
      你的任务描述。Agent 会把这个作为用户消息发给 LLM。
      支持多行。Agent 可以用 read_file/write_file/list_dir 操作文件。
    model: "模型别名"       # 可选，默认第一个可用模型
    deps: [node_id_2]       # 可选，依赖的节点 ID 列表
    inputs: ["input.md"]    # 可选，复制到 .context/ 的输入文件
    dep_inputs:             # 可选，从上游节点复制产物
      - from: "node_id_2/output/draft.md"
        to: "input/deps/node_id_2/draft.md"
    max_iterations: 50      # 可选，默认 50
    timeout: 600            # 可选，默认 600 秒
    retries: 3              # 可选，默认 3
```

---

## 字段说明

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `dag.name` | 否 | string | DAG 名称 |
| `dag.max_parallel` | 否 | int | 最大并行节点数 |
| `dag.webhooks` | 否 | dict | Webhook URL 配置（`on_node_complete`、`on_node_error`） |
| `nodes.<id>.prompt` | **是** | string | 发给 Agent 的任务描述 |
| `nodes.<id>.model` | 否 | string | 模型别名（需在 models.yaml 中定义） |
| `nodes.<id>.deps` | 否 | list[string] | 依赖节点 ID |
| `nodes.<id>.inputs` | 否 | list[string] | 从 DAG 根目录复制到节点 `input/` 的文件 |
| `nodes.<id>.dep_inputs` | 否 | list[{from, to}] | 从上游节点复制产物 |
| `nodes.<id>.max_iterations` | 否 | int | Agent 最大迭代轮数，默认 50 |
| `nodes.<id>.timeout` | 否 | int | 节点超时秒数，默认 600 |
| `nodes.<id>.retries` | 否 | int | 失败重试次数，默认 3 |
| `nodes.<id>.stream` | 否 | bool | 流式输出，默认 true |
| `nodes.<id>.output_schema` | 否 | dict | JSON Schema，Agent 输出将自动校验 |
| `nodes.<id>.approval_required` | 否 | bool | 工具调用需人工审批，默认 false |
| `nodes.<id>.approval_tools` | 否 | list[string] | 需审批的工具列表，默认全部 |
| `nodes.<id>.approval_timeout` | 否 | int | 审批超时秒数，默认 300 |

### 产物规则

- Agent 完成任务的标准：**写入 `output/draft.md`**
- 也可自定义完成标记：`done_file: "output/report.md"`
- 下游节点通过 `dep_inputs` 读取上游产物
- 路径规则：`from` 相对于 DAG 根目录，`to` 相对于节点 `.context/`

---

## 示例 1：简单顺序

```yaml
dag:
  name: "写一篇文章"
  max_parallel: 2

nodes:
  outline:
    prompt: |
      为"AI Agent Runtime"写一篇大纲。
      把大纲写入 output/draft.md。
    model: "gpt-4o"

  draft:
    prompt: |
      读取 .context/outline.md，根据大纲写正文。
      把正文写入 output/draft.md。
    model: "gpt-4o"
    deps: [outline]
    dep_inputs:
      - from: "outline/output/draft.md"
        to: "outline.md"
```

## 示例 2：并行 + 汇聚

```yaml
dag:
  name: "研究报告"
  max_parallel: 4

nodes:
  collect_sources:
    prompt: "收集 5 个关于 AI Agent 的信息源，写入 output/sources.md"
    model: "gpt-4o"

  analyze_tech:
    prompt: "读取 .context/sources.md，分析技术趋势，写入 output/trends.md"
    model: "claude"
    deps: [collect_sources]
    dep_inputs:
      - from: "collect_sources/output/sources.md"
        to: "sources.md"

  analyze_biz:
    prompt: "读取 .context/sources.md，分析商业机会，写入 output/biz.md"
    model: "claude"
    deps: [collect_sources]
    dep_inputs:
      - from: "collect_sources/output/sources.md"
        to: "sources.md"

  write_report:
    prompt: |
      读取 .context/trends.md 和 .context/biz.md，
      写一份综合报告到 output/draft.md。
    model: "gpt-4o"
    deps: [analyze_tech, analyze_biz]
    dep_inputs:
      - from: "analyze_tech/output/trends.md"
        to: "trends.md"
      - from: "analyze_biz/output/biz.md"
        to: "biz.md"
```

## 示例 3：结构化输出

```yaml
dag:
  name: "数据提取"

nodes:
  extract:
    prompt: "从 input/articles.md 中提取关键信息，以 JSON 输出"
    model: "deepseek-pro"
    inputs: ["articles.md"]
    output_schema:
      type: object
      properties:
        title: {type: string}
        entities:
          type: array
          items:
            type: object
            properties:
              name: {type: string}
              type: {type: string}
            required: [name, type]
      required: [title, entities]
```

## 示例 4：带审批

```yaml
dag:
  name: "安全操作"

nodes:
  cleanup:
    prompt: "清理 workspace/ 下的临时文件"
    model: "deepseek-flash"
    approval_required: true
    approval_tools: ["run_shell"]
    approval_timeout: 120
```

## 示例 5：带输入文件

```yaml
dag:
  name: "代码审查"

nodes:
  review:
    prompt: "审查 .context/src/main.py 的代码质量问题"
    model: "claude"
    inputs: ["src/main.py"]   # 从 DAG 根目录复制到 .context/src/main.py
```

---

## Meta Agent 最佳实践

### 推荐工作流

1. **分解任务**：把大任务拆成 3-10 个有依赖关系的小任务
2. **命名节点**：用 `snake_case`，简短清晰（如 `collect_data`, `analyze`, `write_summary`）
3. **写 prompt**：每个 prompt 要明确告诉 Agent：
   - 任务目标
   - 需要读取哪些文件
   - 需要写入哪个文件
4. **声明依赖**：用 `deps` 声明执行顺序，用 `dep_inputs` 声明文件传递
5. **选择模型**：简单任务用便宜模型，复杂任务用好模型

### 常见错误

- ❌ `deps` 引用不存在的节点 ID（拼写错误）
- ❌ `dep_inputs.from` 路径写错（应该是 `上游节点名/output/文件名`）
- ❌ prompt 没告诉 Agent 输出文件路径
- ❌ 忘记在 `dep_inputs` 中传递上游产物，导致下游读不到文件

### 验证

写完 DAG 后，用 Agenda 验证：

```bash
agenda dag validate dag.yaml --json
```

---

## JSON 格式（推荐 Meta Agent 使用）

Meta Agent 生成 JSON 比 YAML 更不容易出错。Agenda 接受 JSON 输入：

```bash
echo '{"dag":{"name":"test"},"nodes":{...}}' | agenda dag create --from-json - -o dag.yaml
```

JSON 结构与 YAML 完全等价，只是格式不同。
