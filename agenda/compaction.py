from __future__ import annotations

"""Context compaction — 系统驱动记忆压缩。

学 Kimi Code CLI 的设计：
- should_auto_compact: 双策略触发（ratio + reserved）
- SimpleCompaction: 保留最近 N 条消息，前面的交给 LLM 生成结构化摘要
- 压缩 LLM 使用固定 system prompt，不赋予工具调用能力
"""

import json
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any

CompactionResult = namedtuple("CompactionResult", ["messages", "usage"])


def estimate_text_tokens(messages: list[dict]) -> int:
    """估算消息列表的 token 数。使用字符数 // 4 的启发式方法。"""
    total_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        total_chars += len(content)
        # tool_calls 也计入
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            total_chars += len(func.get("name", ""))
            total_chars += len(func.get("arguments", ""))
    # ~4 chars per token for English; somewhat underestimates for CJK text,
    # but this is a temporary estimate that gets corrected on the next LLM call.
    return total_chars // 4


def should_auto_compact(
    token_count: int,
    max_context_size: int,
    *,
    trigger_ratio: float = 0.75,
    reserved_context_size: int = 2048,
) -> bool:
    """判断是否应该自动触发压缩。

    Returns True when either condition is met:
    - Ratio-based: token_count >= max_context_size * trigger_ratio
    - Reserved-based: token_count + reserved_context_size >= max_context_size
    """
    return (
        token_count >= max_context_size * trigger_ratio
        or token_count + reserved_context_size >= max_context_size
    )


class SimpleCompaction:
    """简单压缩策略：保留最近 N 条 user/assistant 消息，压缩前面的历史。"""

    def __init__(self, max_preserved_messages: int = 2) -> None:
        self.max_preserved_messages = max_preserved_messages

    def prepare(
        self, messages: list[dict], *, custom_instruction: str = ""
    ) -> tuple[dict | None, list[dict]]:
        """准备压缩。

        Returns:
            (compact_input, to_preserve)
            - compact_input: 喂给压缩 LLM 的单条大消息（role=user）
            - to_preserve: 需要保留的最近消息列表
        """
        if not messages or self.max_preserved_messages <= 0:
            return None, list(messages)

        history = list(messages)
        preserve_start_index = len(history)
        n_preserved = 0
        for index in range(len(history) - 1, -1, -1):
            role = history[index].get("role")
            if role in ("user", "assistant"):
                n_preserved += 1
                if n_preserved == self.max_preserved_messages:
                    preserve_start_index = index
                    break

        if n_preserved < self.max_preserved_messages:
            return None, list(messages)

        to_compact = history[:preserve_start_index]
        to_preserve = history[preserve_start_index:]

        if not to_compact:
            return None, to_preserve

        # 构建压缩 LLM 的输入消息
        parts: list[str] = []
        for i, msg in enumerate(to_compact):
            parts.append(f"## Message {i + 1}\nRole: {msg.get('role', 'unknown')}\nContent:\n")
            content = msg.get("content") or ""
            parts.append(content)
            # 如果有 tool_calls，也写入
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                parts.append(f"\n[tool_call: {func.get('name', '?')}({func.get('arguments', '')})]")

        compact_text = "\n".join(parts)

        # 附加 COMPACT 指令
        compact_md = Path(__file__).parent / "prompts" / "compact.md"
        prompt_text = "\n" + compact_md.read_text(encoding="utf-8")
        if custom_instruction:
            prompt_text += (
                "\n\n**User's Custom Compaction Instruction:**\n"
                "The user has specifically requested the following focus during compaction. "
                "You MUST prioritize this instruction above the default compression priorities:\n"
                f"{custom_instruction}"
            )
        compact_text += prompt_text

        compact_input = {"role": "user", "content": compact_text}
        return compact_input, to_preserve

    async def compact(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        *,
        custom_instruction: str = "",
    ) -> CompactionResult:
        """执行压缩。

        Args:
            messages: 完整对话历史
            client: OpenAI 兼容客户端
            model: 模型名称
            custom_instruction: 可选的自定义压缩指令

        Returns:
            CompactionResult: 压缩后的消息列表和 token 使用信息
        """
        compact_input, to_preserve = self.prepare(messages, custom_instruction=custom_instruction)
        if compact_input is None:
            return CompactionResult(messages=to_preserve, usage=None)

        # 调用压缩 LLM
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that compacts conversation context.",
                },
                compact_input,
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        compacted_content = resp.choices[0].message.content or ""

        usage = None
        if resp.usage:
            usage = {
                "input": resp.usage.prompt_tokens,
                "output": resp.usage.completion_tokens,
                "total": resp.usage.total_tokens,
            }

        # 包装成 user 消息 + 保留的消息
        compacted_messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    "Previous context has been compacted. "
                    "Here is the compaction output:\n\n" + compacted_content
                ),
            },
        ]
        compacted_messages.extend(to_preserve)
        return CompactionResult(messages=compacted_messages, usage=usage)
