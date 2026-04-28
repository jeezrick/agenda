"""Tests for agenda.agent — AgentLoop core logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agenda.agent import AgentLoop
from agenda.models import ModelConfig, ModelRegistry
from agenda.session import Session
from agenda.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_registry() -> ModelRegistry:
    reg = ModelRegistry()
    reg._models["default"] = ModelConfig(
        name="default",
        base_url="http://localhost:9999/v1",
        api_key="test",
        model="test-model",
        token_cap=32000,
    )
    return reg


@pytest.fixture
def simple_tools() -> ToolRegistry:
    tools = ToolRegistry()

    @tools.register("echo")
    def echo(msg: str) -> str:
        return f"echo:{msg}"

    @tools.register("async_echo")
    async def async_echo(msg: str) -> str:
        return f"async:{msg}"

    @tools.register("fail")
    def fail_tool() -> str:
        raise ValueError("tool error")

    return tools


# ---------------------------------------------------------------------------
# AgentLoop.run — happy path
# ---------------------------------------------------------------------------

class TestAgentLoopRun:
    def test_run_from_scratch_no_tools(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """LLM returns completion directly, no tool_calls."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        mock_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "final answer",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(return_value=mock_resp)):
            result = asyncio.run(agent.run("system prompt", "task"))

        assert result == "final answer"
        # Should have saved a turn
        turns = session.load_turns()
        assert len(turns) == 1
        assert turns[0]["iteration"] == 1

    def test_run_with_tool_call_then_completion(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """LLM returns tool_call, then completion."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {"name": "echo", "arguments": '{"msg": "hello"}'},
                    }],
                }
            }]
        }
        final_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "done",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(side_effect=[tool_resp, final_resp])):
            result = asyncio.run(agent.run("system prompt", "task"))

        assert result == "done"
        # messages should contain system, user, assistant(tool), tool(result), assistant(done)
        assert len(agent.messages) == 5
        # tool result should be in messages
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "echo:hello" in tool_msgs[0]["content"]

    def test_run_with_async_tool(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Async tool should be awaited correctly."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {"name": "async_echo", "arguments": '{"msg": "world"}'},
                    }],
                }
            }]
        }
        final_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "ok",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(side_effect=[tool_resp, final_resp])):
            result = asyncio.run(agent.run("system prompt", "task"))

        assert result == "ok"
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        assert "async:world" in tool_msgs[0]["content"]

    def test_run_restores_history(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """If turns.jsonl exists, run should restore history."""
        session = Session(tmp_path / "node")
        session.save_turn({
            "type": "turn",
            "messages": [
                {"role": "user", "content": "previous task"},
                {"role": "assistant", "content": "previous result"},
            ],
        })

        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)
        mock_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "continued",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(return_value=mock_resp)):
            result = asyncio.run(agent.run("new system", "new task"))

        assert result == "continued"
        # system should be new, user should have previous + new task
        assert agent.messages[0]["role"] == "system"
        assert agent.messages[0]["content"] == "new system"
        # previous messages restored (except system)
        assert any(m.get("content") == "previous result" for m in agent.messages)

    def test_run_max_iterations_exceeded(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """If LLM keeps returning tool_calls, should hit max_iterations."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=2, timeout=60)

        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {"name": "echo", "arguments": '{"msg": "x"}'},
                    }],
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(return_value=tool_resp)), pytest.raises(RuntimeError, match="迭代次数达到上限"):
            asyncio.run(agent.run("system", "task"))

        # error.log should be written
        assert session.is_failed()

    def test_run_timeout(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """If _call_llm takes longer than timeout, next loop check should raise."""
        import time as _time
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=10, timeout=0.01)

        async def slow_llm_with_tools():
            # Busy-wait to exceed timeout, then return tool_calls so loop continues
            deadline = _time.monotonic() + 0.05
            while _time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "tc1",
                            "function": {"name": "echo", "arguments": '{"msg": "x"}'},
                        }],
                    }
                }]
            }

        with patch.object(agent, "_call_llm", new=slow_llm_with_tools), pytest.raises(asyncio.TimeoutError):
            asyncio.run(agent.run("system", "task"))

    def test_run_cancelled_saves_partial_turn(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Cancel during loop should save partial turn."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        async def cancel_after_tool_resp():
            agent.cancel()
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "tc1",
                            "function": {"name": "echo", "arguments": '{"msg": "x"}'},
                        }],
                    }
                }]
            }

        with patch.object(agent, "_call_llm", new=cancel_after_tool_resp), pytest.raises(asyncio.CancelledError):
            asyncio.run(agent.run("system", "task"))

        # partial turn should be saved
        turns = session.load_turns()
        assert len(turns) >= 1
        assert turns[0].get("interrupted") is True

    def test_run_unknown_tool(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Unknown tool should return error but not crash."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {"name": "nonexistent", "arguments": "{}"},
                    }],
                }
            }]
        }
        final_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "handled",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(side_effect=[tool_resp, final_resp])):
            result = asyncio.run(agent.run("system", "task"))

        assert result == "handled"
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        assert "未知工具" in tool_msgs[0]["content"]

    def test_run_tool_exception(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Tool that raises should return error message."""
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)

        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {"name": "fail", "arguments": "{}"},
                    }],
                }
            }]
        }
        final_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "recovered",
                }
            }]
        }

        with patch.object(agent, "_call_llm", new=AsyncMock(side_effect=[tool_resp, final_resp])):
            result = asyncio.run(agent.run("system", "task"))

        assert result == "recovered"
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        assert "执行错误" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# IPC events
# ---------------------------------------------------------------------------

class TestIPCEvents:
    def test_poll_interrupt(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)
        session.append_event({"type": "interrupt", "from": "scheduler"})
        agent._events_offset = 0

        asyncio.run(agent._poll_events())
        assert agent._cancelled is True

    def test_poll_message(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        session = Session(tmp_path / "node")
        agent = AgentLoop(session, mock_registry, simple_tools, max_iterations=5, timeout=60)
        session.append_event({"type": "message", "from": "parent", "content": "help"})
        agent._events_offset = 0

        asyncio.run(agent._poll_events())
        user_msgs = [m for m in agent.messages if m.get("role") == "user"]
        assert any("help" in m.get("content", "") for m in user_msgs)


# ---------------------------------------------------------------------------
# _seal_orphan_tool_calls
# ---------------------------------------------------------------------------

class TestSealOrphan:
    def test_seals_when_last_is_assistant_with_tool_calls(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)
        agent.messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        ]
        agent._seal_orphan_tool_calls()
        assert len(agent.messages) == 2
        assert agent.messages[1]["role"] == "tool"
        assert agent.messages[1]["tool_call_id"] == "tc1"

    def test_no_seal_when_already_has_result(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)
        agent.messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        ]
        agent._seal_orphan_tool_calls()
        assert len(agent.messages) == 2

    def test_no_seal_when_last_is_not_assistant(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)
        agent.messages = [
            {"role": "user", "content": "hi"},
        ]
        agent._seal_orphan_tool_calls()
        assert len(agent.messages) == 1

    def test_no_seal_when_empty(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)
        agent._seal_orphan_tool_calls()
        assert len(agent.messages) == 0


# ---------------------------------------------------------------------------
# _msg_to_dict
# ---------------------------------------------------------------------------

class TestMsgToDict:
    def test_dict_passthrough(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)
        d = {"role": "assistant", "content": "hello"}
        assert agent._msg_to_dict(d) is d

    def test_object_conversion(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)

        class FakeMsg:
            role = "assistant"
            content = "hello"
            tool_calls = None

        result = agent._msg_to_dict(FakeMsg())
        assert result == {"role": "assistant", "content": "hello"}


# ---------------------------------------------------------------------------
# _call_llm fallback
# ---------------------------------------------------------------------------

class TestCallLLMFallback:
    def test_fallback_on_fallbackable_error(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Primary model fails with connection error, fallback succeeds."""
        reg = ModelRegistry()
        reg._models["default"] = ModelConfig(
            name="default",
            base_url="http://localhost:9999/v1",
            api_key="test",
            model="primary",
            token_cap=32000,
            fallback_model="backup",
        )
        reg._models["backup"] = ModelConfig(
            name="backup",
            base_url="http://localhost:9998/v1",
            api_key="test",
            model="backup-model",
            token_cap=32000,
        )

        agent = AgentLoop(Session(tmp_path / "node"), reg, simple_tools)

        mock = AsyncMock(side_effect=[
            OSError("connection refused"),
            {"choices": [{"message": {"role": "assistant", "content": "fallback ok"}}]},
        ])
        with patch.object(agent, "_call_llm_with_cfg", new=mock):
            result = asyncio.run(agent._call_llm())
            assert result["choices"][0]["message"]["content"] == "fallback ok"

    def test_no_fallback_on_code_error(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """Code errors (e.g. ValueError) should not trigger fallback."""
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)

        mock = AsyncMock(side_effect=ValueError("bug"))
        with patch.object(agent, "_call_llm_with_cfg", new=mock), pytest.raises(ValueError, match="bug"):
            asyncio.run(agent._call_llm())

    def test_no_fallback_when_no_fallback_model(self, tmp_path: Path, mock_registry: ModelRegistry, simple_tools: ToolRegistry) -> None:
        """No fallback_model configured → error propagates."""
        agent = AgentLoop(Session(tmp_path / "node"), mock_registry, simple_tools)

        mock = AsyncMock(side_effect=OSError("connection refused"))
        with patch.object(agent, "_call_llm_with_cfg", new=mock), pytest.raises(OSError, match="connection refused"):
            asyncio.run(agent._call_llm())
