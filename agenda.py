#!/usr/bin/env python3
"""
Agenda — 给 Agent 调度 Agent 的极简运行时。

设计原则：
- 文件系统即状态（没有数据库、没有 socket）
- 目录即 Session（cd 进去就跑）
- 双目录隔离（.context/ Agent 可见，.system/ 系统私有）
- DAG 原生（依赖关系是 first-class）
- Hook 即策略（关键节点注入行为，不改源码）
- AI 自压缩记忆（token 满时让 Agent 自己归档）

依赖：只有标准库 + requests（或用户自己传 client）
单文件：复制粘贴即可运行，无需 pip install -e .
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

# 尝试导入 yaml，如果没有则给出友好提示
try:
    import yaml
except ImportError:
    print("[错误] 需要安装 PyYAML: pip install pyyaml")
    sys.exit(1)

# ============================================================
# 1. 工具注册表
# ============================================================

ToolFunc = Callable[..., str]


class ToolRegistry:
    """Agent 可调用的工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFunc] = {}

    def register(self, name: str, func: ToolFunc) -> ToolFunc:
        self._tools[name] = func
        return func

    def get(self, name: str) -> ToolFunc | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        """生成 OpenAI function calling 格式的 schemas。"""
        schemas = []
        for name, func in self._tools.items():
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (func.__doc__ or "").strip(),
                    "parameters": {"type": "object", "properties": {}},
                },
            })
        return schemas


# ============================================================
# 2. 模型注册表 — 多模型配置
# ============================================================

@dataclass
class ModelConfig:
    """单个模型的配置。"""
    name: str                # 模型别名，如 "deepseek"、"kimi"
    base_url: str            # API 端点，如 "https://api.deepseek.com/v1"
    api_key: str             # API 密钥
    model: str               # 实际模型名，如 "deepseek-chat"
    token_cap: int = 32000   # 上下文窗口上限
    provider: str = "openai" # 预留：未来支持非 OpenAI 接口


