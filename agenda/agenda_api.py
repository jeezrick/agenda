"""agenda() 顶层函数 — 递归调用的统一入口。

设计：
- Base Case: 单节点 DAG → 直接 AgentLoop.run()，跳过 Scheduler 开销
- Recursive Step: 多节点 DAG → Scheduler.run() 并行调度
- agenda() 是普通函数，Agent 调用它和调用 read_file 没有区别

对应 README 待实现项 #2、#3、#4。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import AgentLoop
from .const import DEFAULT_MAX_ITERATIONS, DEFAULT_NODE_TIMEOUT, MAX_SUB_AGENT_DEPTH
from .scheduler import DAGScheduler
from .session import Session
from .tools import ToolRegistry


async def run_agent_node(
    session: Session,
    node_config: dict,
    model_registry: Any,
    tools_factory: Callable[[Session], ToolRegistry],
    depth: int = 0,
    hooks: Any = None,
) -> str:
    """运行单个 Agent 节点的核心逻辑。

    被 DAGScheduler._run_node() 和 run_sub_dag() Base Case 共用，
    消除重复代码（DRY）。

    Args:
        session: 已准备好的 Session（hints/inputs/history 就绪）
        node_config: 节点配置字典
        model_registry: 模型注册表
        tools_factory: 工具工厂函数
        depth: 当前递归深度

    Returns:
        Agent 的最终输出文本
    """
    tools = tools_factory(session)

    # ── 注入 agenda() 递归工具 ──────────────────────────────────
    @tools.register("agenda")  # type: ignore[arg-type]
    async def agenda_tool(
        dag_yaml: str,
        workspace: str | None = None,
        inputs_json: str = "{}",
    ) -> str:
        """启动子 DAG 实现递归分解。dag_yaml 为 DAG 的 YAML 定义。"""
        import yaml as _yaml

        dag_spec = _yaml.safe_load(dag_yaml)
        ws = Path(workspace) if workspace else session.workspace_dir / "subdags"
        ws.mkdir(parents=True, exist_ok=True)

        # 深度软约束
        if depth >= MAX_SUB_AGENT_DEPTH:
            return (
                f"[深度限制] 当前深度 {depth} 已达软上限 "
                f"{MAX_SUB_AGENT_DEPTH}。建议在当前层级完成任务，"
                f"或精简子 DAG 规模。"
            )

        results = await run_sub_dag(
            dag_spec=dag_spec,
            workspace=ws,
            model_registry=model_registry,
            tools_factory=tools_factory,
            depth=depth + 1,
            hooks=hooks,
        )
        return json.dumps(results, ensure_ascii=False)

    # 构建 system prompt（Jinja2 模板化）
    hints = session.read_system("hints.md")
    tools_description = tools.describe()
    system_prompt = DAGScheduler._render_system_prompt(hints, tools_description)

    # 创建并运行 AgentLoop
    agent = AgentLoop(
        session=session,
        model_registry=model_registry,
        tools=tools,
        model=node_config.get("model"),
        max_iterations=node_config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        timeout=node_config.get("timeout", DEFAULT_NODE_TIMEOUT),
        node_id=session.node_dir.name,
        stream=node_config.get("stream", True),
        hooks=hooks,
    )
    agent.approval_required = bool(node_config.get("approval_required", False))
    agent.approval_tools = node_config.get("approval_tools", [])
    agent.approval_timeout = float(node_config.get("approval_timeout", 300))

    result = await agent.run(system_prompt, node_config.get("prompt", ""))

    # 写入产物（如果 Agent 没有自己写）
    if not session.output_exists and result:
        session.write_file("output/draft.md", result)

    # ── 输出 Schema 校验（可选）──────────────────────────────
    output_schema = node_config.get("output_schema")
    if output_schema and session.output_exists:
        result = await _validate_and_correct_output(
            session=session,
            node_config=node_config,
            model_registry=model_registry,
            tools_factory=tools_factory,
            agent=agent,
            system_prompt=system_prompt,
        )

    return result


async def _validate_and_correct_output(
    session: Session,
    node_config: dict,
    model_registry: Any,
    tools_factory: Callable[[Session], ToolRegistry],
    agent: AgentLoop,
    system_prompt: str,
) -> str:
    """校验输出是否符合 output_schema，不符合则给 Agent 修正机会（最多 3 次）。"""
    output_schema = node_config.get("output_schema")
    max_attempts = 3

    for attempt in range(max_attempts):
        draft_path = session.output_dir / "draft.md"
        if not draft_path.exists():
            return ""

        raw = draft_path.read_text(encoding="utf-8")

        # 尝试解析 JSON
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            if attempt < max_attempts - 1:
                print(f"  [校验] JSON 解析失败 (attempt {attempt + 1}/{max_attempts}): {e}")
                correction_msg = (
                    f"你的输出无法被解析为 JSON: {e}。请重新输出纯 JSON 对象，不要用 markdown 代码块包裹。"
                )
                agent.messages.append({"role": "user", "content": correction_msg})
                result = await agent.run(system_prompt, "请修正你的输出格式。")
                if not session.output_exists and result:
                    session.write_file("output/draft.md", result)
                continue
            else:
                return raw

        # 验证 JSON Schema（如果 jsonschema 可用）
        try:
            import jsonschema

            jsonschema.validate(parsed, output_schema)
        except ImportError:
            pass  # jsonschema 未安装，跳过深度校验
        except jsonschema.ValidationError as e:
            if attempt < max_attempts - 1:
                print(f"  [校验] Schema 校验失败 (attempt {attempt + 1}/{max_attempts}): {e.message}")
                correction_msg = (
                    f"你的输出不符合要求的 JSON Schema: {e.message}。"
                    "请根据 schema 要求修正后重新输出。"
                )
                agent.messages.append({"role": "user", "content": correction_msg})
                result = await agent.run(system_prompt, "请根据 schema 修正你的输出。")
                if not session.output_exists and result:
                    session.write_file("output/draft.md", result)
                continue
            else:
                return raw
        else:
            print("  [校验] Schema 校验通过 ✓")
            break

    # 读取最终产物
    draft_path = session.output_dir / "draft.md"
    if draft_path.exists():
        return draft_path.read_text(encoding="utf-8")
    return ""


async def run_sub_dag(
    dag_spec: dict,
    workspace: Path,
    model_registry: Any,
    tools_factory: Callable[[Session], ToolRegistry],
    depth: int = 0,
    hooks: Any = None,
) -> dict[str, str]:
    """运行子 DAG，自动退化 Base Case。

    Args:
        dag_spec: DAG 定义字典（同 dag.yaml 解析后的结构）
        workspace: 子 DAG 的工作目录
        model_registry: 模型注册表
        tools_factory: 工具工厂函数（接收 Session 返回 ToolRegistry）
        depth: 当前递归深度

    Returns:
        节点状态映射 {node_id: "COMPLETED"|"FAILED"|"PENDING"}
    """
    nodes = dag_spec.get("nodes", {})
    if not nodes:
        return {}

    # ── Base Case 优化 ───────────────────────────────────────────
    # 单节点 DAG 直接 AgentLoop.run()，不创建 Scheduler、不写 scheduler_state
    if len(nodes) == 1:
        node_id = list(nodes.keys())[0]
        node_cfg = nodes[node_id]
        node_dir = workspace / "nodes" / node_id
        session = Session(node_dir)

        # 构造 hints（复用 scheduler 的 hints 逻辑）
        available_files = []
        if session.input_dir.exists():
            for f in sorted(session.input_dir.rglob("*")):
                if f.is_file():
                    available_files.append(str(f.relative_to(session.input_dir)))

        files_section = ""
        if available_files:
            files_section = "\n## 可用输入文件\n"
            for p in available_files:
                files_section += f'- read_file("input/{p}")\n'

        schema_section = ""
        output_schema = node_cfg.get("output_schema")
        if output_schema:
            try:
                schema_json = json.dumps(output_schema, ensure_ascii=False, indent=2)
                schema_section = f"""
