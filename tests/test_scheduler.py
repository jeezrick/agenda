"""Tests for DAG scheduler: topology, cycle detection, Base Case."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import patch

from agenda.scheduler import DAGScheduler
from agenda.session import Session
from agenda.tools import build_tools


class TestTopologicalSort:
    def test_linear_deps(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "linear", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A", "deps": []},
                "b": {"prompt": "B", "deps": ["a"]},
                "c": {"prompt": "C", "deps": ["b"]},
            },
        }
        topo = scheduler.topological_sort()
        assert topo.index("a") < topo.index("b") < topo.index("c")

    def test_diamond_deps(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "diamond", "max_parallel": 4},
            "nodes": {
                "start": {"prompt": "S", "deps": []},
                "left": {"prompt": "L", "deps": ["start"]},
                "right": {"prompt": "R", "deps": ["start"]},
                "end": {"prompt": "E", "deps": ["left", "right"]},
            },
        }
        topo = scheduler.topological_sort()
        assert topo.index("start") < topo.index("left")
        assert topo.index("start") < topo.index("right")
        assert topo.index("left") < topo.index("end")
        assert topo.index("right") < topo.index("end")

    def test_empty_dag(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {"dag": {"name": "empty"}, "nodes": {}}
        assert scheduler.topological_sort() == []


class TestCycleDetection:
    def test_no_cycle(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "acyclic"},
            "nodes": {
                "a": {"deps": []},
                "b": {"deps": ["a"]},
            },
        }
        assert scheduler._detect_cycle() is None

    def test_simple_cycle(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "cyclic"},
            "nodes": {
                "a": {"deps": ["b"]},
                "b": {"deps": ["a"]},
            },
        }
        cycle = scheduler._detect_cycle()
        assert cycle is not None
        assert "a" in cycle and "b" in cycle

    def test_self_loop(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "self"},
            "nodes": {"a": {"deps": ["a"]}},
        }
        cycle = scheduler._detect_cycle()
        assert cycle is not None
        assert "a" in cycle


class TestReadyNodes:
    def test_ready_after_deps_complete(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "ready"},
            "nodes": {
                "a": {"deps": []},
                "b": {"deps": ["a"]},
            },
        }
        # Initially only 'a' is ready
        assert scheduler.ready_nodes() == ["a"]

        # Mark 'a' completed
        scheduler.completed.add("a")
        assert scheduler.ready_nodes() == ["b"]


class TestBaseCaseOptimization:
    def test_single_node_skips_scheduler(self, tmp_path: Path) -> None:
        """Single-node DAG should not create scheduler state or run scheduling loop."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "single", "max_parallel": 4},
            "nodes": {
                "only": {"prompt": "test prompt"},
            },
        }

        # run() should hit Base Case and skip scheduler state creation
        async def _run() -> dict[str, str]:
            return await scheduler.run(tools_factory=lambda s: build_tools(s))

        # This will fail because there is no LLM configured, but we can
        # verify the Base Case path by checking that scheduler_state.json
        # is NOT created before the node run attempt.
        state_file = scheduler.state_file
        assert not state_file.exists()

        # Run will fail (no LLM), but Base Case path should be taken
        with contextlib.suppress(Exception):
            asyncio.run(_run())

        # State file should still not exist because Base Case skips scheduler
        assert not state_file.exists()