class ModelRegistry:
    """
    模型注册表：管理多个 LLM 配置。

    支持从以下位置加载（按优先级）：
    1. DAG 工作区内的 models.yaml
    2. ~/.agenda/models.yaml（全局配置）
    3. 环境变量（fallback）

    示例 models.yaml：
        models:
          deepseek:
            base_url: "https://api.deepseek.com/v1"
            api_key: "${DEEPSEEK_API_KEY}"   # 支持 ${ENV_VAR} 引用
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
    """

    _GLOBAL_PATH = Path.home() / ".agenda" / "models.yaml"

    def __init__(self) -> None:
        self._models: dict[str, ModelConfig] = {}

    def load(self, dag_dir: Path | None = None) -> ModelRegistry:
        """加载模型配置。"""
        # 1. 先尝试 DAG 工作区内的 models.yaml
        if dag_dir:
            local_file = dag_dir / "models.yaml"
            if local_file.exists():
                self._load_file(local_file)
                return self

        # 2. 再尝试全局配置
        if self._GLOBAL_PATH.exists():
            self._load_file(self._GLOBAL_PATH)
            return self

        # 3. fallback：从环境变量创建默认模型
        self._models["default"] = ModelConfig(
            name="default",
            base_url=os.environ.get("AGENDA_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("AGENDA_API_KEY", ""),
            model=os.environ.get("AGENDA_MODEL", "gpt-4"),
            token_cap=int(os.environ.get("AGENDA_TOKEN_CAP", "32000")),
        )
        return self

    def _load_file(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, cfg in (raw.get("models") or {}).items():
            if not isinstance(cfg, dict):
                continue
            self._models[name] = ModelConfig(
                name=name,
                base_url=self._resolve_value(cfg.get("base_url", "")),
                api_key=self._resolve_value(cfg.get("api_key", "")),
                model=self._resolve_value(cfg.get("model", "")),
                token_cap=int(cfg.get("token_cap", 32000)),
                provider=cfg.get("provider", "openai"),
            )

    def _resolve_value(self, value: str) -> str:
        """解析 ${ENV_VAR} 格式的值。"""
        if not isinstance(value, str):
            return str(value)
        match = re.match(r'^\$\{([^}]+)\}$', value.strip())
        if match:
            env_name = match.group(1)
            env_val = os.environ.get(env_name, "")
            if not env_val:
                print(f"[警告] 环境变量未设置: {env_name}")
            return env_val
        return value

    def get(self, name: str | None) -> ModelConfig:
        """获取模型配置。如果 name 为 None 或不存在，返回 default。"""
        if not name:
            return self._models.get("default", self._default_fallback())
        if name not in self._models:
            # 尝试匹配 model 字段（兼容直接写 model id）
            for cfg in self._models.values():
                if cfg.model == name:
                    return cfg
            print(f"[警告] 未知模型别名 '{name}'，使用 default")
            return self._models.get("default", self._default_fallback())
        return self._models[name]

    def _default_fallback(self) -> ModelConfig:
        return ModelConfig(
            name="default",
            base_url=os.environ.get("AGENDA_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("AGENDA_API_KEY", ""),
            model=os.environ.get("AGENDA_MODEL", "gpt-4"),
            token_cap=32000,
        )

    def list_models(self) -> list[str]:
        return list(self._models.keys())


# ============================================================
# 3. Session — 双目录隔离
# ============================================================

class Session:
    """
    一个 Session 就是一个目录。

    目录结构：
        nodes/{node_id}/
            .context/     ← Agent 可见（读/写）
            .system/      ← 系统私有（Agent 不可见）
            output/       ← Agent 产物
    """

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir).resolve()
        self.context_dir = self.node_dir / ".context"
        self.system_dir = self.node_dir / ".system"
        self.output_dir = self.node_dir / "output"

        # 自动创建目录
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # --- Agent 可见操作 ---

    def read_context(self, rel_path: str) -> str:
        """Agent 读取 .context/ 或 output/ 下的文件。"""
        target = self._resolve_safe(rel_path)
        if not target or not target.exists():
            return f"[错误] 文件不存在: {rel_path}"
        return target.read_text(encoding="utf-8")

    def write_output(self, rel_path: str, content: str) -> str:
        """Agent 写入 output/ 目录。"""
        target = self.output_dir / rel_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[成功] 已写入 {rel_path}"

    def list_context(self, rel_path: str = ".") -> str:
        """Agent 列出 .context/ 或 output/ 下的目录。"""
        target = self._resolve_safe(rel_path)
        if not target or not target.exists():
            return f"[错误] 目录不存在: {rel_path}"
        lines = []
        for item in sorted(target.iterdir()):
            t = "[目录]" if item.is_dir() else "[文件]"
            lines.append(f"{t} {item.name}")
        return "\n".join(lines) or "(空)"

    # --- 系统私有操作 ---

    def log_message(self, message: dict) -> None:
        """追加消息到 .system/session.jsonl（append-only）。"""
        session_log = self.system_dir / "session.jsonl"
        with open(session_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def write_system(self, rel_path: str, content: str) -> None:
        """系统写入 .system/ 目录（Agent 不可见）。"""
        target = self.system_dir / rel_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_system(self, rel_path: str) -> str:
        """系统读取 .system/ 目录。"""
        target = self.system_dir / rel_path.lstrip("/")
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")

    def set_state(self, key: str, value: Any) -> None:
        """读写 .system/state.json。"""
        state_file = self.system_dir / "state.json"
        state = {}
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        state[key] = value
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_state(self, key: str, default: Any = None) -> Any:
        state_file = self.system_dir / "state.json"
        if not state_file.exists():
            return default
        state = json.loads(state_file.read_text(encoding="utf-8"))
        return state.get(key, default)

    # --- 内部工具 ---

    def _resolve_safe(self, rel_path: str) -> Path | None:
        """解析路径，确保只在 .context/ 或 output/ 内。"""
        raw = Path(rel_path.lstrip("/"))
        for base in (self.context_dir, self.output_dir):
            candidate = (base / raw).resolve()
            try:
                candidate.relative_to(base.resolve())
                return candidate
            except ValueError:
                continue
        return None

    @property
    def output_exists(self) -> bool:
        """output/draft.md 存在即表示节点完成。"""
        return (self.output_dir / "draft.md").exists()


# ============================================================
# 4. Hook 注册表
# ============================================================

HookFunc = Callable[["AgentLoop"], Coroutine[Any, Any, None]]


class HookRegistry:
    """在 Agent 循环的关键节点插入策略。"""

    def __init__(self) -> None:
        self._before_tool: list[HookFunc] = []
        self._after_tool: list[HookFunc] = []
        self._before_loop: list[HookFunc] = []
        self._after_loop: list[HookFunc] = []
        self._on_complete: list[HookFunc] = []
        self._on_error: list[HookFunc] = []

    def before_tool(self, func: HookFunc) -> HookFunc:
        self._before_tool.append(func)
        return func

    def after_tool(self, func: HookFunc) -> HookFunc:
        self._after_tool.append(func)
        return func

    def before_loop(self, func: HookFunc) -> HookFunc:
        self._before_loop.append(func)
        return func

    def after_loop(self, func: HookFunc) -> HookFunc:
        self._after_loop.append(func)
        return func

    def on_complete(self, func: HookFunc) -> HookFunc:
        self._on_complete.append(func)
        return func

    def on_error(self, func: HookFunc) -> HookFunc:
        self._on_error.append(func)
        return func

    async def fire(self, name: str, loop: AgentLoop) -> None:
        handlers = getattr(self, f"_{name}", [])
        for handler in handlers:
            try:
                await handler(loop)
            except Exception as e:
                print(f"[Hook 错误] {name}: {e}")


# ============================================================
# 5. Agent Loop — 核心循环
# ============================================================

class AgentLoop:
    """
    Agent 的核心循环：
        prompt → LLM → (tool_call → execute → loop) → completion
    """

    def __init__(
        self,
        session: Session,
        model_registry: ModelRegistry,
        tools: ToolRegistry,
        hooks: HookRegistry | None = None,
        model: str | None = None,   # 模型别名，如 "deepseek"、"kimi"
    ) -> None:
        self.session = session
        self.model_registry = model_registry
        self.model_cfg = model_registry.get(model)
        self.tools = tools
        self.hooks = hooks or HookRegistry()
        self.token_cap = self.model_cfg.token_cap
        self.messages: list[dict] = []
        # 延迟创建 client（支持不同模型用不同 endpoint）
        self._client: Any | None = None

    async def run(self, system_prompt: str, task: str) -> str:
        """运行 Agent，返回最终产物。"""
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        try:
            while True:
                # --- 记忆压缩检查 ---
                if self._estimate_tokens() > self.token_cap * 0.75:
                    await self._compact_memory()

                # --- 调用 LLM ---
                response = await self._call_llm()
                msg = response["choices"][0]["message"]
                self.messages.append(self._msg_to_dict(msg))
                self.session.log_message(self._msg_to_dict(msg))

                # --- 完成信号 ---
                if not msg.get("tool_calls"):
                    result = msg.get("content", "")
                    await self.hooks.fire("on_complete", self)
                    return result

                # --- 执行 tools ---
                for tc in msg["tool_calls"]:
                    await self.hooks.fire("before_tool", self)
                    result = await self._execute_tool(tc)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result)[:4000],  # 截断过长结果
                    })
                    await self.hooks.fire("after_tool", self)

        except Exception as e:
            await self.hooks.fire("on_error", self)
            # 写入错误日志
            self.session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise

    # --- 内部方法 ---

    def _ensure_client(self) -> Any:
        """根据模型配置创建 OpenAI 兼容客户端。"""
        if self._client is not None:
            return self._client

        cfg = self.model_cfg
        try:
            from openai import AsyncOpenAI
        except ImportError:
            print("[错误] 需要安装 openai: pip install openai")
            raise SystemExit(1)

        self._client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
        )
        return self._client

    async def _call_llm(self) -> dict:
        """调用 LLM API。兼容任何 OpenAI 格式的客户端。"""
        client = self._ensure_client()
        kwargs = {
            "model": self.model_cfg.model,
            "messages": self.messages,
            "temperature": 0.6,
        }
        if self.tools._tools:
            kwargs["tools"] = self.tools.schemas()
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        return resp.model_dump()

    async def _execute_tool(self, tc: dict) -> str:
        """执行单个 tool call。"""
        func = tc["function"]
        name = func["name"]
        args = json.loads(func["arguments"]) if func["arguments"] else {}

        print(f"  [Tool] {name}({json.dumps(args, ensure_ascii=False)})")

        tool = self.tools.get(name)
        if not tool:
            return f"[错误] 未知工具: {name}"

        try:
            if asyncio.iscoroutinefunction(tool):
                return await tool(**args)
            else:
                return tool(**args)
        except Exception as e:
            return f"[执行错误] {type(e).__name__}: {e}"

    async def _compact_memory(self) -> None:
        """
        AI 自驱动记忆压缩。
        注入《紧急危机》prompt，让 Agent 自己归档记忆。
        """
        compact_prompt = """《紧急危机》！！！记忆容量即将达到上限。

你需要紧急完成三件事：
1. 保存记忆：把当前对话中对完成任务有用的内容，整理成 Markdown 文件，
   写入 .system/memory/YYYYMMDD_N.md
2. 保存技能：提炼对未来有用的知识/技能，写入 .system/skills/
3. 更新线索：修改 .system/hints.md，留下检索线索

可以创建新文件，也可以追加已有文件。
完成后，调用 done_compact 工具通知系统。
不要请求用户确认，直接执行。"""

        # 临时注入压缩 prompt
        self.messages.append({"role": "user", "content": compact_prompt})

        # 等待 Agent 完成归档（最多 3 轮）
        for _ in range(3):
            response = await self._call_llm()
            msg = response["choices"][0]["message"]
            self.messages.append(self._msg_to_dict(msg))

            if not msg.get("tool_calls"):
                break

            for tc in msg["tool_calls"]:
                result = await self._execute_tool(tc)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result)[:4000],
                })

        # 压缩后截断对话历史（保留 system + 最近 2 轮）
        self.messages = [self.messages[0]] + self.messages[-4:]
        print("  [记忆压缩完成]")

    def _estimate_tokens(self) -> int:
        """粗略估算 token 数（中文字符按 1.5 倍，英文按 1 倍）。"""
        text = json.dumps(self.messages, ensure_ascii=False)
        # 简单估算：中文 1.5 token/字，英文 1 token/字
        cn = len(re.findall(r"[\u4e00-\u9fff]", text))
        en = len(text) - cn
        return int(cn * 1.5 + en * 0.5)

    def _msg_to_dict(self, msg: Any) -> dict:
        """把 LLM 返回的消息对象转成 dict。"""
        if isinstance(msg, dict):
            return msg
        # openai 对象
        d = {"role": getattr(msg, "role", "assistant")}
        if hasattr(msg, "content") and msg.content:
            d["content"] = msg.content
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            d["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        return d


# ============================================================
# 6. DAG 调度器
# ============================================================

class DAGScheduler:
    """
    DAG 调度器：
    - 解析 YAML DAG 定义
    - 拓扑排序
    - Asyncio 并行调度
    - 文件系统状态机
    """

    def __init__(self, workspace: Path, dag_name: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.dag_dir = self.workspace / dag_name
        self.dag_file = self.dag_dir / "dag.yaml"
        self.nodes_dir = self.dag_dir / "nodes"

        # 如果没有 dag.yaml，创建一个空的
        self.dag_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(exist_ok=True)

        self.dag: dict = {}
        self.completed: set[str] = set()
        self.running: set[str] = set()

        # 加载模型注册表
        self.model_registry = ModelRegistry().load(self.dag_dir)
        print(f"[模型] 可用模型: {', '.join(self.model_registry.list_models())}")

    def load(self) -> DAGScheduler:
        """从 dag.yaml 加载，或创建默认空 DAG。"""
        import yaml

        if self.dag_file.exists():
            self.dag = yaml.safe_load(self.dag_file.read_text(encoding="utf-8"))
        else:
            self.dag = {"dag": {"name": "untitled", "max_parallel": 4}, "nodes": {}}
        return self

    def save(self) -> None:
        import yaml

        self.dag_file.write_text(yaml.safe_dump(self.dag, allow_unicode=True), encoding="utf-8")

    def node_is_done(self, node_id: str) -> bool:
        """检查节点是否完成：output/draft.md 存在。"""
        session = Session(self.nodes_dir / node_id)
        return session.output_exists

    def node_is_failed(self, node_id: str) -> bool:
        """检查节点是否失败：.system/error.log 存在。"""
        return (self.nodes_dir / node_id / ".system" / "error.log").exists()

    def ready_nodes(self) -> list[str]:
        """返回所有依赖已满足且未运行的节点。"""
        ready = []
        for node_id, config in self.dag.get("nodes", {}).items():
            if node_id in self.completed or node_id in self.running:
                continue
            deps = config.get("deps", [])
            if all(d in self.completed for d in deps):
                ready.append(node_id)
        return ready

    def prepare_node(self, node_id: str) -> Session:
        """准备节点目录：复制 inputs、dep_inputs。"""
        config = self.dag["nodes"][node_id]
        node_dir = self.nodes_dir / node_id
        session = Session(node_dir)

        # 1. 复制 meta inputs
        for src_pattern in config.get("inputs", []):
            self._copy_input(src_pattern, session.context_dir)

        # 2. 复制依赖产物
        for mapping in config.get("dep_inputs", []):
            src = self.dag_dir / mapping["from"]
            dst = session.context_dir / mapping["to"].lstrip("/")
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy(src, dst)

        # 3. 写 hints
        hints = f"""# DAG 任务: {node_id}
## 提示
{config.get('prompt', '')}
## 规则
- 用 read_file / write_file 工具操作文件
- 按需读取 input/ 下的内容，不要一次性加载所有
- 完成后写入 output/draft.md
"""
        session.write_system("hints.md", hints)
        return session

    async def run(
        self,
        tools_factory: Callable[[Session], ToolRegistry],
        hooks_factory: Callable[[], HookRegistry] | None = None,
    ) -> dict[str, str]:
        """运行整个 DAG，返回每个节点的状态。"""
        max_parallel = self.dag.get("dag", {}).get("max_parallel", 4)
        node_ids = list(self.dag.get("nodes", {}).keys())

        # 先标记已完成的节点
        self.completed = {n for n in node_ids if self.node_is_done(n)}
        print(f"[DAG] 总节点: {len(node_ids)}, 已完成: {len(self.completed)}")

        while len(self.completed) < len(node_ids):
            ready = self.ready_nodes()
            failed = [n for n in node_ids if self.node_is_failed(n)]

            # 如果有节点失败，DAG 不能继续（保守策略）
            if failed:
                print(f"[DAG] 节点失败，终止: {failed}")
                break

            if not ready and not self.running:
                remaining = set(node_ids) - self.completed
                print(f"[DAG] 死锁！剩余节点: {remaining}")
                break

            # 启动就绪节点（不超过 max_parallel）
            for node_id in ready[: max_parallel - len(self.running)]:
                asyncio.create_task(self._run_node(node_id, llm_client, tools_factory, hooks_factory))

            await asyncio.sleep(1)

        return {n: ("COMPLETED" if self.node_is_done(n) else "FAILED" if self.node_is_failed(n) else "PENDING") for n in node_ids}

    async def _run_node(
        self,
        node_id: str,
        tools_factory: Callable[[Session], ToolRegistry],
        hooks_factory: Callable[[], HookRegistry] | None,
    ) -> None:
        """运行单个节点。"""
        self.running.add(node_id)
        config = self.dag["nodes"][node_id]
        model_alias = config.get("model")
        print(f"[节点] {node_id} 启动 (模型: {model_alias or 'default'})")

        try:
            session = self.prepare_node(node_id)

            tools = tools_factory(session)
            hooks = hooks_factory() if hooks_factory else HookRegistry()

            # 构建 system prompt
            hints = session.read_system("hints.md")
            system_prompt = f"""你是一个智能体，正在执行 DAG 任务。

{hints}

# 可用工具
你可以调用以下工具来操作文件系统：
- read_file(path): 读取 .context/ 或 output/ 下的文件
- write_file(path, content): 写入 output/ 目录
- list_dir(path="."): 列出目录内容

# 记忆线索
{session.read_system("hints.md")}
"""

            agent = AgentLoop(
                session=session,
                model_registry=self.model_registry,
                tools=tools,
                hooks=hooks,
                model=model_alias,
            )

            result = await agent.run(system_prompt, config["prompt"])

            # 写入产物（如果 Agent 没有自己写）
            if not session.output_exists and result:
                session.write_output("draft.md", result)

            self.completed.add(node_id)
            print(f"[节点] {node_id} 完成")

        except Exception as e:
            print(f"[节点] {node_id} 失败: {e}")
            session = Session(self.nodes_dir / node_id)
            session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

        finally:
            self.running.discard(node_id)

    def _copy_input(self, src_pattern: str, dst_dir: Path) -> None:
        """复制 input 文件到节点 context。支持 #section 锚点。"""
        import shutil

        base = self.dag_dir
        if "#" in src_pattern:
            path, section = src_pattern.split("#", 1)
        else:
            path, section = src_pattern, None

        src = base / path.lstrip("/")
        if not src.exists():
            return

        dst = dst_dir / path.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)

        if section:
            # 简单锚点提取：找 ## section 到下一个 ##
            text = src.read_text(encoding="utf-8")
            pattern = rf"##?\s*{re.escape(section)}.*?(?=\n##?\s|\Z)"
            match = re.search(pattern, text, re.DOTALL)
            if match:
                dst.write_text(match.group(0), encoding="utf-8")
            else:
                shutil.copy(src, dst)
        else:
            shutil.copy(src, dst)


# ============================================================
# 7. 安全审查（从 EVA 移植）
# ============================================================

class SecurityReviewer:
    """
    用 LLM 自己审查命令安全性。
    不是正则匹配，而是让模型判断语义风险。
    """

    REVIEW_PROMPT = """你是一个安全专家。请审查下面的 {shell} 命令：

<command>
{command}
</command>

规则：
- 如果命令仅为只读操作（cat, ls, grep, find, head, tail, pwd, echo），输出"放行"
- 如果命令涉及写入、删除、执行、网络连接、权限修改，输出"禁止"
- 如果命令拼接了管道且难以判断，输出"禁止"

只输出"放行"或"禁止"这两个字。"""

    def __init__(self, llm_client: Any, model: str = "deepseek-chat") -> None:
        self.client = llm_client
        self.model = model

    async def review(self, command: str, shell: str = "bash") -> bool:
        """返回 True 表示放行，False 表示禁止。"""
        prompt = self.REVIEW_PROMPT.format(shell=shell, command=command)
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            text = resp.choices[0].message.content
            return "放行" in text and "禁止" not in text
        except Exception:
            # LLM 审查失败时，保守拒绝
            return False


# ============================================================
# 8. 工具工厂（内置工具）
# ============================================================

def build_tools(session: Session, allow_shell: bool = False, llm_client: Any | None = None) -> ToolRegistry:
    """
    为给定 Session 创建工具注册表。
    工具被限制在该 Session 的 .context/ 和 output/ 内。
    """
    tools = ToolRegistry()

    @tools.register("read_file")
    def read_file(path: str) -> str:
        """读取 .context/ 或 output/ 下的文件内容。"""
        return session.read_context(path)

    @tools.register("write_file")
    def write_file(path: str, content: str) -> str:
        """写入 output/ 目录。路径必须以 output/ 开头。"""
        if not path.startswith("output/"):
            return "[错误] 只能写入 output/ 目录"
        return session.write_output(path, content)

    @tools.register("list_dir")
    def list_dir(path: str = ".") -> str:
        """列出 .context/ 或 output/ 下的目录内容。"""
        return session.list_context(path)

    @tools.register("done_compact")
    def done_compact() -> str:
        """通知系统记忆压缩已完成。"""
        return "[系统] 记忆压缩完成"

    # 可选：shell 工具（带安全审查）
    if allow_shell and llm_client:
        reviewer = SecurityReviewer(llm_client)

        @tools.register("run_shell")
        def run_shell(command: str, timeout: int = 30) -> str:
            """执行 shell 命令（仅限读操作，写入/执行需审查）。"""
            import subprocess

            # 安全检查
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                allowed = loop.run_until_complete(reviewer.review(command))
            finally:
                loop.close()

            if not allowed:
                return "[安全审查] 命令被拒绝。如需执行，请用 read_file / write_file 操作文件。"

            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(session.context_dir),
                )
                output = result.stdout or ""
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"
                return output[:4000]
            except subprocess.TimeoutExpired:
                return f"[超时] 命令执行超过 {timeout} 秒"
            except Exception as e:
                return f"[错误] {type(e).__name__}: {e}"

    return tools


