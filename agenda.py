#!/usr/bin/env python3
"""
Agenda v0.0.4 — DAG-native Agent Runtime with Multi-Agent support.

设计原则：
- 文件系统即状态（没有数据库、没有 socket）
- 目录即 Session（cd 进去就跑）
- 双目录隔离（.context/ Agent 可见，.system/ 系统私有）
- DAG 原生（依赖关系是 first-class）
- Hook 即策略（关键节点注入行为，不改源码）
- AI 自压缩记忆（token 满时让 Agent 自己归档）
- 子 Agent 嵌套（Agent 可动态创建子 Agent，文件系统桥接通信）

依赖：标准库 + pyyaml + openai
单文件：复制粘贴即可运行，无需 pip install -e .

从 Butterfly 学习的关键设计：
  - append-only JSONL 持久化（context.jsonl / messages.jsonl）
  - 子 Agent 通过文件系统桥接（独立 session，轮询 output/done.json）
  - 每轮迭代后立即 commit 历史（防止取消后 orphan tool_call）
  - 最大迭代次数 + 超时保护

从 EVA 学习的关键设计：
  - AI 自驱动记忆压缩（《紧急危机》prompt）
  - LLM 语义安全审查
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
import traceback
import uuid
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
# 0. 常量与退出码
# ============================================================

EXIT_SUCCESS = 0
EXIT_ARGS_ERROR = 1
EXIT_DAG_CONFIG_ERROR = 2
EXIT_EXECUTION_ERROR = 3
EXIT_DEPENDENCY_ERROR = 4

# 子 Agent 最大嵌套深度（防止无限 fork）
MAX_SUB_AGENT_DEPTH = 2

# Agent 默认安全限制
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_NODE_TIMEOUT = 600  # 秒
DEFAULT_MAX_RETRIES = 3

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
            sig = self._infer_schema(func)
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (func.__doc__ or "").strip(),
                    "parameters": sig,
                },
            })
        return schemas

    @staticmethod
    def _infer_schema(func: ToolFunc) -> dict:
        """从函数签名简单推断 JSON Schema（支持常见类型标注）。"""
        import inspect
        sig = inspect.signature(func)
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            if pname in ("session",):
                continue
            ann = param.annotation
            pschema: dict = {"type": "string"}
            if ann is int:
                pschema = {"type": "integer"}
            elif ann is bool:
                pschema = {"type": "boolean"}
            elif ann is float:
                pschema = {"type": "number"}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                pschema["default"] = param.default
            props[pname] = pschema
        return {"type": "object", "properties": props, "required": required}


# ============================================================
# 2. 模型注册表 — 多模型配置
# ============================================================

@dataclass
class ModelConfig:
    """单个模型的配置。"""
    name: str                # 模型别名，如 "deepseek"、"kimi"
    base_url: str            # API 端点
    api_key: str             # API 密钥
    model: str               # 实际模型名，如 "deepseek-chat"
    token_cap: int = 32000   # 上下文窗口上限
    provider: str = "openai" # 预留：未来支持非 OpenAI 接口


class ModelRegistry:
    """
    模型注册表：管理多个 LLM 配置。
    支持从 DAG 工作区内 models.yaml、~/.agenda/models.yaml、环境变量加载。
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
# 3. Session — 双目录隔离 + 持久化
# ============================================================

