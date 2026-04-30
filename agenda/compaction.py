from __future__ import annotations

"""Context Compaction — LLM 驱动的记忆压缩。

## 设计理念

学 Kimi Code CLI + Codex Memento 策略。

LLM 的上下文窗口有限（如 64K tokens）。当对话历史接近上限时，需要
压缩旧消息为结构化摘要，为新消息腾出空间。

压缩流程：
    1. prepare() — 将消息列表分为「待压缩」和「保留」两部分
    2. compact() — 用 LLM 将待压缩部分总结为结构化摘要
    3. validate_compacted() — 验证压缩产物有效（非空、未膨胀）
    4. truncate_messages() — LLM 压缩失败时的回退截断策略

## 双策略触发（should_auto_compact）

    条件 1：token_count >= max_context * trigger_ratio（如 75%）
    条件 2：token_count + reserved >= max_context
    满足任一即触发。参考 Kimi Code CLI 的设计。

## Tool Pair 完整性保证（_ensure_tool_pair_integrity）

压缩边界可能切在 assistant(tool_use) / tool(tool_result) 对中间。
如果 LLM 收到孤立的 tool_result 或孤立的 tool_use，会报 400 错误。
所以边界向前调整，确保所有 tool 对要么全在保留区，要么全在压缩区。

参考 Claw Code + Claude Code 的边界安全设计。

## 保留策略

只计数 user/assistant 消息。system 和 tool 消息不占保留配额。
默认保留 4 条 user/assistant 消息（含工具对完整展开）。
"""

from collections import namedtuple
from pathlib import Path
from typing import Any

CompactionResult = namedtuple("CompactionResult", ["messages", "usage"])


def estimate_text_tokens(messages: list[dict]) -> int:
    """估算消息列表的 token 数。

    改进版启发式（学 Claude Code）：
    - 英文/ASCII 字符：~4 chars/token
    - 中文/CJK 字符：~2 chars/token
    - tool_calls：name + arguments 单独计入
    - 最终 * 4/3 padding 保守上浮

    如果安装了 tiktoken，优先使用 tiktoken 做精确估算。
    """
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            total += len(enc.encode(content))
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                total += len(enc.encode(func.get("name", "")))
                total += len(enc.encode(func.get("arguments", "")))
        return int(total * 4 / 3)  # 保守 padding
    except Exception:
        pass

    total_chars_en = 0
    total_chars_cjk = 0
    for msg in messages:
        content = msg.get("content") or ""
        for ch in content:
            if ord(ch) > 0x4E00 and ord(ch) < 0x9FFF:
                total_chars_cjk += 1
            else:
                total_chars_en += 1

        # tool_calls 按英文估算
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            total_chars_en += len(func.get("name", ""))
            total_chars_en += len(func.get("arguments", ""))

    # 分别估算后合并，再保守上浮
    raw_estimate = total_chars_en // 4 + total_chars_cjk // 2
    return int(raw_estimate * 4 / 3)


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
    return token_count >= max_context_size * trigger_ratio or token_count + reserved_context_size >= max_context_size


class SimpleCompaction:
    """简单压缩策略：保留最近 N 条 user/assistant 消息，压缩前面的历史。

    关键安全保证：不拆散 assistant(tool_use) / tool(tool_result) 对。
    学 Claw Code + Claude Code 的边界安全设计。
    """

    def __init__(self, max_preserved_messages: int = 2) -> None:
        self.max_preserved_messages = max_preserved_messages

    @staticmethod
    def _ensure_tool_pair_integrity(history: list[dict], start_index: int) -> int:
        """调整保留边界，确保不拆分 tool_use/tool_result 对。

        规则：
        1. 保留区内若有 assistant 带 tool_calls，则所有对应 tool 结果必须在保留区。
        2. 保留区内若有 tool 结果，则其对应的 assistant 必须在保留区。

        若边界切在不完整的 pair 中间，将边界向前（索引减小）调整到 pair 起始处。
        """
        idx = start_index
        max_iterations = len(history)  # 防止极端情况的死循环

        for _ in range(max_iterations):
            if idx <= 0:
                break

            preserved = history[idx:]

            # 收集保留区内 assistant 发出的所有 tool_call ids
            assistant_tool_ids = set()
            for msg in preserved:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id", "")
                        if tc_id:
                            assistant_tool_ids.add(tc_id)

            # 收集保留区内所有 tool 结果对应的 ids
            result_tool_ids = set()
            for msg in preserved:
                if msg.get("role") == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    if tc_id:
                        result_tool_ids.add(tc_id)

            # 检查完整性
            missing_results = assistant_tool_ids - result_tool_ids
            missing_assistants = result_tool_ids - assistant_tool_ids

            if not missing_results and not missing_assistants:
                break

            # 向前移动 idx，把缺失部分纳入保留区。
            # 无论缺失的是结果还是 assistant，都需要找到对应 assistant 的位置
            # （因为 assistant 一定在 tool 结果之前）。
            new_idx = idx
            for i in range(idx - 1, -1, -1):
                msg = history[i]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id", "")
                        if tc_id in missing_results or tc_id in missing_assistants:
                            new_idx = min(new_idx, i)

            if new_idx >= idx:
                break
            idx = new_idx

        return idx

    def prepare(self, messages: list[dict], *, custom_instruction: str = "") -> tuple[dict | None, list[dict]]:
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

        # 边界安全：不拆散 tool_use/tool_result 对
        preserve_start_index = self._ensure_tool_pair_integrity(history, preserve_start_index)

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
                    "Previous context has been compacted. Here is the compaction output:\n\n" + compacted_content
                ),
            },
        ]
        compacted_messages.extend(to_preserve)
        return CompactionResult(messages=compacted_messages, usage=usage)

    @staticmethod
    def validate_compacted(
        compacted: CompactionResult,
        original_messages: list[dict],
        token_cap: int,
    ) -> bool:
        """验证压缩产物是否有效。

        Returns True when:
        - 压缩后内容非空
        - 压缩后的 token 数不超过原 token 数（压缩没有膨胀上下文）
        - 压缩后的 token 数不超过 token_cap
        """
        if not compacted.messages:
            return False
        compact_text = compacted.messages[0].get("content", "")
        if not compact_text or len(compact_text) < 50:
            return False
        post_tokens = estimate_text_tokens(compacted.messages)
        pre_tokens = estimate_text_tokens(original_messages)
        if post_tokens > pre_tokens:
            return False
        return not (post_tokens > token_cap)

    @staticmethod
    def truncate_messages(
        messages: list[dict],
        max_tokens: int,
        *,
        max_iterations: int = 100,
    ) -> list[dict]:
        """截断策略：从头部删除消息直到 token 数达标。

        保证不拆分 tool_use/tool_result 对，同时保留 system 消息。
        只在压缩 LLM 多次失败时作为最后手段。
        """
        if not messages:
            return list(messages)

        current = list(messages)
        for _ in range(max_iterations):
            tok = estimate_text_tokens(current)
            if tok <= max_tokens:
                return current
            if len(current) <= 1:
                return current
            # 跳过 system 消息
            drop_idx = 1 if current[0].get("role") == "system" and len(current) > 2 else 0
            if drop_idx >= len(current):
                return current
            # 检查 tool pair 完整性
            boundary = SimpleCompaction._ensure_tool_pair_integrity(current, drop_idx + 1)
            current = current[boundary:] if boundary > 0 else current[1:]
        return current
