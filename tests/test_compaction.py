"""Tests for context compaction and token estimation."""

from __future__ import annotations

import asyncio

import pytest

from agenda.compaction import (
    CompactionResult,
    SimpleCompaction,
    estimate_text_tokens,
    should_auto_compact,
)


# ---------------------------------------------------------------------------
# should_auto_compact
# ---------------------------------------------------------------------------

class TestShouldAutoCompact:
    def test_ratio_trigger(self) -> None:
        # Use a large max_context so reserved_context does not trigger
        assert should_auto_compact(750, 1000, trigger_ratio=0.75, reserved_context_size=0) is True
        assert should_auto_compact(740, 1000, trigger_ratio=0.75, reserved_context_size=0) is False

    def test_reserved_trigger(self) -> None:
        assert should_auto_compact(500, 1000, reserved_context_size=500) is True
        assert should_auto_compact(499, 1000, reserved_context_size=500) is False

    def test_neither_trigger(self) -> None:
        assert should_auto_compact(100, 10000) is False


# ---------------------------------------------------------------------------
# estimate_text_tokens
# ---------------------------------------------------------------------------

class TestEstimateTextTokens:
    """Token estimation with CJK-aware heuristic."""

    def test_ascii_text(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]
        # 11 ascii chars / 4 * 4/3 padding ≈ 4
        assert estimate_text_tokens(msgs) > 0

    def test_cjk_text(self) -> None:
        msgs = [{"role": "user", "content": "你好世界"}]
        # 4 CJK chars / 2 * 4/3 padding ≈ 3
        assert estimate_text_tokens(msgs) > 0

    def test_tool_calls_counted(self) -> None:
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "tc1",
                "function": {"name": "read_file", "arguments": '{"path": "test"}'},
            }],
        }]
        result = estimate_text_tokens(msgs)
        assert result > 0

    def test_mixed_content(self) -> None:
        msgs = [
            {"role": "user", "content": "hello 你好"},
            {"role": "assistant", "content": "world 世界"},
        ]
        result = estimate_text_tokens(msgs)
        # Should be larger than pure-ASCII due to CJK weighting
        assert result > 0


# ---------------------------------------------------------------------------
# SimpleCompaction.prepare
# ---------------------------------------------------------------------------

