"""命令行入口（给 Agent 用的 CLI）。默认 JSON 输出。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .const import (
    EXIT_ARGS_ERROR,
    EXIT_DAG_CONFIG_ERROR,
    EXIT_DEPENDENCY_ERROR,
    EXIT_EXECUTION_ERROR,
    EXIT_SUCCESS,
)
from .models import ModelRegistry
from .scheduler import DAGScheduler
from .session import Session
from .tools import build_tools

# ---------------------------------------------------------------------------
# Agent 自举指南（嵌入 CLI，任何 Agent 都能通过 agenda guide --for-agent 获取）
# ---------------------------------------------------------------------------

AGENT_GUIDE = """\
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

### 产物规则

- **完成标记**：Agent 写入 `output/draft.md` 即表示完成
- **自定义标记**：`done_file: "output/report.md"`
- **下游读取**：通过 `dep_inputs` 把上游 `output/` 复制到下游 `input/`

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
"""


# ---------------------------------------------------------------------------
# 输出层
# ---------------------------------------------------------------------------


def _json_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _error_out(msg: str, code: int, **extra: Any) -> int:
    out: dict = {"error": msg}
    out.update(extra)
    _json_out(out)
    return code


def _now_iso() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _resolve_dag_path(dag_arg: str | None) -> Path:
    dag_path = dag_arg or os.environ.get("AGENDA_DAG")
    if not dag_path:
        _json_out({"error": "未指定 DAG 路径。提供路径或设置 AGENDA_DAG"})
        sys.exit(EXIT_ARGS_ERROR)
    p = Path(dag_arg).expanduser().resolve()  # type: ignore[arg-type]
    return p


def _resolve_models_path(models_arg: str | None) -> Path | None:
    models_path = models_arg or os.environ.get("AGENDA_MODELS")
    if models_path:
        return Path(models_path).expanduser().resolve()
    return None


# ---------------------------------------------------------------------------
# Scheduler 加载
# ---------------------------------------------------------------------------


def _load_scheduler(dag_path: Path, models_path: Path | None = None) -> DAGScheduler:
    """加载 DAG 和模型配置。"""
    if dag_path.is_file():
        dag_dir, dag_file = dag_path.parent, dag_path
    elif (dag_path / "dag.yaml").exists():
        dag_dir, dag_file = dag_path, dag_path / "dag.yaml"
    else:
        raise FileNotFoundError(f"DAG 文件不存在: {dag_path} (也不是包含 dag.yaml 的目录)")

    scheduler = DAGScheduler(dag_dir.parent, dag_dir.name)
    scheduler.dag_file = dag_file
    scheduler.load()
    if models_path:
        scheduler.model_registry = ModelRegistry().load(
            models_path.parent if models_path.name == "models.yaml" else None
        )
    return scheduler


# ---------------------------------------------------------------------------
# DAG 校验
# ---------------------------------------------------------------------------


def _validate_dag(scheduler: DAGScheduler) -> tuple[list[str], list[str]]:
    """校验 DAG 配置。返回 (errors, warnings)。"""
    errors: list[str] = []
    warnings: list[str] = []

    dag_meta = scheduler.dag.get("dag")
    nodes = scheduler.dag.get("nodes")

    # 1. 结构完整性
    if not isinstance(dag_meta, dict):
        errors.append("缺少 dag 元数据字段")
    if not isinstance(nodes, dict):
        errors.append("缺少 nodes 字段")
        return errors, warnings
    if not nodes:
        warnings.append("nodes 为空")

    node_ids = set(nodes.keys())
    registry = scheduler.model_registry

    for nid, cfg in nodes.items():
        # 2. 节点配置完整性
        if not cfg.get("prompt"):
            warnings.append(f"节点 {nid} 缺少 prompt")

        # 3. deps 有效性
        for dep in cfg.get("deps", []):
            if dep not in node_ids:
                errors.append(f"节点 {nid} 依赖不存在的节点: {dep}")

        # 4. 模型存在性
        model_alias = cfg.get("model")
        if model_alias:
            known = set(registry._models.keys())
            known_models = {c.model for c in registry._models.values()}
            if model_alias not in known and model_alias not in known_models:
                warnings.append(f"节点 {nid} 模型 '{model_alias}' 未在注册表中定义")

        # 5. dep_inputs 路径格式
        for mapping in cfg.get("dep_inputs", []):
            from_path = mapping.get("from", "")
            to_path = mapping.get("to", "")
            if not from_path:
                errors.append(f"节点 {nid} dep_inputs 缺少 from 字段")
            if not to_path:
                errors.append(f"节点 {nid} dep_inputs 缺少 to 字段")
            # from 路径应该指向某个节点的 output/
            if "output/" not in from_path and "output\\" not in from_path:
                warnings.append(f"节点 {nid} dep_inputs from 路径 '{from_path}' 不包含 output/")

        # 6. inputs 文件存在性（warn 级别）
        for inp in cfg.get("inputs", []):
            plain = inp.split("#")[0].lstrip("/")
            if not (scheduler.dag_dir / plain).exists():
                warnings.append(f"节点 {nid} 输入文件不存在: {inp}")

    # 7. 循环依赖
    cycle = scheduler._detect_cycle()
    if cycle:
        errors.append("循环依赖: " + " -> ".join(cycle) + " -> " + cycle[0])

    return errors, warnings


# ---------------------------------------------------------------------------
# DAG 运行
# ---------------------------------------------------------------------------


def _run_dag(dag_path: Path, models_path: Path | None, max_parallel: int) -> int:
    scheduler = _load_scheduler(dag_path, models_path)
    scheduler.dag["dag"]["max_parallel"] = max_parallel

    from . import agenda as _agenda

    results = asyncio.run(
        _agenda(
            dag_spec=scheduler.dag,
            workspace=scheduler.dag_dir,
            model_registry=scheduler.model_registry,
            tools_factory=lambda session: build_tools(session),
        )
    )
    _json_out({"results": results})
    failed = [n for n, s in results.items() if s == "FAILED"]
    pending = [n for n, s in results.items() if s == "PENDING"]
    if failed:
        return EXIT_DEPENDENCY_ERROR
    if pending:
        return EXIT_EXECUTION_ERROR
    return EXIT_SUCCESS


async def _run_single_node(
    dag_path: Path,
    node_id: str,
    models_path: Path | None,
    force: bool = False,
) -> int:
    """通过 agenda() 统一入口运行单个节点（Base Case 优化）。"""
    scheduler = _load_scheduler(dag_path, models_path)
    if node_id not in scheduler.dag.get("nodes", {}):
        _json_out({"error": f"节点不存在: {node_id}"})
        return EXIT_DAG_CONFIG_ERROR

    node_cfg = scheduler.dag["nodes"][node_id]

    if force:
        node_dir = scheduler.dag_dir / "nodes" / node_id
        if node_dir.exists():
            shutil.rmtree(node_dir)
            _json_out({"reset": node_id})

    single_dag = {
        "dag": {"name": node_id, "max_parallel": 1},
        "nodes": {node_id: node_cfg},
    }

    from . import agenda as _agenda

    results = await _agenda(
        dag_spec=single_dag,
        workspace=scheduler.dag_dir,
        model_registry=scheduler.model_registry,
        tools_factory=lambda session: build_tools(session),
    )
    _json_out({"results": results})
    status = results.get(node_id, "PENDING")
    if status == "COMPLETED":
        return EXIT_SUCCESS
    if status == "FAILED":
        return EXIT_EXECUTION_ERROR
    return EXIT_EXECUTION_ERROR


# ---------------------------------------------------------------------------
# Status / Watch
# ---------------------------------------------------------------------------


def _dag_status(scheduler: DAGScheduler, dag_path: Path) -> dict:
    nodes = scheduler.dag.get("nodes", {})
    completed = [n for n in nodes if scheduler.node_is_done(n)]
    failed = [n for n in nodes if scheduler.node_is_failed(n)]
    running = [n for n in nodes if scheduler.node_is_running(n)]
    pending = [n for n in nodes if n not in completed and n not in failed and n not in running]
    return {
        "dag": scheduler.dag.get("dag", {}).get("name", "untitled"),
        "path": str(dag_path),
        "total": len(nodes),
        "completed": completed,
        "failed": failed,
        "running": running,
        "pending": pending,
        "progress": f"{len(completed) + len(failed)}/{len(nodes)}",
    }


# ---------------------------------------------------------------------------
# Init template
# ---------------------------------------------------------------------------

DEFAULT_DAG_YAML = """\
dag:
  name: example
  max_parallel: 2

