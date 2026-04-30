"""Tests for agenda_api.py — run_agent_node, run_sub_dag, output validation."""

import asyncio
import json
from pathlib import Path

from agenda.agent import AgentLoop
from agenda.models import ModelRegistry
from agenda.session import Session
from agenda.tools import build_tools


def _registry() -> ModelRegistry:
    return ModelRegistry()


class TestRunSubDag:
    """Tests for run_sub_dag — Base Case and Recursive Step dispatch."""

    def _make_dag(self, nodes: dict) -> dict:
        return {"dag": {"name": "test", "max_parallel": 2}, "nodes": nodes}

    def _patch_agent_run(self, monkeypatch, results: dict[str, str] | None = None) -> None:
        async def mock_run(self: AgentLoop, system_prompt: str, task: str) -> str:
            if results and self.node_id in results:
                return results[self.node_id]
            return "done"

        monkeypatch.setattr(AgentLoop, "run", mock_run)

    def test_empty_nodes_returns_empty_dict(self, tmp_path: Path) -> None:
        from agenda.agenda_api import run_sub_dag

        result = asyncio.run(run_sub_dag(self._make_dag({}), tmp_path, _registry(), lambda s: build_tools(s)))
        assert result == {}

    def test_single_node_base_case(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_sub_dag

        self._patch_agent_run(monkeypatch, {"only": "base case done"})

        dag = self._make_dag({"only": {"prompt": "solo task"}})
        result = asyncio.run(run_sub_dag(dag, tmp_path, _registry(), lambda s: build_tools(s)))
        assert result == {"only": "COMPLETED"}
        assert (tmp_path / "nodes" / "only" / "output" / "draft.md").read_text() == "base case done"

    def test_single_node_base_case_failure(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_sub_dag

        async def mock_run_fail(self: AgentLoop, system_prompt: str, task: str) -> str:
            raise RuntimeError("agent failed")

        monkeypatch.setattr(AgentLoop, "run", mock_run_fail)

        dag = self._make_dag({"crash": {"prompt": "boom"}})
        result = asyncio.run(run_sub_dag(dag, tmp_path, _registry(), lambda s: build_tools(s)))
        assert result == {"crash": "FAILED"}

    def test_multi_node_recursive_step(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_sub_dag

        self._patch_agent_run(monkeypatch, {"a": "A done", "b": "B done"})

        dag = self._make_dag(
            {
                "a": {"prompt": "task A"},
                "b": {"prompt": "task B"},
            }
        )
        result = asyncio.run(run_sub_dag(dag, tmp_path, _registry(), lambda s: build_tools(s)))
        assert result["a"] == "COMPLETED"
        assert result["b"] == "COMPLETED"

    def test_hooks_passed_through(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_sub_dag
        from agenda.hook import HookRegistry

        events: list[str] = []
        hooks = HookRegistry()
        hooks.register("on_node_start", lambda **kw: events.append("start"))
        hooks.register("on_node_complete", lambda **kw: events.append("complete"))

        self._patch_agent_run(monkeypatch)

        dag = self._make_dag({"a": {"prompt": "task A"}, "b": {"prompt": "task B"}})
        asyncio.run(run_sub_dag(dag, tmp_path, _registry(), lambda s: build_tools(s), hooks=hooks))
        assert "start" in events
        assert "complete" in events


class TestValidateAndCorrectOutput:
    """Tests for _validate_and_correct_output — structured output contract."""

    def test_valid_json_output_passes(self, tmp_path: Path) -> None:
        from agenda.agenda_api import _validate_and_correct_output

        session = Session(tmp_path / "nodes" / "test")
        session.write_file("output/draft.md", '{"summary": "hello", "key_points": ["a", "b"]}')

        node_config = {
            "output_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
            }
        }

        result = asyncio.run(
            _validate_and_correct_output(
                session=session,
                node_config=node_config,
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
                agent=None,
                system_prompt="",
            )
        )
        parsed = json.loads(result)
        assert parsed["summary"] == "hello"

    def test_invalid_json_triggers_correction(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import _validate_and_correct_output

        session = Session(tmp_path / "nodes" / "test")
        session.write_file("output/draft.md", "not valid json {{{")

        # Mock AgentLoop to write valid JSON on second attempt
        fix_count = [0]

        async def mock_run(self: AgentLoop, system_prompt: str, task: str) -> str:
            fix_count[0] += 1
            return '{"fixed": true}'

        monkeypatch.setattr(AgentLoop, "run", mock_run)

        # Need a real AgentLoop instance for the correction path
        from agenda.models import ModelRegistry

        agent = AgentLoop(
            session=session,
            model_registry=ModelRegistry(),
            tools=build_tools(session),
            node_id="test",
        )
        agent.messages = [{"role": "system", "content": ""}]

        node_config = {"output_schema": {"type": "object", "properties": {"fixed": {"type": "boolean"}}}}

        result = asyncio.run(
            _validate_and_correct_output(
                session=session,
                node_config=node_config,
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
                agent=agent,
                system_prompt="",
            )
        )
        assert json.loads(result) == {"fixed": True}
        assert fix_count[0] >= 1

    def test_no_output_schema_skips_validation(self, tmp_path: Path) -> None:
        from agenda.agenda_api import _validate_and_correct_output

        session = Session(tmp_path / "nodes" / "test")
        session.write_file("output/draft.md", "anything goes")

        node_config: dict = {}  # No output_schema

        result = asyncio.run(
            _validate_and_correct_output(
                session=session,
                node_config=node_config,
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
                agent=None,
                system_prompt="",
            )
        )
        assert result == "anything goes"

    def test_no_draft_file_returns_empty(self, tmp_path: Path) -> None:
        from agenda.agenda_api import _validate_and_correct_output

        session = Session(tmp_path / "nodes" / "test")
        # No output/draft.md written

        node_config = {"output_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}

        result = asyncio.run(
            _validate_and_correct_output(
                session=session,
                node_config=node_config,
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
                agent=None,
                system_prompt="",
            )
        )
        assert result == ""


class TestRunAgentNode:
    """Tests for run_agent_node — core agent execution."""

    def test_agent_writes_output_when_missing(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_agent_node

        async def mock_run(self: AgentLoop, sp: str, t: str) -> str:
            return "agent output"

        monkeypatch.setattr(AgentLoop, "run", mock_run)

        session = Session(tmp_path / "nodes" / "test")
        session.write_system("hints.md", "# Test Hints")

        result = asyncio.run(
            run_agent_node(
                session=session,
                node_config={"prompt": "do it"},
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
            )
        )
        assert result == "agent output"
        assert session.output_exists
        assert session.read_file("output/draft.md") == "agent output"

    def test_hooks_passed_to_agent(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_agent_node
        from agenda.hook import HookRegistry

        events: list[str] = []
        hooks = HookRegistry()
        hooks.register("on_turn_start", lambda **kw: events.append("turn"))

        # Don't patch — let the real AgentLoop.__init__ receive hooks
        # but patch run to avoid actual LLM call
        async def mock_run(self: AgentLoop, sp: str, task: str) -> str:
            assert self.hooks is not None
            assert self.hooks.has("on_turn_start")
            return "done"

        monkeypatch.setattr(AgentLoop, "run", mock_run)

        session = Session(tmp_path / "nodes" / "test")
        session.write_system("hints.md", "# Test")

        asyncio.run(
            run_agent_node(
                session=session,
                node_config={"prompt": "task"},
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
                hooks=hooks,
            )
        )
        # No exception = hooks were passed correctly

    def test_approval_config_passed_to_agent(self, tmp_path: Path, monkeypatch) -> None:
        from agenda.agenda_api import run_agent_node

        captured_agent: list[AgentLoop] = []

        async def mock_run(self: AgentLoop, sp: str, task: str) -> str:
            captured_agent.append(self)
            return "done"

        monkeypatch.setattr(AgentLoop, "run", mock_run)

        session = Session(tmp_path / "nodes" / "test")
        session.write_system("hints.md", "# Test")

        asyncio.run(
            run_agent_node(
                session=session,
                node_config={
                    "prompt": "task",
                    "approval_required": True,
                    "approval_tools": ["run_shell"],
                    "approval_timeout": 120,
                },
                model_registry=_registry(),
                tools_factory=lambda s: build_tools(s),
            )
        )
        agent = captured_agent[0]
        assert agent.approval_required is True
        assert agent.approval_tools == ["run_shell"]
        assert agent.approval_timeout == 120.0
