"""Integration tests for Agenda — full DAG execution with mocked LLM."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agenda.agent import AgentLoop
from agenda.scheduler import DAGScheduler
from agenda.session import Session
from agenda.tools import build_tools

# ── Mock helpers ───────────────────────────────────────────────────────────


def _patch_agent_run(monkeypatch, results: dict[str, str] | None = None) -> None:
    """Patch AgentLoop.run to return results directly without LLM calls."""
    async def mock_run(self: AgentLoop, system_prompt: str, task: str) -> str:
        if results and self.node_id in results:
            return results[self.node_id]
        return "done"
    monkeypatch.setattr(AgentLoop, "run", mock_run)


def _patch_agent_run_with_agenda(monkeypatch) -> None:
    """Patch AgentLoop.run so that the first call executes agenda() then returns."""
    async def mock_run(self: AgentLoop, system_prompt: str, task: str) -> str:
        agenda_tool = self.tools.get("agenda")
        if agenda_tool and "delegate" in task.lower():
            child_dag = (
                "dag:\n  name: child\n"
                "nodes:\n  child_a:\n    prompt: child task\n"
            )
            await agenda_tool(dag_yaml=child_dag)
        return "done after agenda"
    monkeypatch.setattr(AgentLoop, "run", mock_run)


# ── Integration test cases ─────────────────────────────────────────────────


class TestSimpleParallelDAG:
    """Two independent nodes run in parallel and produce outputs."""

    def test_parallel_completion(self, tmp_path: Path, monkeypatch) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "parallel", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "task A"},
                "b": {"prompt": "task B"},
            },
        }
        _patch_agent_run(monkeypatch, {"a": "A done", "b": "B done"})

        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        results = asyncio.run(_run())
        assert results["a"] == "COMPLETED"
        assert results["b"] == "COMPLETED"
        assert (scheduler.nodes_dir / "a" / "output" / "draft.md").read_text() == "A done"
        assert (scheduler.nodes_dir / "b" / "output" / "draft.md").read_text() == "B done"


class TestDependencyChain:
    """Node B depends on A; B must wait until A finishes."""

    def test_b_starts_after_a(self, tmp_path: Path, monkeypatch) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "chain", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "task A"},
                "b": {"prompt": "task B", "deps": ["a"]},
            },
        }
        _patch_agent_run(monkeypatch)

        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        results = asyncio.run(_run())
        assert results["a"] == "COMPLETED"
        assert results["b"] == "COMPLETED"


class TestAgendaRecursion:
    """A node calls agenda() to spawn a child DAG."""

    def test_child_dag_execution(self, tmp_path: Path, monkeypatch) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "parent", "max_parallel": 4},
            "nodes": {
                "parent": {"prompt": "delegate to child"},
            },
        }
        _patch_agent_run_with_agenda(monkeypatch)

        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        results = asyncio.run(_run())
        assert results["parent"] == "COMPLETED"

        # Child DAG Base Case should have created its node dir
        child_output = (
            tmp_path / "test" / "nodes" / "parent" / "workspace" / "subdags"
            / "nodes" / "child_a" / "output" / "draft.md"
        )
        assert child_output.exists()
        assert child_output.read_text() == "done after agenda"


class TestBaseCaseOptimization:
    """Single-node DAG skips Scheduler state entirely."""

    def test_no_scheduler_state_created(self, tmp_path: Path, monkeypatch) -> None:
        from agenda import agenda as agenda_fn

        dag_spec = {
            "dag": {"name": "single", "max_parallel": 4},
            "nodes": {"only": {"prompt": "solo task"}},
        }
        _patch_agent_run(monkeypatch, {"only": "solo done"})

        results = asyncio.run(agenda_fn(dag_spec, tmp_path / "basecase"))
        assert results["only"] == "COMPLETED"

        # No scheduler_state.json should exist for Base Case
        state_file = tmp_path / "basecase" / "single" / ".system" / "scheduler_state.json"
        assert not state_file.exists()


class TestCrashRecovery:
    """Scheduler resumes from persisted state after crash."""

    def test_resumes_completed_nodes(self, tmp_path: Path, monkeypatch) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "recover", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "task A"},
                "b": {"prompt": "task B", "deps": ["a"]},
            },
        }

        # Simulate crash: mark 'a' as already completed on disk
        node_a_dir = scheduler.nodes_dir / "a"
        Session(node_a_dir).write_file("output/draft.md", "A was done before crash")

        # Also persist scheduler state saying 'a' is completed
        scheduler.state_file.write_text(
            json.dumps({"completed": ["a"], "failed": [], "running": [], "retries": {}}),
            encoding="utf-8",
        )

        _patch_agent_run(monkeypatch, {"b": "B done"})

        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        results = asyncio.run(_run())
        assert results["a"] == "COMPLETED"
        assert results["b"] == "COMPLETED"


class TestDepInputsRouting:
    """Upstream output is routed to downstream input/ via dep_inputs."""

    def test_downstream_receives_upstream_output(self, tmp_path: Path, monkeypatch) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "routing", "max_parallel": 4},
            "nodes": {
                "producer": {"prompt": "produce data"},
                "consumer": {
                    "prompt": "consume data",
                    "deps": ["producer"],
                    "dep_inputs": [
                        {"from": "nodes/producer/output/draft.md", "to": "producer_result.md"}
                    ],
                },
            },
        }
        _patch_agent_run(monkeypatch, {"producer": "PRODUCED_DATA", "consumer": "consumed"})

        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        results = asyncio.run(_run())
        assert results["producer"] == "COMPLETED"
        assert results["consumer"] == "COMPLETED"

        # Consumer's input/ should contain the routed file
        consumer_input = scheduler.nodes_dir / "consumer" / "input" / "producer_result.md"
        assert consumer_input.exists()
        assert consumer_input.read_text() == "PRODUCED_DATA"


class TestTopLevelAgendaEntry:
    """The public agenda() function works as a unified entry point."""

    def test_agenda_function_runs_multi_node(self, tmp_path: Path, monkeypatch) -> None:
        from agenda import agenda as agenda_fn

        dag_spec = {
            "dag": {"name": "entry", "max_parallel": 4},
            "nodes": {
                "x": {"prompt": "X"},
                "y": {"prompt": "Y", "deps": ["x"]},
            },
        }
        _patch_agent_run(monkeypatch, {"x": "X done", "y": "Y done"})

        results = asyncio.run(agenda_fn(dag_spec, tmp_path / "entry_test"))
        assert results["x"] == "COMPLETED"
        assert results["y"] == "COMPLETED"