# ============================================================
# 9. 命令行入口
# ============================================================

def cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Agenda — 给 Agent 调度 Agent 的极简运行时")
    parser.add_argument("command", choices=["init", "run", "status", "reset"])
    parser.add_argument("--workspace", default=".", help="工作区目录")
    parser.add_argument("--dag", default="default", help="DAG 名称")
    parser.add_argument("--node", help="指定节点（用于 reset）")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    dag_dir = workspace / args.dag

    if args.command == "init":
        dag_dir.mkdir(parents=True, exist_ok=True)
        (dag_dir / "nodes").mkdir(exist_ok=True)
        dag_file = dag_dir / "dag.yaml"
        if not dag_file.exists():
            dag_file.write_text(
                "dag:\n  name: untitled\n  max_parallel: 4\nnodes:\n",
                encoding="utf-8",
            )
        print(f"[Agenda] 已初始化 DAG: {dag_dir}")
        return 0

    if args.command == "status":
        scheduler = DAGScheduler(workspace, args.dag).load()
        for node_id in scheduler.dag.get("nodes", {}):
            if scheduler.node_is_done(node_id):
                status = "✅ 完成"
            elif scheduler.node_is_failed(node_id):
                status = "❌ 失败"
            else:
                status = "⏳ 等待"
            print(f"  {node_id}: {status}")
        return 0

    if args.command == "reset":
        if not args.node:
            print("[错误] --node 参数必填")
            return 1
        node_dir = dag_dir / "nodes" / args.node
        if node_dir.exists():
            import shutil
            shutil.rmtree(node_dir)
        print(f"[Agenda] 已重置节点: {args.node}")
        return 0

    print(f"[Agenda] 未知命令: {args.command}")
    return 1


# ============================================================
# 10. 示例用法
# ============================================================

"""
# 示例 1：初始化工作区
python agenda.py init --workspace ./workspace --dag my_book

# 示例 2：定义 DAG（手写 dag.yaml）
cat > workspace/my_book/dag.yaml << 'EOF'
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
EOF

# 示例 3：Python API 调用
import asyncio
from openai import AsyncOpenAI
from agenda import DAGScheduler, build_tools

async def main():
    client = AsyncOpenAI(api_key="sk-...", base_url="https://api.deepseek.com/v1")
    scheduler = DAGScheduler("workspace", "my_book").load()
    results = await scheduler.run(
        llm_client=client,
        tools_factory=lambda session: build_tools(session, allow_shell=False),
    )
    print(results)

asyncio.run(main())
"""

if __name__ == "__main__":
    raise SystemExit(cli())
