from __future__ import annotations

"""Agent Loop — 核心循环（加固版）。

学 Butterfly + EVA + Kimi Code 的加固措施：
- max_iterations: 防止无限循环（默认 50）
- timeout: 节点级超时（默认 600s）
- turn 级别持久化（每轮后 save_turn，取消时 save_partial_turn）
- IPC 事件轮询（每轮前检查 events.jsonl，支持外部中断/消息）
- 取消时补全 pending tool_calls（防止下次运行 400）
- 系统驱动记忆压缩（SimpleCompaction，保留最近 N 条，LLM 生成结构化摘要）
"""

import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime
from typing import Any

from .compaction import SimpleCompaction, estimate_text_tokens, should_auto_compact
from .const import (
    DEFAULT_COMPACTION_RESERVED,
    DEFAULT_COMPACTION_TRIGGER_RATIO,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_NODE_TIMEOUT,
)
from .session import Session
from .models import ModelRegistry
from .tools import ToolRegistry


class AgentLoop:
    """
    Agent 的核心循环：
        prompt → LLM → (tool_call → execute → loop) → completion

    事件流：
        1. 恢复历史（turns.jsonl replay）
        2. while 迭代:
           a. 检查 IPC 事件（interrupt / message）
           b. 记忆压缩检查
           c. 调用 LLM
           d. 如果有 tool_calls，执行 tools
           e. 一轮结束 save_turn
        3. 返回最终产物
    """

    def __init__(
        self,
        session: Session,
        model_registry: ModelRegistry,
        tools: ToolRegistry,
        model: str | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout: float = DEFAULT_NODE_TIMEOUT,
        node_id: str | None = None,
    ) -> None:
        self.session = session
        self.model_registry = model_registry
        self.model_cfg = model_registry.get(model)
        self.tools = tools
        self.token_cap = self.model_cfg.token_cap
        self.messages: list[dict] = []
        self.max_iterations = max(1, max_iterations)
        self.timeout = timeout
        self.node_id = node_id
        self._clients: dict[tuple, Any] = {}
        self._cancelled = False
        self._events_offset: int = 0

    async def run(self, system_prompt: str, task: str) -> str:
        """运行 Agent，返回最终产物。支持超时、取消、IPC。"""
        # 1. 恢复历史
        loaded = self.session.replay_history()
        if loaded:
            # 保留 system prompt（可能和上次不同），其余从 turns 恢复
            self.messages = [{"role": "system", "content": system_prompt}]
            # 跳过已恢复消息中的 system
            for msg in loaded:
                if msg.get("role") != "system":
                    self.messages.append(msg)
            print(f"  [恢复] 从 turns.jsonl 加载 {len(loaded)} 条历史消息")
        else:
            self.messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

        # 初始化 IPC offset
        self._events_offset = self.session.events_size()

        start_time = time.monotonic()
        iteration = 0
        turn_start_idx = len(self.messages)  # 本轮起始位置

        try:
            while iteration < self.max_iterations:
                iteration += 1

                # 超时检查
                if time.monotonic() - start_time > self.timeout:
                    raise asyncio.TimeoutError(f"节点运行超过 {self.timeout} 秒")

                # 取消检查
                if self._cancelled:
                    raise asyncio.CancelledError("Agent 被取消")

                # IPC 事件轮询（学 Butterfly 的 poll_inputs）
                await self._poll_events()

                # 系统驱动记忆压缩（学 Kimi Code）
                token_count = estimate_text_tokens(self.messages)
                if should_auto_compact(
                    token_count,
                    self.token_cap,
                    trigger_ratio=DEFAULT_COMPACTION_TRIGGER_RATIO,
                    reserved_context_size=DEFAULT_COMPACTION_RESERVED,
                ):
                    print(f"  [压缩] Context too long ({token_count} tokens), compacting...")
                    try:
                        await self._compact_context(system_prompt)
                        turn_start_idx = len(self.messages)
                    except Exception as compact_err:
                        print(f"  [压缩失败] {type(compact_err).__name__}: {compact_err}")
                        raise

                # 调用 LLM
                response = await self._call_llm()
                msg = response["choices"][0]["message"]
                msg_dict = self._msg_to_dict(msg)
                self.messages.append(msg_dict)

                # 完成信号
                if not msg_dict.get("tool_calls"):
                    # 一轮完整结束，save_turn
                    self.session.save_turn({
                        "type": "turn",
                        "messages": self.messages[turn_start_idx:],
                        "iteration": iteration,
                        "ts": datetime.now().isoformat(),
                    })
                    result = msg_dict.get("content", "")
                    return result

                # 执行 tools
                pending_tool_calls: list[dict] = msg_dict.get("tool_calls", [])
                for tc in pending_tool_calls:
                    result = await self._execute_tool(tc)
                    tool_result = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result)[:4000],
                    }
                    self.messages.append(tool_result)

                # 发送 progress 事件到 IPC（让外部观察者知道进度）
                self.session.append_event({
                    "type": "progress",
                    "node_id": self.node_id,
                    "iteration": iteration,
                    "tool": pending_tool_calls[-1]["function"]["name"] if pending_tool_calls else None,
                })

            # 迭代次数超限
            raise RuntimeError(f"Agent 迭代次数达到上限 {self.max_iterations}")

        except asyncio.CancelledError:
            # Butterfly 式修复：保存 partial turn + 补全 orphan tool_calls
            self._seal_orphan_tool_calls()
            committed = self.messages[turn_start_idx:]
            if committed:
                self.session.save_partial_turn(committed, iteration, interrupted=True)
            raise
        except Exception as e:
            self.session.write_system("error.log", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise

    def cancel(self) -> None:
        """标记取消（由外部调度器调用）。"""
        self._cancelled = True

    # --- IPC 事件轮询 ---

    async def _poll_events(self) -> None:
        """检查 events.jsonl 中的新事件。"""
        events, self._events_offset = self.session.poll_events(self._events_offset)
        for event in events:
            etype = event.get("type")
            if etype == "interrupt":
                source = event.get("from", "unknown")
                print(f"  [IPC] 收到来自 {source} 的中断信号")
                self._cancelled = True
            elif etype == "message":
                source = event.get("from", "unknown")
                content = event.get("content", "")
                print(f"  [IPC] 收到来自 {source} 的消息")
                self.messages.append({
                    "role": "user",
                    "content": f"[{source}] {content}",
                })

    # --- 内部方法 ---

    def _get_client(self, cfg: Any) -> Any:
        """获取或创建 OpenAI 兼容客户端（按配置缓存）。"""
        key = (cfg.base_url, cfg.model)
        if key not in self._clients:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError("需要安装 openai: pip install openai") from exc
            self._clients[key] = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key)
        return self._clients[key]

    async def _call_llm(self) -> dict:
        """调用 LLM API。支持 fallback 模型。"""
        cfg = self.model_cfg
        try:
            return await self._call_llm_with_cfg(cfg)
        except Exception as primary_exc:
            # 只有网络/服务端错误才 fallback，代码错误直接抛
            if not self._is_fallbackable_error(primary_exc):
                raise
            fb_model = cfg.fallback_model
            if not fb_model:
                raise
            print(f"  [Fallback] 主模型失败 ({type(primary_exc).__name__})，切换备用模型: {fb_model}")
            fb_cfg = self.model_registry.get(fb_model)
            return await self._call_llm_with_cfg(fb_cfg)

    def _is_fallbackable_error(self, exc: Exception) -> bool:
        """判断错误是否可 fallback（网络/服务端错误）。"""
        import openai
        if isinstance(exc, (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.InternalServerError,
            openai.RateLimitError,
        )):
            return True
        # OSError 包含连接错误
        if isinstance(exc, OSError):
            return True
        return False

    async def _call_llm_with_cfg(self, cfg: Any) -> dict:
        """用指定配置调用 LLM。"""
        client = self._get_client(cfg)
        kwargs = {
            "model": cfg.model,
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
        补全 synthetic tool_result，防止下次运行 LLM 返回 400。
        """
        if not self.messages:
            return
        last = self.messages[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return
        for tc in last["tool_calls"]:
            tc_id = tc.get("id", "")
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

    async def _compact_context(self, system_prompt: str) -> None:
        """系统驱动记忆压缩（学 Kimi Code CLI）。"""
        compactor = SimpleCompaction(max_preserved_messages=2)
        compacted = await compactor.compact(
            self.messages,
            client=self._get_client(self.model_cfg),
            model=self.model_cfg.model,
        )

        # 重建 context：rotate 旧文件 → 重写 system prompt → 写入压缩结果
        backup = self.session.rotate_turns()
        if backup:
            print(f"  [压缩] 旧 turns 已 rotate 到 {backup.name}")

        self.messages = list(compacted.messages)
        self.session.clear_turns()
        self.session.write_system_turn(system_prompt)
        self.session.save_turn({
            "type": "turn",
            "messages": list(self.messages),
            "compact": True,
            "ts": datetime.now().isoformat(),
        })
        print(f"  [压缩完成] 保留 {len(self.messages)} 条消息")

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