nodes:
  research:
    prompt: >
      搜索并总结关于 '未来城市' 的 3 个关键趋势。
      将结果写入 output/draft.md。
    model: deepseek-flash
    max_iterations: 10
    timeout: 120

  write:
    prompt: >
      基于 input/deps/research.md 的内容，撰写一篇 200 字的短文。
      保存到 output/draft.md。
    model: deepseek-flash
    deps: [research]
    dep_inputs:
      - from: nodes/research/output/draft.md
        to: deps/research.md
    max_iterations: 10
    timeout: 120
"""

DEFAULT_MODELS_YAML = """\
models:
  deepseek-flash:
    base_url: "https://api.deepseek.com"
    api_key: "${DEEPSEEK_API_KEY}"
    model: "deepseek-v4-flash"
    token_cap: 64000
    temperature: 1.0
    max_tokens: 8192
    thinking:
      type: enabled
    reasoning_effort: high

  deepseek-pro:
    base_url: "https://api.deepseek.com"
    api_key: "${DEEPSEEK_API_KEY}"
    model: "deepseek-v4-pro"
    token_cap: 64000
    temperature: 1.0
    max_tokens: 8192
    thinking:
      type: enabled
    reasoning_effort: high
    fallback_model: "deepseek-flash"
"""


def _init_workspace(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    dag_path = target_dir / "dag.yaml"
    if not dag_path.exists():
        dag_path.write_text(DEFAULT_DAG_YAML, encoding="utf-8")
    models_path = target_dir / "models.yaml"
    if not models_path.exists():
        models_path.write_text(DEFAULT_MODELS_YAML, encoding="utf-8")
    _json_out(
        {
            "init": str(target_dir.resolve()),
            "files": ["dag.yaml", "models.yaml"],
        }
    )


# ---------------------------------------------------------------------------
# CLI 主入口
# ---------------------------------------------------------------------------


def cli() -> int:
    parser = argparse.ArgumentParser(
        description="Agenda — 给 Agent 调度 Agent 的极简运行时 v0.0.6",
        epilog="提示: Agent 用户可运行 'agenda guide --for-agent' 获取完整使用手册",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.0.6")
    subparsers = parser.add_subparsers(dest="cmd", help="命令")

    # ------------------------------------------------------------------
    # dag
    # ------------------------------------------------------------------
    dag_parser = subparsers.add_parser("dag", help="DAG 管理")
    dag_sub = dag_parser.add_subparsers(dest="dag_cmd")

    dag_init = dag_sub.add_parser("init", help="初始化示例工作区")
    dag_init.add_argument("path", nargs="?", default=".")

    dag_create = dag_sub.add_parser("create", help="JSON -> YAML")
    dag_create.add_argument("--from-json", required=True)
    dag_create.add_argument("-o", "--output", required=True)

    dag_validate = dag_sub.add_parser("validate", help="验证 DAG")
    dag_validate.add_argument("path", nargs="?")

    dag_run = dag_sub.add_parser("run", help="运行 DAG")
    dag_run.add_argument("path", nargs="?")
    dag_run.add_argument("--models")
    dag_run.add_argument("--max-parallel", type=int)
    dag_run.add_argument("--dry-run", action="store_true")

    dag_status = dag_sub.add_parser("status", help="DAG 状态")
    dag_status.add_argument("path", nargs="?")
    dag_status.add_argument("--watch", action="store_true")

    # ------------------------------------------------------------------
    # node
    # ------------------------------------------------------------------
    node_parser = subparsers.add_parser("node", help="节点管理")
    node_sub = node_parser.add_subparsers(dest="node_cmd")

    node_run = node_sub.add_parser("run", help="运行单个节点")
    node_run.add_argument("path", nargs="?")
    node_run.add_argument("--node", required=True)
    node_run.add_argument("--models")
    node_run.add_argument("--force", action="store_true")

    node_reset = node_sub.add_parser("reset", help="重置节点")
    node_reset.add_argument("path", nargs="?")
    node_reset.add_argument("--node", required=True)

    node_history = node_sub.add_parser("history", help="节点对话历史")
    node_history.add_argument("path", nargs="?")
    node_history.add_argument("--node", required=True)

    node_logs = node_sub.add_parser("logs", help="节点日志")
    node_logs.add_argument("path", nargs="?")
    node_logs.add_argument("--node", required=True)

    node_approve = node_sub.add_parser("approve", help="批准节点审批请求")
    node_approve.add_argument("path", nargs="?")
    node_approve.add_argument("--node", required=True)

    node_reject = node_sub.add_parser("reject", help="拒绝节点审批请求")
    node_reject.add_argument("path", nargs="?")
    node_reject.add_argument("--node", required=True)

    # ------------------------------------------------------------------
    # daemon
    # ------------------------------------------------------------------
    daemon_parser = subparsers.add_parser("daemon", help="Daemon")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_cmd")

    daemon_start = daemon_sub.add_parser("start")
    daemon_start.add_argument("path", nargs="?")
    daemon_start.add_argument("--foreground", action="store_true")

    daemon_stop = daemon_sub.add_parser("stop")
    daemon_stop.add_argument("path", nargs="?")
    daemon_status = daemon_sub.add_parser("status")
    daemon_status.add_argument("path", nargs="?")

    # ------------------------------------------------------------------
    # models
    # ------------------------------------------------------------------
    models_parser = subparsers.add_parser("models", help="模型管理")
    models_sub = models_parser.add_subparsers(dest="models_cmd")

    models_list = models_sub.add_parser("list")
    models_list.add_argument("--config", help="模型配置文件路径")

    models_validate = models_sub.add_parser("validate")
    models_validate.add_argument("--config", help="模型配置文件路径")

    # ------------------------------------------------------------------
    # run (quick single-node)
    # ------------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="快速运行单节点任务（不写 DAG）")
    run_parser.add_argument("prompt", help="任务描述（发给 Agent 的 prompt）")
    run_parser.add_argument("--model", "-m", default="deepseek-flash", help="模型别名，默认 deepseek-flash")
    run_parser.add_argument("--output-dir", "-o", default=".agenda-quick", help="工作区目录，默认 ./.agenda-quick")
    run_parser.add_argument("--ephemeral", "-e", action="store_true", help="用完即走：跑完后自动删除工作区")
    run_parser.add_argument("--max-iterations", type=int, default=50, help="最大迭代轮数，默认 50")
    run_parser.add_argument("--timeout", type=int, default=600, help="超时秒数，默认 600")
    run_parser.add_argument(
        "--input-file", "-i", action="append", default=[], help="输入文件路径（可多次指定，复制到节点 input/）"
    )

    # ------------------------------------------------------------------
    # guide
    # ------------------------------------------------------------------
    guide_parser = subparsers.add_parser("guide", help="Agent 使用指南")
    guide_parser.add_argument("--for-agent", action="store_true", help="输出完整 Agent 使用手册")
    guide_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")

    # ------------------------------------------------------------------
    # Parse & dispatch
    # ------------------------------------------------------------------
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return EXIT_SUCCESS

    # ------------------------------------------------------------------
    # run (quick single-node)
    # ------------------------------------------------------------------
    if args.cmd == "run":
        workspace = Path(args.output_dir).expanduser().resolve()
        if args.ephemeral:
            import tempfile

            workspace = Path(tempfile.mkdtemp(prefix="agenda-quick-"))

        node_id = "task"
        node_dir = workspace / "nodes" / node_id
        node_dir.mkdir(parents=True, exist_ok=True)

        # 复制输入文件到节点 input/
        inputs_rel: list[str] = []
        if args.input_file:
            input_dir = node_dir / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            for src in args.input_file:
                src_path = Path(src).expanduser().resolve()
                if src_path.exists():
                    dst = input_dir / src_path.name
                    shutil.copy2(src_path, dst)
                    inputs_rel.append(str(Path("input") / src_path.name))
                else:
                    _json_out({"error": f"输入文件不存在: {src}"})
                    return EXIT_ARGS_ERROR

        # 构建单节点 DAG
        single_dag = {
            "dag": {"name": "quick", "max_parallel": 1},
            "nodes": {
                node_id: {
                    "prompt": args.prompt,
                    "model": args.model,
                    "max_iterations": args.max_iterations,
                    "timeout": args.timeout,
                    **({"inputs": inputs_rel} if inputs_rel else {}),
                }
            },
        }

        # 写 dag.yaml 到工作区（方便后续查看/调试）
        dag_file = workspace / "dag.yaml"
        try:
            import yaml

            dag_file.write_text(yaml.safe_dump(single_dag, allow_unicode=True, sort_keys=False), "utf-8")
        except Exception:
            pass  # yaml 写失败不影响运行

        models_path = _resolve_models_path(None)
        registry = ModelRegistry()
        if models_path and models_path.exists():
            registry.load(models_path.parent)
        else:
            registry.load()

        from . import agenda as _agenda

        try:
            results = asyncio.run(
                _agenda(
                    dag_spec=single_dag,
                    workspace=workspace,
                    model_registry=registry,
                    tools_factory=lambda session: build_tools(session),
                )
            )
        except KeyboardInterrupt:
            _json_out({"interrupted": True})
            if args.ephemeral and workspace.exists():
                shutil.rmtree(workspace)
            return 130
        except Exception as e:
            if args.ephemeral and workspace.exists():
                shutil.rmtree(workspace)
            return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        status = results.get(node_id, "PENDING")
        output_path = node_dir / "output" / "draft.md"
        output_preview = None
        if output_path.exists():
            try:
                text = output_path.read_text("utf-8")
                output_preview = text[:500] if len(text) <= 500 else text[:500] + "..."
            except Exception:
                pass

        result = {
            "status": status,
            "node": node_id,
            "workspace": str(workspace),
            "model": args.model,
            "output_path": str(output_path) if output_path.exists() else None,
            "output_preview": output_preview,
        }

        if args.ephemeral and workspace.exists():
            shutil.rmtree(workspace)
            result["workspace_deleted"] = True

        _json_out(result)
        return EXIT_SUCCESS if status == "COMPLETED" else EXIT_EXECUTION_ERROR

    # ------------------------------------------------------------------
    # guide
    # ------------------------------------------------------------------
    if args.cmd == "guide":
        if args.json:
            sections: dict[str, list[str]] = {}
            current = None
            for line in AGENT_GUIDE.splitlines():
                if line.startswith("## "):
                    current = line[3:].strip()
                    sections[current] = []
                elif current is not None:
                    sections[current].append(line)
            _json_out({"guide": {title: "\n".join(lines).strip() for title, lines in sections.items()}})
        else:
            if args.for_agent:
                print(AGENT_GUIDE)
            else:
                print("Agenda Agent 使用指南")
                print("")
                print("运行 `agenda guide --for-agent` 查看完整手册")
                print("运行 `agenda guide --for-agent --json` 查看结构化数据")
        return EXIT_SUCCESS

    # ------------------------------------------------------------------
    # dag commands
    # ------------------------------------------------------------------
    if args.cmd == "dag":
        if not args.dag_cmd:
            dag_parser.print_help()
            return EXIT_SUCCESS

        if args.dag_cmd == "init":
            _init_workspace(Path(args.path).expanduser().resolve())
            return EXIT_SUCCESS

        if args.dag_cmd == "create":
            import yaml

            src = args.from_json
            data = json.loads(sys.stdin.read() if src == "-" else Path(src).read_text("utf-8"))
            if "nodes" not in data:
                _json_out({"error": "JSON 缺少 nodes 字段"})
                return EXIT_ARGS_ERROR
            data.setdefault("dag", {"name": "untitled", "max_parallel": 4})
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
            _json_out({"created": str(out)})
            return EXIT_SUCCESS

        if args.dag_cmd == "validate":
            try:
                scheduler = _load_scheduler(_resolve_dag_path(args.path))
                errors, warnings = _validate_dag(scheduler)
                result = {
                    "valid": len(errors) == 0,
                    "path": str(scheduler.dag_file),
                    "nodes": len(scheduler.dag.get("nodes", {})),
                    "models": sorted(
                        {c.get("model") for c in scheduler.dag.get("nodes", {}).values() if c.get("model")}
                    ),
                    "warnings": warnings,
                    "errors": errors,
                }
                _json_out(result)
                return EXIT_SUCCESS if result["valid"] else EXIT_DAG_CONFIG_ERROR
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_DAG_CONFIG_ERROR, traceback=traceback.format_exc())

        if args.dag_cmd == "run":
            dag_path = _resolve_dag_path(args.path)
            models_path = _resolve_models_path(args.models)
            max_parallel = args.max_parallel or int(os.environ.get("AGENDA_MAX_PARALLEL", "4"))

            if args.dry_run:
                try:
                    scheduler = _load_scheduler(dag_path, models_path)
                    _json_out(
                        {
                            "dry_run": True,
                            "dag": str(dag_path),
                            "max_parallel": max_parallel,
                            "topo": scheduler.topological_sort(),
                            "nodes": {
                                n: {"model": c.get("model", "default"), "deps": c.get("deps", [])}
                                for n, c in scheduler.dag.get("nodes", {}).items()
                            },
                        }
                    )
                    return EXIT_SUCCESS
                except FileNotFoundError as e:
                    return _error_out(str(e), EXIT_ARGS_ERROR)
                except Exception as e:
                    return _error_out(str(e), EXIT_DAG_CONFIG_ERROR, traceback=traceback.format_exc())

            try:
                return _run_dag(dag_path, models_path, max_parallel)
            except KeyboardInterrupt:
                _json_out({"interrupted": True})
                return 130
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.dag_cmd == "status":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)

                if args.watch:
                    try:
                        while True:
                            _json_out(_dag_status(scheduler, dag_path))
                            nodes = scheduler.dag.get("nodes", {})
                            completed = [n for n in nodes if scheduler.node_is_done(n)]
                            failed = [n for n in nodes if scheduler.node_is_failed(n)]
                            if len(completed) + len(failed) == len(nodes):
                                break
                            time.sleep(1)
                        return EXIT_SUCCESS
                    except KeyboardInterrupt:
                        return 130

                _json_out(_dag_status(scheduler, dag_path))
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

    # ------------------------------------------------------------------
    # node commands
    # ------------------------------------------------------------------
    if args.cmd == "node":
        if not args.node_cmd:
            node_parser.print_help()
            return EXIT_SUCCESS

        if args.node_cmd == "run":
            try:
                return asyncio.run(
                    _run_single_node(
                        _resolve_dag_path(args.path),
                        args.node,
                        _resolve_models_path(args.models),
                        force=args.force,
                    )
                )
            except KeyboardInterrupt:
                _json_out({"interrupted": True})
                return 130
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.node_cmd == "reset":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)
                node_dir = scheduler.nodes_dir / args.node
                if node_dir.exists():
                    shutil.rmtree(node_dir)
                    _json_out({"reset": args.node})
                else:
                    _json_out({"reset": args.node, "note": "节点不存在"})
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.node_cmd == "history":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)
                session = Session(scheduler.nodes_dir / args.node)
                turns = session.load_turns()
                _json_out({"node": args.node, "turns": turns})
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.node_cmd == "logs":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)
                session = Session(scheduler.nodes_dir / args.node)
                error_log = session.read_system("error.log")
                state = session.get_state("status")
                _json_out(
                    {
                        "node": args.node,
                        "status": state,
                        "error_log": error_log or None,
                        "output_exists": session.output_exists,
                    }
                )
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.node_cmd == "approve":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)
                session = Session(scheduler.nodes_dir / args.node)
                session.respond_approval("approved")
                _json_out({"node": args.node, "approved": True})
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

        if args.node_cmd == "reject":
            try:
                dag_path = _resolve_dag_path(args.path)
                scheduler = _load_scheduler(dag_path)
                session = Session(scheduler.nodes_dir / args.node)
                session.respond_approval("rejected")
                _json_out({"node": args.node, "rejected": True})
                return EXIT_SUCCESS
            except FileNotFoundError as e:
                return _error_out(str(e), EXIT_ARGS_ERROR)
            except Exception as e:
                return _error_out(str(e), EXIT_EXECUTION_ERROR, traceback=traceback.format_exc())

    # ------------------------------------------------------------------
    # daemon commands
    # ------------------------------------------------------------------
    if args.cmd == "daemon":
        if not args.daemon_cmd:
            daemon_parser.print_help()
            return EXIT_SUCCESS

        from .daemon import _cmd_status, _cmd_stop, _start_daemon, _start_foreground

        dag_path = (
            _resolve_dag_path(args.path)
            if hasattr(args, "path") and args.path
            else Path(os.environ.get("AGENDA_DAG", "."))
        )
        dag_dir = dag_path.parent if dag_path.is_file() else dag_path

        if args.daemon_cmd == "start":
            if args.foreground:
                return _start_foreground(dag_dir, dag_dir / "dag.yaml")
            return _start_daemon(dag_dir, dag_dir / "dag.yaml")

        if args.daemon_cmd == "stop":
            return _cmd_stop(dag_dir)

        if args.daemon_cmd == "status":
            return _cmd_status(dag_dir)

    # ------------------------------------------------------------------
    # models commands
    # ------------------------------------------------------------------
    if args.cmd == "models":
        if not args.models_cmd:
            models_parser.print_help()
            return EXIT_SUCCESS

        models_path = _resolve_models_path(args.config) if hasattr(args, "config") else None
        registry = ModelRegistry()
        if models_path and models_path.exists():
            registry.load(models_path.parent)
        else:
            registry.load()

        if args.models_cmd == "list":
            models_result: list[dict] = []
            for name, cfg in registry._models.items():
                models_result.append(
                    {
                        "name": name,
                        "model": cfg.model,
                        "base_url": cfg.base_url,
                        "token_cap": cfg.token_cap,
                        "temperature": cfg.temperature,
                        "max_tokens": cfg.max_tokens,
                    }
                )
            _json_out({"models": models_result})
            return EXIT_SUCCESS

        if args.models_cmd == "validate":
            validate_result: list[dict] = []
            for name, cfg in registry._models.items():
                item = {
                    "name": name,
                    "valid": bool(cfg.api_key) and bool(cfg.model) and bool(cfg.base_url),
                    "model": cfg.model,
                    "base_url": cfg.base_url,
                    "has_api_key": bool(cfg.api_key),
                }
                validate_result.append(item)
            _json_out({"models": validate_result})
            return EXIT_SUCCESS

    _json_out({"error": f"未知命令: {args.cmd}"})
    return EXIT_ARGS_ERROR
