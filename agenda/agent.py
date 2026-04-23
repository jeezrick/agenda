from __future__ import annotations

"""Agent Loop — 核心循环（加固版）。"""

import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime
from typing import Any

from .const import DEFAULT_MAX_ITERATIONS, DEFAULT_NODE_TIMEOUT
from .session import Session
from .models import ModelRegistry
from .hooks import HookRegistry
from .tools import ToolRegistry


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

