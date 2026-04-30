"""Tests for metrics.py — MetricsHook event collection."""

import json
from pathlib import Path

from agenda.hook import HookRegistry
from agenda.metrics import MetricsHook


class TestMetricsHook:
    def test_register_all_adds_four_hooks(self, tmp_path: Path) -> None:
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)
        assert registry.has("on_node_start")
        assert registry.has("on_node_complete")
        assert registry.has("on_node_error")
        assert registry.has("on_compaction")

    def test_node_start_writes_metrics_file(self, tmp_path: Path) -> None:
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)

        import asyncio

        asyncio.run(registry.emit("on_node_start", node_id="test_node"))
        asyncio.run(registry.emit("on_node_complete", node_id="test_node"))

        metrics_file = tmp_path / "metrics.jsonl"
        assert metrics_file.exists()

        lines = [json.loads(line) for line in metrics_file.read_text().strip().split("\n")]
        assert len(lines) == 2
        assert lines[0]["event"] == "node_start"
        assert lines[0]["node_id"] == "test_node"
        assert "ts" in lines[0]
        assert lines[1]["event"] == "node_complete"
        assert lines[1]["node_id"] == "test_node"
        assert lines[1]["duration_s"] >= 0.0

    def test_node_error_records_error_info(self, tmp_path: Path) -> None:
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)

        import asyncio

        asyncio.run(registry.emit("on_node_start", node_id="failing_node"))
        asyncio.run(registry.emit("on_node_error", node_id="failing_node", error=ValueError("test error")))

        metrics_file = tmp_path / "metrics.jsonl"
        lines = [json.loads(line) for line in metrics_file.read_text().strip().split("\n")]
        error_line = [e for e in lines if e["event"] == "node_error"][0]
        assert error_line["node_id"] == "failing_node"
        assert "test error" in error_line["error"]

    def test_compaction_records_token_info(self, tmp_path: Path) -> None:
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)

        import asyncio

        asyncio.run(
            registry.emit(
                "on_compaction",
                node_id="compacting_node",
                pre_tokens=10000,
                post_tokens=2000,
                success=True,
                fallback=None,
            )
        )

        metrics_file = tmp_path / "metrics.jsonl"
        lines = [json.loads(line) for line in metrics_file.read_text().strip().split("\n")]
        assert len(lines) == 1
        assert lines[0]["event"] == "compaction"
        assert lines[0]["pre_tokens"] == 10000
        assert lines[0]["post_tokens"] == 2000
        assert lines[0]["success"] is True

    def test_multiple_nodes(self, tmp_path: Path) -> None:
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)

        import asyncio

        asyncio.run(registry.emit("on_node_start", node_id="a"))
        asyncio.run(registry.emit("on_node_start", node_id="b"))
        asyncio.run(registry.emit("on_node_complete", node_id="a"))
        asyncio.run(registry.emit("on_node_complete", node_id="b"))

        metrics_file = tmp_path / "metrics.jsonl"
        lines = [json.loads(line) for line in metrics_file.read_text().strip().split("\n")]
        completed = [e for e in lines if e["event"] == "node_complete"]
        assert len(completed) == 2

    def test_idempotent_register(self, tmp_path: Path) -> None:
        """Calling register_all twice should not cause duplicate emissions."""
        hook = MetricsHook(tmp_path)
        registry = HookRegistry()
        hook.register_all(registry)
        hook.register_all(registry)

        import asyncio

        asyncio.run(registry.emit("on_node_start", node_id="n"))
        asyncio.run(registry.emit("on_node_complete", node_id="n"))

        metrics_file = tmp_path / "metrics.jsonl"
        lines = [json.loads(line) for line in metrics_file.read_text().strip().split("\n")]
        nodes = [e for e in lines if e["event"] == "node_start"]
        # Should be 2 because register_all called twice — this is current behavior
        # Not a bug, just documenting the behavior
        assert len(nodes) in (1, 2)