class TestInferDepth:
    def test_depth_from_session_state(self, tmp_path: Path) -> None:
        """_infer_depth should read from session state, not path."""
        scheduler = DAGScheduler(tmp_path, "test")
        session = Session(tmp_path / "node")
        session.set_state("agenda_depth", 3)
        assert scheduler._infer_depth(session) == 3

    def test_default_depth_zero(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        session = Session(tmp_path / "node")
        # No depth state set
        assert scheduler._infer_depth(session) == 0

    def test_no_false_positive_from_path(self, tmp_path: Path) -> None:
        """Project path containing 'subdags' should not affect depth."""
        scheduler = DAGScheduler(tmp_path, "test")
        # Simulate project in a directory named 'subdags'
        node_dir = tmp_path / "subdags" / "project" / "nodes" / "a"
        session = Session(node_dir)
        # Without explicit depth state, default is 0
        assert scheduler._infer_depth(session) == 0

        # With explicit depth state, return exact value
        session.set_state("agenda_depth", 2)
        assert scheduler._infer_depth(session) == 2


# ---------------------------------------------------------------------------
# Scheduler run loop
# ---------------------------------------------------------------------------

class TestSchedulerRunLoop:
    def test_empty_dag_returns_empty(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {"dag": {"name": "empty"}, "nodes": {}}
        result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))
        assert result == {}

    def test_single_node_base_case(self, tmp_path: Path) -> None:
        """Single node should go through base case path."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "single", "max_parallel": 4},
            "nodes": {"only": {"prompt": "test"}},
        }

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            session.write_file("output/draft.md", "done")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"only": "COMPLETED"}
        assert scheduler.node_is_done("only")

    def test_multi_node_sequential(self, tmp_path: Path) -> None:
        """A -> B: B should only start after A completes."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "seq", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A"},
                "b": {"prompt": "B", "deps": ["a"]},
            },
        }
        execution_order: list[str] = []

        async def mock_run_node(node_id, tools_factory, depth=0):
            execution_order.append(node_id)
            session = Session(scheduler.nodes_dir / node_id)
            session.write_file("output/draft.md", f"result:{node_id}")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"a": "COMPLETED", "b": "COMPLETED"}
        assert execution_order.index("a") < execution_order.index("b")

    def test_multi_node_parallel(self, tmp_path: Path) -> None:
        """Independent nodes should both complete."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "par", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A"},
                "b": {"prompt": "B"},
            },
        }

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            session.write_file("output/draft.md", f"result:{node_id}")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"a": "COMPLETED", "b": "COMPLETED"}

    def test_node_failure_with_retry(self, tmp_path: Path) -> None:
        """Node fails once, retries, then succeeds."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "retry", "max_parallel": 4},
            "nodes": {
                "ok": {"prompt": "ok"},
                "flaky": {"prompt": "test", "deps": ["ok"], "retries": 1},
            },
        }
        call_count = 0

        async def mock_run_node(node_id, tools_factory, depth=0):
            nonlocal call_count
            session = Session(scheduler.nodes_dir / node_id)
            if node_id == "flaky":
                call_count += 1
                if call_count == 1:
                    session.write_system("error.log", "error")
                    raise RuntimeError("fail")
            session.write_file("output/draft.md", "ok")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result["flaky"] == "COMPLETED"
        assert call_count == 2

    def test_node_failure_exhausts_retries(self, tmp_path: Path) -> None:
        """Node fails more times than retries allowed."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "fail", "max_parallel": 4},
            "nodes": {
                "ok": {"prompt": "ok"},
                "bad": {"prompt": "test", "deps": ["ok"], "retries": 0},
            },
        }

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            if node_id == "bad":
                session.write_system("error.log", "error")
                raise RuntimeError("always fail")
            session.write_file("output/draft.md", "ok")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"ok": "COMPLETED", "bad": "FAILED"}

    def test_dependency_failure_blocks_downstream(self, tmp_path: Path) -> None:
        """Upstream fails → downstream blocked."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "depfail", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A"},
                "b": {"prompt": "B", "deps": ["a"]},
            },
        }

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            if node_id == "a":
                session.write_system("error.log", "fail")
                raise RuntimeError("fail")
            session.write_file("output/draft.md", "done")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"a": "FAILED", "b": "PENDING"}

    def test_crash_recovery_running_reset(self, tmp_path: Path) -> None:
        """Simulate crash: running state in scheduler_state.json should be reset."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "recover", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A"},
                "b": {"prompt": "B"},
            },
        }
        # Simulate a crash: mark node as running but no output
        scheduler.running.add("a")
        scheduler._save_scheduler_state()

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            session.write_file("output/draft.md", "done")

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(scheduler.run(tools_factory=lambda s: build_tools(s)))

        assert result == {"a": "COMPLETED", "b": "COMPLETED"}
        assert "a" not in scheduler.running

    def test_scheduler_cancel(self, tmp_path: Path) -> None:
        """Cancel should stop scheduling, leaving unstarted nodes as PENDING."""
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "cancel", "max_parallel": 4},
            "nodes": {
                "a": {"prompt": "A"},
                "b": {"prompt": "B"},
                "c": {"prompt": "C"},
            },
        }

        async def mock_run_node(node_id, tools_factory, depth=0):
            session = Session(scheduler.nodes_dir / node_id)
            session.write_file("output/draft.md", "done")

        async def run_and_cancel():
            task = asyncio.create_task(scheduler.run(tools_factory=lambda s: build_tools(s)))
            # Cancel immediately before the loop can start any nodes
            scheduler.cancel()
            return await task

        with patch.object(scheduler, "_run_node", new=mock_run_node):
            result = asyncio.run(run_and_cancel())

        # All nodes should be PENDING because cancel stopped scheduling before any ran
        assert all(status == "PENDING" for status in result.values())


class TestPrepareNode:
    def test_prepare_node_creates_dirs_and_hints(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        scheduler.dag = {
            "dag": {"name": "prep"},
            "nodes": {
                "node1": {"prompt": "do something"},
            },
        }
        session = scheduler.prepare_node("node1")
        assert (session.node_dir / ".system" / "hints.md").exists()
        hints = session.read_system("hints.md")
        assert "do something" in hints
        assert session.get_state("status") == "running"

    def test_prepare_node_copies_inputs(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        # Create source file
        (scheduler.dag_dir / "data.txt").write_text("data")
        scheduler.dag = {
            "dag": {"name": "prep"},
            "nodes": {
                "node1": {"prompt": "test", "inputs": ["data.txt"]},
            },
        }
        session = scheduler.prepare_node("node1")
        assert (session.input_dir / "data.txt").read_text() == "data"

    def test_prepare_node_copies_dep_inputs(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        # Create upstream output under nodes/
        upstream = Session(scheduler.nodes_dir / "upstream")
        upstream.write_file("output/draft.md", "upstream result")

        scheduler.dag = {
            "dag": {"name": "prep"},
            "nodes": {
                "downstream": {
                    "prompt": "test",
                    "dep_inputs": [{"from": "nodes/upstream/output/draft.md", "to": "deps/upstream.md"}],
                },
            },
        }
        session = scheduler.prepare_node("downstream")
        assert (session.input_dir / "deps" / "upstream.md").read_text() == "upstream result"


class TestCopyInput:
    def test_copy_input_plain(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        (scheduler.dag_dir / "src.txt").write_text("hello")
        dst = tmp_path / "dst"
        dst.mkdir()
        scheduler._copy_input("src.txt", dst)
        assert (dst / "src.txt").read_text() == "hello"

    def test_copy_input_with_section(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        (scheduler.dag_dir / "doc.md").write_text("# Intro\nintro text\n# Section\nsection text\n# End\nend text")
        dst = tmp_path / "dst"
        dst.mkdir()
        scheduler._copy_input("doc.md#Section", dst)
        result = (dst / "doc.md").read_text()
        assert "section text" in result
        assert "end text" not in result

    def test_copy_input_missing_source(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        dst = tmp_path / "dst"
        dst.mkdir()
        # Should not raise
        scheduler._copy_input("missing.txt", dst)
        assert not (dst / "missing.txt").exists()


class TestRenderSystemPrompt:
    def test_render_system_prompt(self, tmp_path: Path) -> None:
        scheduler = DAGScheduler(tmp_path, "test")
        result = scheduler._render_system_prompt("hints here", "tools here")
        assert "hints here" in result
        assert "tools here" in result