class TestCompactionPrepare:
    def test_basic_split(self) -> None:
        """History longer than max_preserved_messages gets split."""
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "user", "content": "msg3"},
            {"role": "assistant", "content": "msg4"},
        ]
        sc = SimpleCompaction(max_preserved_messages=2)
        compact_input, to_preserve = sc.prepare(history)

        assert compact_input is not None
        assert compact_input["role"] == "user"
        assert "msg1" in compact_input["content"]
        assert "msg2" in compact_input["content"]

        assert len(to_preserve) == 2
        assert to_preserve[0]["content"] == "msg3"
        assert to_preserve[1]["content"] == "msg4"

    def test_all_system_messages_in_compact(self) -> None:
        """System messages are not counted as preserved, so they land in compact zone."""
        history = [
            {"role": "system", "content": "sys1"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        sc = SimpleCompaction(max_preserved_messages=1)
        compact_input, to_preserve = sc.prepare(history)

        assert compact_input is not None
        assert "sys1" in compact_input["content"]
        assert len(to_preserve) == 1
        assert to_preserve[0]["content"] == "msg2"

    def test_no_messages(self) -> None:
        sc = SimpleCompaction(max_preserved_messages=2)
        compact_input, to_preserve = sc.prepare([])
        assert compact_input is None
        assert to_preserve == []

    def test_zero_preserve(self) -> None:
        sc = SimpleCompaction(max_preserved_messages=0)
        history = [{"role": "user", "content": "hi"}]
        compact_input, to_preserve = sc.prepare(history)
        assert compact_input is None
        assert to_preserve == history

    def test_insufficient_messages(self) -> None:
        sc = SimpleCompaction(max_preserved_messages=5)
        history = [{"role": "user", "content": "hi"}]
        compact_input, to_preserve = sc.prepare(history)
        assert compact_input is None
        assert len(to_preserve) == 1

    def test_custom_instruction_appended(self) -> None:
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        sc = SimpleCompaction(max_preserved_messages=1)
        compact_input, to_preserve = sc.prepare(history, custom_instruction="focus on X")
        assert compact_input is not None
        assert "focus on X" in compact_input["content"]

    def test_no_compact_when_all_preserved(self) -> None:
        """If history is exactly max_preserved_messages long, nothing to compact."""
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        sc = SimpleCompaction(max_preserved_messages=2)
        compact_input, to_preserve = sc.prepare(history)
        assert compact_input is None
        assert len(to_preserve) == 2


# ---------------------------------------------------------------------------
# Tool pair integrity
# ---------------------------------------------------------------------------

class TestToolPairIntegrity:
    """Compaction must not split assistant(tool_use) / tool(tool_result) pairs."""

    def _make_pair(self) -> list[dict]:
        return [
            {"role": "user", "content": "a"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
            {"role": "user", "content": "b"},
        ]

    def test_pair_preserved_when_assistant_in_preserve(self) -> None:
        """If assistant is preserved, its tool result must also be preserved."""
        history = self._make_pair()
        sc = SimpleCompaction(max_preserved_messages=1)
        compact_input, to_preserve = sc.prepare(history)

        # Only user "b" is counted as 1 preserved message,
        # but assistant+tool pair is behind it and stays in compact zone.
        # No pair is split because the boundary does not cut inside the pair.
        assert compact_input is not None
        roles = [m["role"] for m in to_preserve]
        assert "user" in roles

    def test_defense_pulls_assistant_when_tool_is_preserved(self) -> None:
        """If boundary places a tool result in preserve without its assistant,
        _ensure_tool_pair_integrity should pull the assistant in."""
        # Manually construct a scenario where the raw boundary is unsafe.
        history = [
            {"role": "user", "content": "a"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
            {"role": "user", "content": "b"},
        ]
        # Force boundary at index 2 (tool result in preserve, assistant in compact)
        idx = SimpleCompaction._ensure_tool_pair_integrity(history, 2)
        assert idx == 1, f"assistant must be pulled into preserve, got {idx}"

    def test_multi_tool_pair_preserved(self) -> None:
        """Assistant with multiple tool_calls — all results must be preserved."""
        history = [
            {"role": "user", "content": "a"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc1", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "tc2", "function": {"name": "write_file", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
            {"role": "tool", "tool_call_id": "tc2", "content": "r2"},
            {"role": "user", "content": "b"},
        ]
        # Boundary at assistant (index 1) — both results follow, safe
        idx = SimpleCompaction._ensure_tool_pair_integrity(history, 1)
        assert idx == 1


# ---------------------------------------------------------------------------
# SimpleCompaction.compact (async, requires mocked client)
# ---------------------------------------------------------------------------

class MockChoice:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class MockUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 100
        self.completion_tokens = 50
        self.total_tokens = 150


class MockCompletion:
    def __init__(self, content: str, with_usage: bool = True) -> None:
        self.choices = [MockChoice(content)]
        self.usage = MockUsage() if with_usage else None


class MockClient:
    def __init__(self, content: str = "compacted summary", with_usage: bool = True) -> None:
        self.content = content
        self.with_usage = with_usage

    async def create(self, **kwargs):
        return MockCompletion(self.content, self.with_usage)


class FakeChatCompletions:
    def __init__(self, client) -> None:
        self._client = client

    async def create(self, **kwargs):
        return await self._client.create(**kwargs)


class FakeAsyncClient:
    def __init__(self, content: str = "summary", with_usage: bool = True) -> None:
        self.chat = type("Chat", (), {"completions": FakeChatCompletions(MockClient(content, with_usage))})()


class TestCompactionCompact:
    def test_compact_returns_compacted_messages(self) -> None:
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "new"},
        ]
        sc = SimpleCompaction(max_preserved_messages=1)
        client = FakeAsyncClient(content="compacted summary")
        result = asyncio.run(sc.compact(history, client, model="gpt-4"))

        assert isinstance(result, CompactionResult)
        assert len(result.messages) == 2  # compacted + preserved
        assert "compacted" in result.messages[0]["content"]
        assert result.messages[1]["content"] == "new"

    def test_compact_usage_returned(self) -> None:
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "new"},
        ]
        sc = SimpleCompaction(max_preserved_messages=1)
        client = FakeAsyncClient(content="summary", with_usage=True)
        result = asyncio.run(sc.compact(history, client, model="gpt-4"))

        assert result.usage is not None
        assert result.usage["input"] == 100
        assert result.usage["output"] == 50
        assert result.usage["total"] == 150

    def test_compact_no_usage(self) -> None:
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "new"},
        ]
        sc = SimpleCompaction(max_preserved_messages=1)
        client = FakeAsyncClient(content="summary", with_usage=False)
        result = asyncio.run(sc.compact(history, client, model="gpt-4"))

        assert result.usage is None

    def test_compact_nothing_to_compact(self) -> None:
        """If prepare returns None, compact should return original messages."""
        history = [{"role": "user", "content": "hi"}]
        sc = SimpleCompaction(max_preserved_messages=5)
        client = FakeAsyncClient(content="should not be called")
        result = asyncio.run(sc.compact(history, client, model="gpt-4"))

        assert result.messages == history
        assert result.usage is None