class Session:
    """
    一个 Session 就是一个目录。

    目录结构：
        nodes/{node_id}/
            .context/     ← Agent 可见（读/写）
            .system/      ← 系统私有（Agent 不可见）
            output/       ← Agent 产物
            children/     ← 子 Agent 的 session（Agent 不可见）

    持久化（学 Butterfly 的 append-only JSONL）：
        .system/messages.jsonl  ← 对话历史（append-only，每轮后 flush）
        .system/state.json      ← 运行状态（running/completed/failed）
        .system/session.jsonl   ← 运行时事件日志
    """

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir).resolve()
        self.context_dir = self.node_dir / ".context"
        self.system_dir = self.node_dir / ".system"
        self.output_dir = self.node_dir / "output"
        self.children_dir = self.node_dir / "children"

        # 自动创建目录
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.children_dir.mkdir(parents=True, exist_ok=True)

        # 文件路径
        self._messages_path = self.system_dir / "messages.jsonl"
        self._events_path = self.system_dir / "session.jsonl"
        self._state_path = self.system_dir / "state.json"

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
        """追加消息到 .system/session.jsonl（append-only 运行时日志）。"""
        with open(self._events_path, "a", encoding="utf-8") as f:
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
        state = {}
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        state[key] = value
        self._state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_state(self, key: str, default: Any = None) -> Any:
        if not self._state_path.exists():
            return default
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state.get(key, default)
        except (json.JSONDecodeError, OSError):
            return default

    # --- 持久化：messages.jsonl（学 Butterfly 的 context.jsonl） ---

    def save_message(self, message: dict) -> None:
        """追加单条消息到 messages.jsonl。"""
        with open(self._messages_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load_messages(self) -> list[dict]:
        """从 messages.jsonl 恢复对话历史。"""
        messages: list[dict] = []
        if not self._messages_path.exists():
            return messages
        with open(self._messages_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return messages

    def clear_messages(self) -> None:
        """清空 messages.jsonl（重置节点时调用）。"""
        if self._messages_path.exists():
            self._messages_path.unlink()

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
        """output/draft.md 存在即表示节点完成（默认判定）。"""
        return (self.output_dir / "draft.md").exists()

    def is_done(self, done_file: str | None = None) -> bool:
        """检查节点是否完成，支持自定义完成标记文件。"""
        if done_file:
            return (self.output_dir / done_file).exists()
        return self.output_exists

    def is_failed(self) -> bool:
        """检查节点是否失败：.system/error.log 存在。"""
        return (self.system_dir / "error.log").exists()

    # --- 子 Agent 管理 ---

    def child_session(self, name: str) -> "Session":
        """获取/创建子 Agent 的 session。"""
        child_dir = self.children_dir / name
        return Session(child_dir)


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
# 5. Agent Loop — 核心循环（加固版）
# ============================================================

class AgentLoop:
    """
    Agent 的核心循环：
        prompt → LLM → (tool_call → execute → loop) → completion

    加固措施（学 Butterfly + EVA）：
    - max_iterations: 防止无限循环（默认 50）
    - timeout: 节点级超时（默认 600s）
    - 每轮后 save_message() 持久化（Butterfly 的 per-commit）
    - 取消时补全 pending tool_calls（防止下次运行 400）
    - AI 自驱动记忆压缩
    """

    def __init__(
        self,
        session: Session,
        model_registry: ModelRegistry,
        tools: ToolRegistry,
        hooks: HookRegistry | None = None,
        model: str | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout: float = DEFAULT_NODE_TIMEOUT,
    ) -> None:
        self.session = session
        self.model_registry = model_registry
        self.model_cfg = model_registry.get(model)
        self.tools = tools
        self.hooks = hooks or HookRegistry()
        self.token_cap = self.model_cfg.token_cap
        self.messages: list[dict] = []
        self.max_iterations = max(1, max_iterations)
        self.timeout = timeout
        self._client: Any | None = None
        self._cancelled = False

    async def run(self, system_prompt: str, task: str) -> str:
        """运行 Agent，返回最终产物。支持超时和取消。"""
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        # 持久化初始消息
        for msg in self.messages:
            self.session.save_message(msg)

        start_time = time.monotonic()
        iteration = 0

        try:
            while iteration < self.max_iterations:
                iteration += 1

                # 超时检查
                if time.monotonic() - start_time > self.timeout:
                    raise asyncio.TimeoutError(f"节点运行超过 {self.timeout} 秒")

                # 取消检查
                if self._cancelled:
                    raise asyncio.CancelledError("Agent 被取消")

                # 记忆压缩检查
                if self._estimate_tokens() > self.token_cap * 0.75:
                    await self._compact_memory()

                # 调用 LLM
                response = await self._call_llm()
                msg = response["choices"][0]["message"]
                msg_dict = self._msg_to_dict(msg)
                self.messages.append(msg_dict)
                self.session.save_message(msg_dict)
                self.session.log_message({"type": "llm_response", "iteration": iteration, "ts": datetime.now().isoformat()})

                # 完成信号
                if not msg_dict.get("tool_calls"):
                    result = msg_dict.get("content", "")
                    await self.hooks.fire("on_complete", self)
                    return result

                # 执行 tools
                pending_tool_calls: list[dict] = msg_dict.get("tool_calls", [])
                for tc in pending_tool_calls:
                    await self.hooks.fire("before_tool", self)
                    result = await self._execute_tool(tc)
                    tool_result = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result)[:4000],
                    }
                    self.messages.append(tool_result)
                    self.session.save_message(tool_result)
                    await self.hooks.fire("after_tool", self)

            # 迭代次数超限
            raise RuntimeError(f"Agent 迭代次数达到上限 {self.max_iterations}")

        except asyncio.CancelledError:
            # Butterfly 式 orphan 修复：补全 pending tool_calls
            self._seal_orphan_tool_calls()
            await self.hooks.fire("on_error", self)
            raise
        except Exception as e:
            await self.hooks.fire("on_error", self)
            self.session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise

    def cancel(self) -> None:
        """标记取消（由外部调度器调用）。"""
        self._cancelled = True

    # --- 内部方法 ---

    def _ensure_client(self) -> Any:
        """根据模型配置创建 OpenAI 兼容客户端。"""
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError:
            print("[错误] 需要安装 openai: pip install openai")
            raise SystemExit(1)
        cfg = self.model_cfg
        self._client = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key)
        return self._client

    async def _call_llm(self) -> dict:
        """调用 LLM API。"""
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
        print(f"  [Tool] {name}({json.dumps(args, ensure_ascii=False)[:200]})")

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

    def _seal_orphan_tool_calls(self) -> None:
        """
        Butterfly 式修复：取消时，如果 messages 末尾有未完成的 tool_use，
        补全 synthetic tool_result，防止下次运行 LLM 返回 400（orphan tool_use）。
        """
        if not self.messages:
            return
        last = self.messages[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return
        for tc in last["tool_calls"]:
            tc_id = tc.get("id", "")
            # 检查是否已有对应的 tool_result
            has_result = any(
                m.get("role") == "tool" and m.get("tool_call_id") == tc_id
                for m in self.messages
            )
            if not has_result:
                synthetic = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "[系统] 工具调用因任务中断而被取消。",
                }
                self.messages.append(synthetic)
                self.session.save_message(synthetic)

    async def _compact_memory(self) -> None:
        """AI 自驱动记忆压缩（从 EVA 移植）。"""
        compact_prompt = """《紧急危机》！！！记忆容量即将达到上限。

你需要紧急完成三件事：
1. 保存记忆：把当前对话中对完成任务有用的内容，整理成 Markdown 文件，
   写入 .system/memory/YYYYMMDD_N.md
2. 保存技能：提炼对未来有用的知识/技能，写入 .system/skills/
3. 更新线索：修改 .system/hints.md，留下检索线索

可以创建新文件，也可以追加已有文件。
完成后，调用 done_compact 工具通知系统。
不要请求用户确认，直接执行。"""

        self.messages.append({"role": "user", "content": compact_prompt})
        self.session.save_message({"role": "user", "content": compact_prompt})

        for _ in range(3):
            response = await self._call_llm()
            msg = response["choices"][0]["message"]
            msg_dict = self._msg_to_dict(msg)
            self.messages.append(msg_dict)
            self.session.save_message(msg_dict)

            if not msg_dict.get("tool_calls"):
                break

            for tc in msg_dict["tool_calls"]:
                result = await self._execute_tool(tc)
                tr = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result)[:4000],
                }
                self.messages.append(tr)
                self.session.save_message(tr)

        # 压缩后截断：保留 system + 最近 4 轮
        self.messages = [self.messages[0]] + self.messages[-4:]
        # 重新持久化截断后的历史（覆盖式，因为 JSONL 不支持中间删除）
        self.session.clear_messages()
        for msg in self.messages:
            self.session.save_message(msg)
        print("  [记忆压缩完成]")

    def _estimate_tokens(self) -> int:
        """粗略估算 token 数。"""
        text = json.dumps(self.messages, ensure_ascii=False)
        cn = len(re.findall(r"[\u4e00-\u9fff]", text))
        en = len(text) - cn
        return int(cn * 1.5 + en * 0.5)

    def _msg_to_dict(self, msg: Any) -> dict:
        """把 LLM 返回的消息对象转成 dict。"""
        if isinstance(msg, dict):
            return msg
        d = {"role": getattr(msg, "role", "assistant")}
        if hasattr(msg, "content") and msg.content:
            d["content"] = msg.content
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            d["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        return d


# ============================================================
# 6. 子 Agent 系统（简化版 Butterfly sub_agent）
# ============================================================

class SubAgentManager:
    """
    子 Agent 管理器。

    设计：不实现 Butterfly 的完整 BridgeSession，而是用文件系统状态机：
    - 父 Agent 调用 spawn_child → 创建子 session + 启动子 Agent 任务
    - 子 Agent 运行完成后写 output/done.json
    - 父 Agent 轮询 wait_for_child 读取结果

    最大嵌套深度：MAX_SUB_AGENT_DEPTH（默认 2）
    """

    def __init__(self, parent_session: Session, model_registry: ModelRegistry,
                 max_depth: int = MAX_SUB_AGENT_DEPTH) -> None:
        self.parent_session = parent_session
        self.model_registry = model_registry
        self.max_depth = max_depth
        self._child_tasks: dict[str, asyncio.Task] = {}

    def _current_depth(self) -> int:
        """计算当前嵌套深度。"""
        depth = 0
        node_dir = self.parent_session.node_dir
        # 向上追溯 children/ 目录层数
        while node_dir.name == "children" or (node_dir.parent and node_dir.parent.name == "children"):
            depth += 1
            node_dir = node_dir.parent.parent if node_dir.parent else node_dir
        return depth

    async def spawn_child(
        self,
        task: str,
        name: str,
        model: str | None = None,
        system_prompt: str | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout: float = DEFAULT_NODE_TIMEOUT,
    ) -> str:
        """创建并启动子 Agent。返回 child_name。"""
        current_depth = self._current_depth()
        if current_depth >= self.max_depth:
            return f"[错误] 子 Agent 嵌套深度已达上限 {self.max_depth}"

        child_session = self.parent_session.child_session(name)
        # 如果已存在且已完成，拒绝覆盖
        if child_session.is_done("done.json"):
            return f"[错误] 子 Agent '{name}' 已存在且已完成"

        # 准备子 Agent 的 system prompt
        child_system = system_prompt or f"""你是一个子 Agent，被父 Agent 委派执行特定任务。

# 规则
- 你是独立的 Agent，有自己的 .context/ 和 output/
- 完成任务后，必须写入 output/draft.md 和 output/done.json
- done.json 格式: {{"status": "completed", "summary": "任务摘要"}}
- 如果失败，写入 output/done.json: {{"status": "failed", "error": "错误信息"}}

# 记忆线索
当前嵌套深度: {current_depth + 1}/{self.max_depth}
"""

        # 创建子 Agent 的工具集
        child_tools = build_tools(child_session, allow_shell=False)
        child_hooks = HookRegistry()

        async def _run_child() -> None:
            agent = AgentLoop(
                session=child_session,
                model_registry=self.model_registry,
                tools=child_tools,
                hooks=child_hooks,
                model=model,
                max_iterations=max_iterations,
                timeout=timeout,
            )
            try:
                result = await agent.run(child_system, task)
                # 写完成标记
                child_session.write_output("done.json", json.dumps({
                    "status": "completed",
                    "summary": result[:500] if result else "",
                    "finished_at": datetime.now().isoformat(),
                }, ensure_ascii=False))
                if not child_session.output_exists:
                    child_session.write_output("draft.md", result)
            except Exception as e:
                child_session.write_output("done.json", json.dumps({
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "finished_at": datetime.now().isoformat(),
                }, ensure_ascii=False))
                child_session.write_system("error.log", traceback.format_exc())

        task_obj = asyncio.create_task(_run_child(), name=f"subagent_{name}")
        self._child_tasks[name] = task_obj
        return f"[系统] 子 Agent '{name}' 已启动（深度: {current_depth + 1}/{self.max_depth}）"

    async def wait_for_child(self, name: str, poll_interval: float = 1.0, timeout: float = 300.0) -> str:
        """轮询等待子 Agent 完成。返回结果摘要。"""
        child_session = self.parent_session.child_session(name)
        deadline = time.monotonic() + timeout
        done_path = child_session.output_dir / "done.json"

        while time.monotonic() < deadline:
            if done_path.exists():
                try:
                    done = json.loads(done_path.read_text(encoding="utf-8"))
                    status = done.get("status", "unknown")
                    if status == "completed":
                        draft = child_session.read_context("output/draft.md")
                        return f"[子 Agent '{name}' 完成]\n{draft[:2000]}"
                    else:
                        return f"[子 Agent '{name}' 失败] {done.get('error', '未知错误')}"
                except Exception as e:
                    return f"[错误] 读取子 Agent '{name}' 结果失败: {e}"
            await asyncio.sleep(poll_interval)

        return f"[超时] 等待子 Agent '{name}' 超过 {timeout} 秒"

    def list_children(self) -> list[str]:
        """列出所有子 Agent。"""
        if not self.parent_session.children_dir.exists():
            return []
        return [d.name for d in self.parent_session.children_dir.iterdir() if d.is_dir()]

    def kill_child(self, name: str) -> str:
        """取消子 Agent 任务。"""
        task = self._child_tasks.get(name)
        if task and not task.done():
            task.cancel()
            return f"[系统] 子 Agent '{name}' 已取消"
        return f"[系统] 子 Agent '{name}' 未运行"


# ============================================================
# 7. DAG 调度器（完善版）
# ============================================================

class DAGScheduler:
    """
    DAG 调度器：
    - 解析 YAML DAG 定义
    - 拓扑排序 + 环检测（DFS）
    - Asyncio 并行调度
    - 文件系统状态机
    - 节点重试策略（最多 3 次）
    - 调度状态持久化（中断后可恢复）
    """

    def __init__(self, workspace: Path, dag_name: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.dag_dir = self.workspace / dag_name
        self.dag_file = self.dag_dir / "dag.yaml"
        self.nodes_dir = self.dag_dir / "nodes"
        self.state_file = self.dag_dir / ".system" / "scheduler_state.json"

        self.dag_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(exist_ok=True)
        (self.dag_dir / ".system").mkdir(parents=True, exist_ok=True)

        self.dag: dict = {}
        self.completed: set[str] = set()
        self.running: set[str] = set()
        self.failed: set[str] = set()
        self.retries: dict[str, int] = {}  # 节点 -> 已重试次数
        self._cancelled = False

        # 加载模型注册表
        self.model_registry = ModelRegistry().load(self.dag_dir)
        print(f"[模型] 可用模型: {', '.join(self.model_registry.list_models())}")

    def load(self) -> DAGScheduler:
        """从 dag.yaml 加载，或创建默认空 DAG。"""
        if self.dag_file.exists():
            self.dag = yaml.safe_load(self.dag_file.read_text(encoding="utf-8")) or {}
        else:
            self.dag = {"dag": {"name": "untitled", "max_parallel": 4}, "nodes": {}}
        return self

    def save(self) -> None:
        self.dag_file.write_text(yaml.safe_dump(self.dag, allow_unicode=True), encoding="utf-8")

    # --- 状态检查 ---

    def node_is_done(self, node_id: str) -> bool:
        """检查节点是否完成：output/draft.md 存在（或配置自定义完成文件）。"""
        session = Session(self.nodes_dir / node_id)
        config = self.dag.get("nodes", {}).get(node_id, {})
        done_file = config.get("done_file")
        return session.is_done(done_file)

    def node_is_failed(self, node_id: str) -> bool:
        return (self.nodes_dir / node_id / ".system" / "error.log").exists()

    def node_is_running(self, node_id: str) -> bool:
        state = Session(self.nodes_dir / node_id).get_state("status")
        return state == "running"

    # --- 调度状态持久化 ---

    def _save_scheduler_state(self) -> None:
        """保存调度器运行状态到文件（用于中断恢复）。"""
        state = {
            "completed": sorted(self.completed),
            "failed": sorted(self.failed),
            "running": sorted(self.running),
            "retries": self.retries,
            "saved_at": datetime.now().isoformat(),
        }
        self.state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_scheduler_state(self) -> None:
        """从文件恢复调度器状态。"""
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.completed = set(state.get("completed", []))
            self.failed = set(state.get("failed", []))
            self.running = set(state.get("running", []))
            self.retries = state.get("retries", {})
            print(f"[调度器] 从状态恢复: 已完成 {len(self.completed)}, 失败 {len(self.failed)}")
        except (json.JSONDecodeError, OSError):
            pass

    # --- 拓扑算法 ---

    def _detect_cycle(self) -> list[str] | None:
        """DFS 环检测。返回环中的节点列表，无环返回 None。"""
        nodes = self.dag.get("nodes", {})
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in nodes}
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            path.append(node)
            for dep in nodes.get(node, {}).get("deps", []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    # 发现环
                    cycle_start = path.index(dep)
                    return path[cycle_start:]
                if color[dep] == WHITE:
                    result = dfs(dep)
                    if result:
                        return result
            path.pop()
            color[node] = BLACK
            return None

        for node in nodes:
            if color[node] == WHITE:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    def topological_sort(self) -> list[str]:
        """返回拓扑排序后的节点列表。"""
        nodes = self.dag.get("nodes", {})
        in_degree = {n: 0 for n in nodes}
        adj = {n: [] for n in nodes}
        for n, cfg in nodes.items():
            for dep in cfg.get("deps", []):
                if dep in adj:
                    adj[dep].append(n)
                    in_degree[n] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        result = []
        while queue:
            n = queue.pop(0)
            result.append(n)
            for m in adj[n]:
                in_degree[m] -= 1
                if in_degree[m] == 0:
                    queue.append(m)
        return result

    def ready_nodes(self) -> list[str]:
        """返回所有依赖已满足且未运行的节点。"""
        ready = []
        for node_id, config in self.dag.get("nodes", {}).items():
            if node_id in self.completed or node_id in self.running or node_id in self.failed:
                continue
            deps = config.get("deps", [])
            if all(d in self.completed for d in deps):
                ready.append(node_id)
        return ready

    # --- 节点准备 ---

    def prepare_node(self, node_id: str) -> Session:
        """准备节点目录：复制 inputs、dep_inputs，恢复历史。"""
        config = self.dag["nodes"][node_id]
        node_dir = self.nodes_dir / node_id
        session = Session(node_dir)

        # 设置 running 状态
        session.set_state("status", "running")
        session.set_state("started_at", datetime.now().isoformat())

        # 1. 复制 meta inputs
        for src_pattern in config.get("inputs", []):
            self._copy_input(src_pattern, session.context_dir)

        # 2. 复制依赖产物
        for mapping in config.get("dep_inputs", []):
            src = self.dag_dir / mapping["from"]
            dst = session.context_dir / mapping["to"].lstrip("/")
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)

        # 3. 恢复历史（如果之前有中断）
        loaded = session.load_messages()
        if loaded:
            print(f"  [节点 {node_id}] 恢复 {len(loaded)} 条历史消息")

        # 4. 写 hints
        hints = f"""# DAG 任务: {node_id}
## 提示
{config.get('prompt', '')}
## 规则
- 用 read_file / write_file 工具操作文件
- 按需读取 input/ 下的内容，不要一次性加载所有
- 完成后写入 output/draft.md
- 如需创建子 Agent，使用 spawn_child 工具
"""
        session.write_system("hints.md", hints)
        return session

    # --- DAG 运行 ---

    async def run(
        self,
        tools_factory: Callable[[Session], ToolRegistry],
        hooks_factory: Callable[[], HookRegistry] | None = None,
    ) -> dict[str, str]:
        """运行整个 DAG，返回每个节点的状态。"""
        max_parallel = self.dag.get("dag", {}).get("max_parallel", 4)
        node_ids = list(self.dag.get("nodes", {}).keys())

        if not node_ids:
            print("[DAG] 空 DAG，无节点可运行")
            return {}

        # 恢复之前的状态
        self._load_scheduler_state()

        # 扫描已完成节点（文件系统 + 持久化状态）
        for n in node_ids:
            if self.node_is_done(n):
                self.completed.add(n)
            elif self.node_is_failed(n):
                # 检查是否可重试
                retries = self.retries.get(n, 0)
                max_retry = self.dag["nodes"][n].get("retries", DEFAULT_MAX_RETRIES)
                if retries < max_retry:
                    print(f"[DAG] 节点 {n} 失败，将重试 ({retries + 1}/{max_retry})")
                    # 清除错误标记，允许重试
                    error_log = self.nodes_dir / n / ".system" / "error.log"
                    if error_log.exists():
                        error_log.unlink()
                else:
                    self.failed.add(n)

        print(f"[DAG] 总节点: {len(node_ids)}, 已完成: {len(self.completed)}, 失败: {len(self.failed)}")

        pending_tasks: dict[str, asyncio.Task] = {}

        while len(self.completed) + len(self.failed) < len(node_ids):
            if self._cancelled:
                print("[DAG] 调度器被取消")
                break

            # 清理已完成的任务
            done_tasks = [n for n, t in pending_tasks.items() if t.done()]
            for n in done_tasks:
                del pending_tasks[n]
                self.running.discard(n)
                if self.node_is_done(n):
                    self.completed.add(n)
                    print(f"[节点] {n} 完成")
                elif self.node_is_failed(n):
                    retries = self.retries.get(n, 0)
                    max_retry = self.dag["nodes"][n].get("retries", DEFAULT_MAX_RETRIES)
                    if retries < max_retry:
                        self.retries[n] = retries + 1
                        # 不清除，等待下一轮 ready_nodes 重新调度
                        print(f"[节点] {n} 失败，将在下一轮重试 ({self.retries[n]}/{max_retry})")
                    else:
                        self.failed.add(n)
                        print(f"[节点] {n} 最终失败（重试耗尽）")
                self._save_scheduler_state()

            # 如果有节点失败且不可重试，终止 DAG（保守策略）
            if self.failed:
                # 检查是否还有依赖未满足的节点依赖于失败的节点
                for n in list(self.failed):
                    for other, cfg in self.dag.get("nodes", {}).items():
                        if n in cfg.get("deps", []) and other not in self.completed and other not in self.failed:
                            print(f"[DAG] 节点 {n} 失败导致下游 {other} 无法执行")
                # 如果所有 remaining 节点都依赖 failed 节点，则终止
                remaining = set(node_ids) - self.completed - self.failed
                blocked = {n for n in remaining if any(d in self.failed for d in self.dag["nodes"][n].get("deps", []))}
                if blocked == remaining:
                    print(f"[DAG] 所有剩余节点被失败节点阻塞，终止")
                    break

            # 死锁检测
            ready = self.ready_nodes()
            remaining = set(node_ids) - self.completed - self.failed - self.running
            if not ready and not self.running and remaining:
                print(f"[DAG] 死锁！剩余节点: {remaining}")
                break

            # 启动就绪节点（不超过 max_parallel）
            slots = max_parallel - len(self.running)
            for node_id in ready[:slots]:
                if self._cancelled:
                    break
                task = asyncio.create_task(
                    self._run_node(node_id, tools_factory, hooks_factory),
                    name=f"node_{node_id}",
                )
                pending_tasks[node_id] = task
                self.running.add(node_id)
                self._save_scheduler_state()

            if pending_tasks:
                # 等待任意任务完成或 1 秒超时（用于轮询新就绪节点）
                try:
                    done, _ = await asyncio.wait(
                        pending_tasks.values(),
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=1.0,
                    )
                except asyncio.CancelledError:
                    self._cancelled = True
                    for t in pending_tasks.values():
                        t.cancel()
                    raise
            else:
                await asyncio.sleep(0.5)

        # 等待所有 pending 任务完成
        if pending_tasks:
            await asyncio.gather(*pending_tasks.values(), return_exceptions=True)
            for n, t in pending_tasks.items():
                self.running.discard(n)
                if self.node_is_done(n):
                    self.completed.add(n)
                elif self.node_is_failed(n):
                    self.failed.add(n)

        self._save_scheduler_state()
        return {
            n: ("COMPLETED" if n in self.completed else "FAILED" if n in self.failed else "PENDING")
            for n in node_ids
        }

    def cancel(self) -> None:
        """取消整个 DAG 运行。"""
        self._cancelled = True

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

            # 注入子 Agent 工具
            sub_manager = SubAgentManager(session, self.model_registry)

            @tools.register("spawn_child")
            async def spawn_child(task: str, name: str, model: str | None = None) -> str:
                """创建子 Agent 执行子任务。name 必须唯一。"""
                return await sub_manager.spawn_child(task=task, name=name, model=model)

            @tools.register("wait_for_child")
            async def wait_for_child(name: str, timeout: int = 300) -> str:
                """等待子 Agent 完成并返回结果。"""
                return await sub_manager.wait_for_child(name=name, timeout=float(timeout))

            @tools.register("list_children")
            def list_children() -> str:
                """列出所有子 Agent。"""
                return json.dumps(sub_manager.list_children(), ensure_ascii=False)

            # 构建 system prompt
            hints = session.read_system("hints.md")
            system_prompt = f"""你是一个智能体，正在执行 DAG 任务。

{hints}

# 可用工具
你可以调用以下工具来操作文件系统：
- read_file(path): 读取 .context/ 或 output/ 下的文件
- write_file(path, content): 写入 output/ 目录
- list_dir(path="."): 列出目录内容
- spawn_child(task, name, model?): 创建子 Agent 执行子任务
- wait_for_child(name, timeout?): 等待子 Agent 完成
- list_children(): 列出所有子 Agent

# 记忆线索
{session.read_system("hints.md")}
"""

            agent = AgentLoop(
                session=session,
                model_registry=self.model_registry,
                tools=tools,
                hooks=hooks,
                model=model_alias,
                max_iterations=config.get("max_iterations", DEFAULT_MAX_ITERATIONS),
                timeout=config.get("timeout", DEFAULT_NODE_TIMEOUT),
            )

            result = await agent.run(system_prompt, config["prompt"])

            # 写入产物（如果 Agent 没有自己写）
            if not session.output_exists and result:
                session.write_output("draft.md", result)

            session.set_state("status", "completed")
            session.set_state("completed_at", datetime.now().isoformat())
            print(f"[节点] {node_id} 完成")

        except asyncio.CancelledError:
            session = Session(self.nodes_dir / node_id)
            session.set_state("status", "cancelled")
            print(f"[节点] {node_id} 被取消")
            raise
        except Exception as e:
            print(f"[节点] {node_id} 失败: {e}")
            session = Session(self.nodes_dir / node_id)
            session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            session.set_state("status", "failed")

        finally:
            self.running.discard(node_id)

    def _copy_input(self, src_pattern: str, dst_dir: Path) -> None:
        """复制 input 文件到节点 context。支持 #section 锚点。"""
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
# 8. 安全审查（从 EVA 移植）
# ============================================================

class SecurityReviewer:
    """用 LLM 自己审查命令安全性。"""

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
            return False


# ============================================================
# 9. 工具工厂（内置工具）
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
# 10. 命令行入口（给 Agent 用的 CLI）
# ============================================================

def _resolve_dag_path(dag_arg: str | None) -> Path:
    """解析 DAG 路径。支持环境变量 AGENDA_DAG 兜底。"""
    dag_path = dag_arg or os.environ.get("AGENDA_DAG")
    if not dag_path:
        print("[错误] 未指定 DAG 路径。请提供路径或设置 AGENDA_DAG 环境变量。")
        sys.exit(EXIT_ARGS_ERROR)
    return Path(dag_path).expanduser().resolve()


def _resolve_models_path(models_arg: str | None) -> Path | None:
    """解析模型配置路径。支持环境变量 AGENDA_MODELS 兜底。"""
    models_path = models_arg or os.environ.get("AGENDA_MODELS")
    if models_path:
        return Path(models_path).expanduser().resolve()
    return None


def _load_scheduler(dag_path: Path, models_path: Path | None = None) -> DAGScheduler:
    """加载 DAG 调度器。"""
    if dag_path.is_file():
        dag_dir = dag_path.parent
        dag_file = dag_path
    else:
        dag_dir = dag_path
        dag_file = dag_path / "dag.yaml"

    dag_name = dag_dir.name
    scheduler = DAGScheduler(dag_dir.parent, dag_name)
    scheduler.dag_file = dag_file
    scheduler.load()

    if models_path:
        scheduler.model_registry = ModelRegistry().load(
            models_path.parent if models_path.name == "models.yaml" else None
        )
    return scheduler


def _json_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _ndjson_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now().isoformat()


def cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Agenda — 给 Agent 调度 Agent 的极简运行时 v0.0.4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  AGENDA_DAG          默认 DAG 路径
  AGENDA_MODELS       默认模型配置路径
  AGENDA_MAX_PARALLEL 默认最大并行度

退出码:
  0  成功
  1  参数/命令错误
  2  DAG 配置错误
  3  节点执行失败
  4  依赖失败导致无法继续
  130 用户中断 (Ctrl+C)
        """,
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.0.4")

    subparsers = parser.add_subparsers(dest="cmd", help="命令")

    # ============================================================
    # dag 命令组
    # ============================================================
    dag_parser = subparsers.add_parser("dag", help="DAG 管理")
    dag_sub = dag_parser.add_subparsers(dest="dag_cmd", help="DAG 子命令")

    # dag init
    dag_init = dag_sub.add_parser("init", help="初始化 DAG 工作区")
    dag_init.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_init.add_argument("--from-template", help="从模板初始化")

    # dag validate
    dag_validate = dag_sub.add_parser("validate", help="验证 DAG 配置")
    dag_validate.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_validate.add_argument("--json", action="store_true", help="JSON 输出")

    # dag inspect
    dag_inspect = dag_sub.add_parser("inspect", help="查看 DAG 拓扑结构")
    dag_inspect.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_inspect.add_argument("--json", action="store_true", help="JSON 输出")

    # dag run
    dag_run = dag_sub.add_parser("run", help="运行 DAG")
    dag_run.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_run.add_argument("--models", help="模型配置文件路径（默认 AGENDA_MODELS）")
    dag_run.add_argument("--max-parallel", type=int, help="最大并行度")
    dag_run.add_argument("--dry-run", action="store_true", help="预演模式（不实际执行）")

    # dag status
    dag_status = dag_sub.add_parser("status", help="查看 DAG 运行状态")
    dag_status.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_status.add_argument("--json", action="store_true", help="JSON 输出")
    dag_status.add_argument("--watch", action="store_true", help="实时监听状态变化")

    # dag stop
    dag_stop = dag_sub.add_parser("stop", help="停止正在运行的 DAG")
    dag_stop.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")

    # ============================================================
    # node 命令组
    # ============================================================
    node_parser = subparsers.add_parser("node", help="节点管理")
    node_sub = node_parser.add_subparsers(dest="node_cmd", help="节点子命令")

    # node run
    node_run = node_sub.add_parser("run", help="运行单个节点")
    node_run.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_run.add_argument("--node", required=True, help="节点 ID")
    node_run.add_argument("--models", help="模型配置文件路径")
    node_run.add_argument("--force", action="store_true", help="强制重新运行（重置后再跑）")

    # node reset
    node_reset = node_sub.add_parser("reset", help="重置节点")
    node_reset.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_reset.add_argument("--node", required=True, help="节点 ID")

    # node logs
    node_logs = node_sub.add_parser("logs", help="查看节点日志")
    node_logs.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_logs.add_argument("--node", required=True, help="节点 ID")
    node_logs.add_argument("--tail", type=int, default=50, help="显示最后 N 行")

    # node history
    node_history = node_sub.add_parser("history", help="查看节点对话历史")
    node_history.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_history.add_argument("--node", required=True, help="节点 ID")
    node_history.add_argument("--json", action="store_true", help="JSON 输出")

    # ============================================================
    # models 命令组
    # ============================================================
    models_parser = subparsers.add_parser("models", help="模型管理")
    models_sub = models_parser.add_subparsers(dest="models_cmd", help="模型子命令")

    # models list
    models_list = models_sub.add_parser("list", help="列出可用模型")
    models_list.add_argument("--config", help="模型配置文件路径（默认 AGENDA_MODELS）")
    models_list.add_argument("--json", action="store_true", help="JSON 输出")

    # models validate
    models_validate = models_sub.add_parser("validate", help="验证模型配置")
    models_validate.add_argument("--config", help="模型配置文件路径（默认 AGENDA_MODELS）")

    # ============================================================
    # 解析命令
    # ============================================================
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return EXIT_SUCCESS

    # ============================================================
    # dag 子命令处理
    # ============================================================
    if args.cmd == "dag":
        if not args.dag_cmd:
            dag_parser.print_help()
            return EXIT_SUCCESS

        # dag init
        if args.dag_cmd == "init":
            dag_path = _resolve_dag_path(args.path) if args.path else Path(os.environ.get("AGENDA_DAG", "./dag.yaml"))
            dag_dir = dag_path.parent
            dag_dir.mkdir(parents=True, exist_ok=True)
            (dag_dir / "nodes").mkdir(exist_ok=True)

            if not dag_path.exists():
                dag_path.write_text(
                    "dag:\n  name: untitled\n  max_parallel: 4\nnodes:\n",
                    encoding="utf-8",
                )
            print(f"[dag init] 已初始化 DAG: {dag_path}")
            return EXIT_SUCCESS

        # dag validate
        if args.dag_cmd == "validate":
            dag_path = _resolve_dag_path(args.path)
            try:
                scheduler = _load_scheduler(dag_path)
                nodes = scheduler.dag.get("nodes", {})

                # 环检测
                cycle = scheduler._detect_cycle()
                cycle_error = None
                if cycle:
                    cycle_error = f"检测到循环依赖: {' -> '.join(cycle)} -> {cycle[0]}"

                # 检查模型配置
                models_used = set()
                for node_id, config in nodes.items():
                    model = config.get("model")
                    if model:
                        models_used.add(model)

                # 检查输入文件是否存在
                warnings = []
                for node_id, config in nodes.items():
                    for inp in config.get("inputs", []):
                        src = scheduler.dag_dir / inp.split("#")[0].lstrip("/")
                        if not src.exists():
                            warnings.append(f"节点 {node_id} 的输入文件不存在: {inp}")

                result = {
                    "valid": cycle is None and len(warnings) == 0,
                    "path": str(dag_path),
                    "nodes": len(nodes),
                    "models": sorted(models_used),
                    "warnings": warnings,
                }
                if cycle_error:
                    result["cycle_error"] = cycle_error
                    result["valid"] = False

                if args.json:
                    _json_out(result)
                else:
                    print(f"DAG: {dag_path}")
                    print(f"  节点数: {result['nodes']}")
                    print(f"  使用模型: {', '.join(result['models']) or 'default'}")
                    if cycle_error:
                        print(f"  ❌ {cycle_error}")
                    print(f"  警告: {len(warnings)}")
                    for w in warnings:
                        print(f"    ⚠️ {w}")
                    print(f"  验证结果: {'✅ 通过' if result['valid'] else '❌ 失败'}")

                return EXIT_SUCCESS if result["valid"] else EXIT_DAG_CONFIG_ERROR

            except Exception as e:
                if args.json:
                    _json_out({"valid": False, "error": str(e)})
                else:
                    print(f"[错误] 验证失败: {e}")
                return EXIT_DAG_CONFIG_ERROR

        # dag inspect
        if args.dag_cmd == "inspect":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            nodes = scheduler.dag.get("nodes", {})

            # 拓扑排序
            topo = scheduler.topological_sort()

            # 计算拓扑深度
            depth = {n: 0 for n in nodes}
            changed = True
            while changed:
                changed = False
                for n, cfg in nodes.items():
                    for dep in cfg.get("deps", []):
                        if dep in depth and depth[dep] + 1 > depth[n]:
                            depth[n] = depth[dep] + 1
                            changed = True

            # 关键路径（最长路径）
            critical_path = []
            if topo:
                # 找最长路径的简单方法：从深度最大的节点回溯
                max_node = max(depth, key=depth.get)
                critical_path = [max_node]
                while True:
                    current = critical_path[-1]
                    deps = nodes.get(current, {}).get("deps", [])
                    if not deps:
                        break
                    # 找深度最大的依赖
                    next_node = max((d for d in deps if d in depth), key=lambda d: depth[d], default=None)
                    if next_node is None:
                        break
                    critical_path.append(next_node)
                critical_path.reverse()

            result = {
                "path": str(dag_path),
                "topological_order": topo,
                "nodes": {
                    n: {
                        "deps": cfg.get("deps", []),
                        "model": cfg.get("model", "default"),
                        "depth": depth.get(n, 0),
                    }
                    for n, cfg in nodes.items()
                },
                "max_depth": max(depth.values()) if depth else 0,
                "critical_path": critical_path,
            }

            if args.json:
                _json_out(result)
            else:
                print(f"DAG: {dag_path}")
                print(f"  总节点: {len(nodes)}")
                print(f"  拓扑排序: {' -> '.join(topo)}")
                print(f"  最大深度: {result['max_depth']}")
                print(f"  关键路径: {' -> '.join(critical_path) or 'N/A'}")
                print(f"  节点列表:")
                for n in topo:
                    info = result["nodes"][n]
                    deps = f" 依赖: {', '.join(info['deps'])}" if info["deps"] else ""
                    print(f"    [{info['depth']}] {n} (模型: {info['model']}){deps}")

            return EXIT_SUCCESS

        # dag run
        if args.dag_cmd == "run":
            dag_path = _resolve_dag_path(args.path)
            models_path = _resolve_models_path(args.models)
            max_parallel = args.max_parallel or int(os.environ.get("AGENDA_MAX_PARALLEL", "4"))

            if args.dry_run:
                scheduler = _load_scheduler(dag_path, models_path)
                print(f"[dry-run] DAG: {dag_path}")
                print(f"[dry-run] 模型: {models_path or 'default'}")
                print(f"[dry-run] 最大并行: {max_parallel}")
                topo = scheduler.topological_sort()
                print(f"[dry-run] 拓扑顺序: {' -> '.join(topo)}")
                cycle = scheduler._detect_cycle()
                if cycle:
                    print(f"[dry-run] ⚠️ 检测到环: {' -> '.join(cycle)}")
                print(f"[dry-run] 节点:")
                for n, cfg in scheduler.dag.get("nodes", {}).items():
                    print(f"  {n}: model={cfg.get('model', 'default')}, deps={cfg.get('deps', [])}")
                return EXIT_SUCCESS

            try:
                scheduler = _load_scheduler(dag_path, models_path)
                scheduler.dag["dag"]["max_parallel"] = max_parallel

                results = asyncio.run(scheduler.run(
                    tools_factory=lambda session: build_tools(session),
                ))

                failed = [n for n, s in results.items() if s == "FAILED"]
                if failed:
                    print(f"[dag run] 失败节点: {', '.join(failed)}")
                    return EXIT_DEPENDENCY_ERROR

                print(f"[dag run] 全部完成: {len(results)} 个节点")
                return EXIT_SUCCESS

            except KeyboardInterrupt:
                print("\n[dag run] 已中断")
                return 130
            except Exception as e:
                print(f"[dag run] 错误: {e}")
                return EXIT_EXECUTION_ERROR

        # dag status
        if args.dag_cmd == "status":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            nodes = scheduler.dag.get("nodes", {})

            if args.watch:
                try:
                    while True:
                        completed = [n for n in nodes if scheduler.node_is_done(n)]
                        failed = [n for n in nodes if scheduler.node_is_failed(n)]
                        running = [n for n in nodes if scheduler.node_is_running(n)]
                        pending = [n for n in nodes if n not in completed and n not in failed and n not in running]

                        event = {
                            "ts": _now_iso(),
                            "dag": str(dag_path),
                            "completed": len(completed),
                            "total": len(nodes),
                            "running": running,
                            "failed": failed,
                            "pending": pending,
                        }

                        if args.json:
                            _ndjson_out(event)
                        else:
                            print(f"\r[{len(completed)}/{len(nodes)}] 运行中: {', '.join(running) or '无'}  失败: {', '.join(failed) or '无'}", end="", flush=True)

                        if len(completed) + len(failed) == len(nodes):
                            if not args.json:
                                print()
                            break

                        time.sleep(1)
                    return EXIT_SUCCESS
                except KeyboardInterrupt:
                    if not args.json:
                        print()
                    print("[status] 监听已停止")
                    return 130

            # 单次查询模式
            completed = [n for n in nodes if scheduler.node_is_done(n)]
            failed = [n for n in nodes if scheduler.node_is_failed(n)]
            running = [n for n in nodes if scheduler.node_is_running(n)]
            pending = [n for n in nodes if n not in completed and n not in failed and n not in running]

            result = {
                "dag": scheduler.dag.get("dag", {}).get("name", "untitled"),
                "path": str(dag_path),
                "completed": len(completed),
                "total": len(nodes),
                "running": [{"node": n, "model": nodes[n].get("model", "default")} for n in running],
                "failed": [{"node": n, "model": nodes[n].get("model", "default")} for n in failed],
                "pending": [{"node": n, "model": nodes[n].get("model", "default")} for n in pending],
            }

            if args.json:
                _json_out(result)
            else:
                print(f"DAG: {result['dag']}")
                print(f"  总节点: {result['total']}")
                print(f"  已完成: {result['completed']}")
                print(f"  运行中: {len(result['running'])}")
                for n in result["running"]:
                    print(f"    ⏳ {n['node']} (模型: {n['model']})")
                print(f"  失败: {len(result['failed'])}")
                for n in result["failed"]:
                    print(f"    ❌ {n['node']} (模型: {n['model']})")
                print(f"  等待中: {len(result['pending'])}")
                for n in result["pending"]:
                    print(f"    📋 {n['node']} (模型: {n['model']})")

            return EXIT_SUCCESS

        # dag stop
        if args.dag_cmd == "stop":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            scheduler.cancel()
            print(f"[dag stop] 已发送取消信号到 DAG: {dag_path}")
            return EXIT_SUCCESS

    # ============================================================
    # node 子命令处理
    # ============================================================
    if args.cmd == "node":
        if not args.node_cmd:
            node_parser.print_help()
            return EXIT_SUCCESS

        dag_path = _resolve_dag_path(args.path)
        scheduler = _load_scheduler(dag_path)
        node_id = args.node

        # node run
        if args.node_cmd == "run":
            models_path = _resolve_models_path(args.models)
            if args.force:
                node_dir = scheduler.nodes_dir / node_id
                if node_dir.exists():
                    shutil.rmtree(node_dir)
                    print(f"[node run] 已重置节点: {node_id}")

            try:
                results = asyncio.run(scheduler._run_node(
                    node_id,
                    tools_factory=lambda session: build_tools(session),
                    hooks_factory=None,
                ))
                return EXIT_SUCCESS
            except Exception as e:
                print(f"[node run] 错误: {e}")
                return EXIT_EXECUTION_ERROR

        # node reset
        if args.node_cmd == "reset":
            node_dir = scheduler.nodes_dir / node_id
            if node_dir.exists():
                shutil.rmtree(node_dir)
                print(f"[node reset] 已重置节点: {node_id}")
            else:
                print(f"[node reset] 节点不存在: {node_id}")
            return EXIT_SUCCESS

        # node logs
        if args.node_cmd == "logs":
            error_log = scheduler.nodes_dir / node_id / ".system" / "error.log"
            if error_log.exists():
                lines = error_log.read_text(encoding="utf-8").splitlines()
                for line in lines[-args.tail:]:
                    print(line)
            else:
                print("(无错误日志)")
            return EXIT_SUCCESS

        # node history
        if args.node_cmd == "history":
            session = Session(scheduler.nodes_dir / node_id)
            messages = session.load_messages()

            if not messages:
                print("(无对话历史)")
                return EXIT_SUCCESS

            if args.json:
                _json_out({"node": node_id, "messages": messages})
            else:
                print(f"节点 {node_id} 的对话历史 ({len(messages)} 条):")
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = json.dumps(content, ensure_ascii=False)[:200]
                    else:
                        content = str(content)[:200]
                    print(f"  [{role}] {content}...")

            return EXIT_SUCCESS

    # ============================================================
    # models 子命令处理
    # ============================================================
    if args.cmd == "models":
        if not args.models_cmd:
            models_parser.print_help()
            return EXIT_SUCCESS

        models_path = _resolve_models_path(args.config)

        # models list
        if args.models_cmd == "list":
            registry = ModelRegistry()
            if models_path and models_path.exists():
                registry.load(models_path.parent)
            else:
                registry.load()

            result = []
            for name, cfg in registry._models.items():
                result.append({
                    "name": name,
                    "model": cfg.model,
                    "base_url": cfg.base_url,
                    "token_cap": cfg.token_cap,
                })

            if args.json:
                _json_out({"models": result})
            else:
                print("可用模型:")
                for m in result:
                    print(f"  {m['name']}: {m['model']} @ {m['base_url']} (token_cap: {m['token_cap']})")

            return EXIT_SUCCESS

        # models validate
        if args.models_cmd == "validate":
            registry = ModelRegistry()
            if models_path and models_path.exists():
                registry.load(models_path.parent)
            else:
                registry.load()

            print(f"验证 {len(registry._models)} 个模型配置...")
            for name, cfg in registry._models.items():
                if not cfg.api_key:
                    print(f"  ❌ {name}: API key 未设置")
                else:
                    print(f"  ✅ {name}: {cfg.model} @ {cfg.base_url}")

            return EXIT_SUCCESS

    # 未知命令
    print(f"[错误] 未知命令: {args.cmd}")
    return EXIT_ARGS_ERROR


# ============================================================
# 11. 示例用法
# ============================================================

"""
# 示例 1：初始化工作区
python agenda.py dag init ./my_book

# 示例 2：定义 DAG（手写 dag.yaml）
cat > my_book/dag.yaml << 'EOF'
dag:
  name: "Hermes vs OpenClaw"
  max_parallel: 4

nodes:
  ch01_intro:
    prompt: "写第一章：Agent 爆发背景"
    inputs:
      - "meta/outline.md"

  ch03_hermes:
    prompt: "写第三章：Hermes Agent 深度解析"
    deps: [ch01_intro]
    inputs:
      - "meta/outline.md"
    dep_inputs:
      - from: "ch01_intro/output/draft.md"
        to: "input/deps/ch01_intro/draft.md"
EOF

# 示例 3：验证 DAG
python agenda.py dag validate ./my_book

# 示例 4：预演
python agenda.py dag run ./my_book --dry-run

# 示例 5：运行 DAG
python agenda.py dag run ./my_book --max-parallel 4

# 示例 6：查看状态
python agenda.py dag status ./my_book --watch --json

# 示例 7：Python API 调用
import asyncio
from agenda import DAGScheduler, build_tools

async def main():
    scheduler = DAGScheduler("./workspace", "my_book").load()
    results = await scheduler.run(
        tools_factory=lambda session: build_tools(session, allow_shell=False),
    )
    print(results)

asyncio.run(main())
"""

if __name__ == "__main__":
    sys.exit(cli())