## 输出格式要求

你的最终产物必须是符合以下 JSON Schema 的有效 JSON，写入 output/draft.md：

```json
{schema_json}
```

请确保输出是一个纯 JSON 对象，不要用 markdown 代码块包裹，不要加任何前缀或后缀文字。
"""
            except (TypeError, ValueError):
                pass

        hints = f"""# 任务: {node_id}
## 提示
{node_cfg.get("prompt", "")}{files_section}{schema_section}
## 规则
- 用 read_file / write_file 工具操作文件
- 按需读取 input/ 下的内容，不要一次性加载所有
- workspace/ 可放草稿和中间产物
- 完成后将最终产物写入 output/draft.md
- 如需继续分解任务，使用 agenda(dag_yaml) 工具
"""
        session.write_system("hints.md", hints)

        try:
            await run_agent_node(
                session=session,
                node_config=node_cfg,
                model_registry=model_registry,
                tools_factory=tools_factory,
                depth=depth,
                hooks=hooks,
            )
            session.set_state("status", "completed")
            return {node_id: "COMPLETED"}
        except asyncio.CancelledError:
            session.set_state("status", "cancelled")
            raise
        except Exception:
            session.set_state("status", "failed")
            return {node_id: "FAILED"}

    # ── Recursive Step ───────────────────────────────────────────
    scheduler = DAGScheduler(workspace, f"subdag_{depth}")
    scheduler.dag = dag_spec
    scheduler.hooks = hooks
    return await scheduler.run(tools_factory=tools_factory)
