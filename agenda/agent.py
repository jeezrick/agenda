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
import random
import time
import traceback
from datetime import datetime
from typing import Any

from .compaction import SimpleCompaction, estimate_text_tokens, should_auto_compact
from .const import (
    DEFAULT_COMPACTION_MAX_PRESERVED,
    DEFAULT_COMPACTION_RESERVED,
    DEFAULT_COMPACTION_TRIGGER_RATIO,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_NODE_TIMEOUT,
    MAX_PARALLEL_TOOLS,
)
from .models import ModelRegistry
from .session import Session
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
        stream: bool = True,
        hooks: Any = None,
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
        self.stream = stream and self.model_cfg.stream
        self.hooks = hooks
        self.approval_required: bool = False
        self.approval_tools: list[str] = []
        self.approval_timeout: float = 300.0
        self._compact_model_cfg = (
            model_registry.get(self.model_cfg.compact_model) if self.model_cfg.compact_model else None
        )
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
                if self.hooks:
                    await self.hooks.emit(
                        "on_turn_start",
                        iteration=iteration,
                        node_id=self.node_id,
                        token_count=estimate_text_tokens(self.messages),
                    )
                response = await self._call_llm()
                choice = response["choices"][0]
                finish_reason = choice.get("finish_reason")
                if finish_reason == "insufficient_system_resource":
                    raise RuntimeError("[DeepSeek] 系统推理资源不足，生成被打断")

                # 记录 usage（prompt cache、reasoning tokens 等）
                usage = response.get("usage")
                if usage:
                    cache_info = ""
                    if "prompt_cache_hit_tokens" in usage:
                        hit = usage["prompt_cache_hit_tokens"]
                        miss = usage.get("prompt_cache_miss_tokens", 0)
                        cache_info = f" cache_hit={hit} miss={miss}"
                    if "completion_tokens_details" in usage:
                        details = usage["completion_tokens_details"]
                        if details and "reasoning_tokens" in details:
                            cache_info += f" reasoning={details['reasoning_tokens']}"
                    if cache_info:
                        print(
                            f"  [Usage] in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}{cache_info}"
                        )

                msg = choice["message"]
                msg_dict = self._msg_to_dict(msg)
                self.messages.append(msg_dict)

                # 完成信号
                if not msg_dict.get("tool_calls"):
                    # 一轮完整结束，save_turn
                    self.session.save_turn(
                        {
                            "type": "turn",
                            "messages": self.messages[turn_start_idx:],
                            "iteration": iteration,
                            "ts": datetime.now().isoformat(),
                        }
                    )
                    result: str = msg_dict.get("content", "")
                    return result

                # 执行 tools（并行：asyncio.gather）
                pending_tool_calls: list[dict] = msg_dict.get("tool_calls", [])
                batches = [
                    pending_tool_calls[i : i + MAX_PARALLEL_TOOLS]
                    for i in range(0, len(pending_tool_calls), MAX_PARALLEL_TOOLS)
                ]
                for batch in batches:
                    if self.hooks:
                        for tc in batch:
                            await self.hooks.emit(
                                "on_tool_call",
                                node_id=self.node_id,
                                tool=tc["function"]["name"],
                                args=tc["function"].get("arguments", "{}"),
                            )
                    results = await asyncio.gather(
                        *[self._execute_tool(tc) for tc in batch],
                        return_exceptions=True,
                    )
                    for tc, raw_result in zip(batch, results, strict=False):
                        result_text = (
                            f"[执行错误] {type(raw_result).__name__}: {raw_result}"
                            if isinstance(raw_result, Exception)
                            else str(raw_result)
                        )
                        tool_result = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_text[:4000],
                        }
                        self.messages.append(tool_result)

                # 发送 progress 事件到 IPC（让外部观察者知道进度）
                self.session.append_event(
                    {
                        "type": "progress",
                        "node_id": self.node_id,
                        "iteration": iteration,
                        "tool": pending_tool_calls[-1]["function"]["name"] if pending_tool_calls else None,
                    }
                )

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
                self.messages.append(
                    {
                        "role": "user",
                        "content": f"[{source}] {content}",
                    }
                )

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
        """调用 LLM API。支持 fallback 模型和流式输出。"""
        cfg = self.model_cfg
        try:
            return await self._call_llm_with_cfg(cfg, stream=self.stream)
        except Exception as primary_exc:
            if not self._is_fallbackable_error(primary_exc):
                raise
            fb_model = cfg.fallback_model
            if not fb_model:
                raise
            print(f"  [Fallback] 主模型失败 ({type(primary_exc).__name__})，切换备用模型: {fb_model}")
            fb_cfg = self.model_registry.get(fb_model)
            return await self._call_llm_with_cfg(fb_cfg, stream=self.stream)

    def _is_fallbackable_error(self, exc: Exception) -> bool:
        """判断错误是否可 fallback（网络/服务端错误）。"""
        import openai

        if isinstance(
            exc,
            (
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.InternalServerError,
                openai.RateLimitError,
            ),
        ):
            return True
        return bool(isinstance(exc, OSError))

    async def _call_llm_with_cfg(self, cfg: Any, *, stream: bool = False) -> dict:
        """用指定配置调用 LLM。stream=True 时走流式路径。"""
        if stream:
            return await self._call_llm_stream(cfg)
        return await self._call_llm_batch(cfg)

    async def _call_llm_batch(self, cfg: Any) -> dict:
        """非流式调用 LLM（原 _call_llm_with_cfg 逻辑）。"""
        client = self._get_client(cfg)
        kwargs = self._build_llm_kwargs(cfg)
        resp = await client.chat.completions.create(**kwargs)
        return resp.model_dump()  # type: ignore[no-any-return]

    async def _call_llm_stream(self, cfg: Any) -> dict:
        """流式调用 LLM，逐 chunk 输出到 stdout，返回完整响应 dict。"""
        client = self._get_client(cfg)
        kwargs = self._build_llm_kwargs(cfg)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        tool_call_deltas: dict[int, dict[str, Any]] = {}  # index -> accumulated
        usage: dict | None = None

        prefix = f"[{self.node_id}] " if self.node_id else ""
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                # 最后一个 chunk（只含 usage，无 choices）
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage.model_dump()
                continue

            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                print(f"{prefix}{delta.content}", end="", flush=True)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_deltas:
                        tool_call_deltas[idx] = {
                            "id": tc_delta.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    acc = tool_call_deltas[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["function"]["arguments"] += tc_delta.function.arguments

        if content_parts:
            print()  # 换行结束流式输出

        content = "".join(content_parts)
        tool_calls = [tool_call_deltas[i] for i in sorted(tool_call_deltas)] if tool_call_deltas else None

        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": usage,
        }

    def _build_llm_kwargs(self, cfg: Any) -> dict[str, Any]:
        """构建 LLM API 调用参数（公共逻辑）。"""
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": self.messages,
            "temperature": cfg.temperature,
        }
        if cfg.max_tokens:
            kwargs["max_tokens"] = cfg.max_tokens
        if cfg.extra_params:
            kwargs.update(cfg.extra_params)
        if self.tools._tools:
            kwargs["tools"] = self.tools.schemas()
            kwargs["tool_choice"] = "auto"

        standard_keys = {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "tools",
            "tool_choice",
            "stream",
            "stream_options",
            "stop",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "logprobs",
            "top_logprobs",
            "response_format",
            "n",
            "user",
        }
        extra_body = {k: v for k, v in kwargs.items() if k not in standard_keys}
        standard_kwargs = {k: v for k, v in kwargs.items() if k in standard_keys}
        if extra_body:
            standard_kwargs["extra_body"] = extra_body
        return standard_kwargs

    async def _request_approval(self, tool_name: str, args_json: str) -> bool:
        """请求人工审批工具调用。轮询等待批准/拒绝/超时。"""
        self.session.request_approval(tool_name, args_json)
        print(f"  [审批] 等待人工批准: {tool_name}")

        start = asyncio.get_event_loop().time()
        checked = 0
        while True:
            events, new_offset = self.session.poll_events(checked)
            for e in events[checked:]:
                if e.get("type") == "approval":
                    decision = str(e.get("decision", "rejected"))
                    print(f"  [审批] {tool_name}: {decision}")
                    self._events_offset = new_offset
                    return decision == "approved"
            checked = len(events) if events else checked
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > self.approval_timeout:
                print(f"  [审批] {tool_name}: 超时")
                return False
            await asyncio.sleep(0.5)

    async def _execute_tool(self, tc: dict) -> str:
        """执行单个 tool call。支持人工审批门。"""
        func = tc["function"]
        name = func["name"]
        args = json.loads(func["arguments"]) if func["arguments"] else {}
        args_json = json.dumps(args, ensure_ascii=False)[:200]
        print(f"  [Tool] {name}({args_json})")

        # 审批检查：需要审批且未批准则拒绝
        if self.approval_required:
            approval_tools = self.approval_tools if self.approval_tools else [name]
            if name in approval_tools:
                approved = await self._request_approval(name, args_json)
                if not approved:
                    return f"[审批] 工具调用 {name} 被拒绝或超时"

        tool = self.tools.get(name)
        if not tool:
            return f"[错误] 未知工具: {name}"

        try:
            if asyncio.iscoroutinefunction(tool):
                return await tool(**args)  # type: ignore[no-any-return]
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
            has_result = any(m.get("role") == "tool" and m.get("tool_call_id") == tc_id for m in self.messages)
            if not has_result:
                synthetic = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "[系统] 工具调用因任务中断而被取消。",
                }
                self.messages.append(synthetic)

    async def _compact_context(self, system_prompt: str) -> None:
        """系统驱动记忆压缩（学 Kimi Code CLI）。

        改进：专用压缩模型 + 压缩后验证 + 截断回退。
        """
        max_retries = 3
        base_delay = 1.0

        # 使用专用压缩模型（配置了 compact_model 时），否则回退到主模型
        compact_cfg = self._compact_model_cfg or self.model_cfg
        compact_client = self._get_client(compact_cfg)
        compact_model = compact_cfg.model
        if self._compact_model_cfg:
            print(f"  [压缩] 使用专用压缩模型: {compact_model}")

        pre_token_count = estimate_text_tokens(self.messages)

        for attempt in range(max_retries):
            try:
                compactor = SimpleCompaction(max_preserved_messages=DEFAULT_COMPACTION_MAX_PRESERVED)
                compacted = await compactor.compact(
                    self.messages,
                    client=compact_client,
                    model=compact_model,
                )

                # 压缩后验证
                if not SimpleCompaction.validate_compacted(compacted, self.messages, self.token_cap):
                    raise RuntimeError("压缩验证失败：产物无效或 token 膨胀")

                # 重建 context：rotate 旧文件 → 重写 system prompt → 写入压缩结果
                backup = self.session.rotate_turns()
                if backup:
                    print(f"  [压缩] 旧 turns 已 rotate 到 {backup.name}")

                self.messages = list(compacted.messages)
                self.session.clear_turns()
                self.session.write_system_turn(system_prompt)
                self.session.save_turn(
                    {
                        "type": "turn",
                        "messages": list(self.messages),
                        "compact": True,
                        "ts": datetime.now().isoformat(),
                    }
                )

                post_token_count = estimate_text_tokens(self.messages)
                usage_info = ""
                if compacted.usage:
                    u = compacted.usage
                    usage_info = f" (LLM: {u['input']}→{u['output']} tok)"
                print(
                    f"  [压缩完成] {pre_token_count} → {post_token_count} tokens"
                    f"{usage_info}, 保留 {len(self.messages)} 条消息"
                )
                if self.hooks:
                    await self.hooks.emit(
                        "on_compaction",
                        node_id=self.node_id,
                        pre_tokens=pre_token_count,
                        post_tokens=post_token_count,
                        success=True,
                    )
                return

            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt) + random.uniform(0, 1)
                    print(f"  [压缩重试 {attempt + 1}/{max_retries}] {type(e).__name__}: {e}，{delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)
                else:
                    break

        # 所有重试失败 → 截断回退
        print(f"  [压缩] LLM 压缩失败，回退到截断策略 (原 {pre_token_count} tokens)")
        target = int(self.token_cap * 0.7)
        truncated = SimpleCompaction.truncate_messages(self.messages, max_tokens=target)
        self.messages = truncated
        post_token_count = estimate_text_tokens(self.messages)
        print(f"  [截断完成] {pre_token_count} → {post_token_count} tokens, 保留 {len(self.messages)} 条消息")
        if self.hooks:
            await self.hooks.emit(
                "on_compaction",
                node_id=self.node_id,
                pre_tokens=pre_token_count,
                post_tokens=post_token_count,
                success=False,
                fallback="truncation",
            )

    def _msg_to_dict(self, msg: Any) -> dict:
        """把 LLM 返回的消息对象转成 dict。"""
        if isinstance(msg, dict):
            return msg
        d: dict[str, Any] = {"role": getattr(msg, "role", "assistant")}
        if hasattr(msg, "content") and msg.content:
            d["content"] = msg.content
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            d["reasoning_content"] = msg.reasoning_content
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            d["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        return d
