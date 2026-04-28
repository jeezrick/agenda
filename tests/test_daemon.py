"""Tests for agenda.daemon — PID files, locks, NodeWatcher, and CLI commands."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agenda.daemon import (
    NodeWatcher,
    _acquire_lock,
    _clear_pid,
    _cmd_status,
    _cmd_stop,
    _is_running,
    _lock_file,
    _log_file,
    _pid_file,
    _read_pid,
    _release_lock,
    _write_pid,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_pid_file(self, tmp_path: Path) -> None:
        assert _pid_file(tmp_path) == tmp_path / ".system" / "agenda.pid"

    def test_lock_file(self, tmp_path: Path) -> None:
        assert _lock_file(tmp_path) == tmp_path / ".system" / "agenda.lock"

    def test_log_file(self, tmp_path: Path) -> None:
        assert _log_file(tmp_path) == tmp_path / ".system" / "agenda.log"


# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------


class TestPidManagement:
    def test_write_and_read_pid(self, tmp_path: Path) -> None:
        _write_pid(tmp_path)
        pid = _read_pid(tmp_path)
        assert pid == os.getpid()

    def test_read_pid_missing(self, tmp_path: Path) -> None:
        assert _read_pid(tmp_path) is None

    def test_read_pid_invalid(self, tmp_path: Path) -> None:
        pf = _pid_file(tmp_path)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("not-a-number")
        assert _read_pid(tmp_path) is None

    def test_clear_pid(self, tmp_path: Path) -> None:
        _write_pid(tmp_path)
        assert _pid_file(tmp_path).exists()
        _clear_pid(tmp_path)
        assert not _pid_file(tmp_path).exists()

    def test_clear_pid_missing_no_error(self, tmp_path: Path) -> None:
        # Should not raise even if file doesn't exist
        _clear_pid(tmp_path)


# ---------------------------------------------------------------------------
# _is_running
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_no_pid_file(self, tmp_path: Path) -> None:
        assert _is_running(tmp_path) is None

    def test_pid_running(self, tmp_path: Path) -> None:
        _write_pid(tmp_path)
        # Our own process is definitely running
        assert _is_running(tmp_path) == os.getpid()

    def test_pid_stale(self, tmp_path: Path) -> None:
        pf = _pid_file(tmp_path)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("999999")
        # Process 999999 almost certainly doesn't exist
        assert _is_running(tmp_path) is None
        # Stale PID file should be cleaned up
        assert not pf.exists()

    def test_pid_permission_denied(self, tmp_path: Path) -> None:
        pf = _pid_file(tmp_path)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("1")  # init, usually requires root to signal
        # PermissionError should be handled gracefully
        result = _is_running(tmp_path)
        # On most systems, signal pid 1 requires privileges.
        # The implementation catches PermissionError and clears the PID.
        assert result is None


# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------


class TestLockManagement:
    def test_acquire_and_release_lock(self, tmp_path: Path) -> None:
        # Ensure clean state
        _release_lock()
        assert _acquire_lock(tmp_path) is True
        _release_lock()

    def test_acquire_lock_twice_same_dir(self, tmp_path: Path) -> None:
        _release_lock()
        assert _acquire_lock(tmp_path) is True
        # Second acquire should succeed because _lock_fd is already set
        assert _acquire_lock(tmp_path) is True
        _release_lock()

    def test_acquire_lock_different_dirs(self, tmp_path: Path) -> None:
        _release_lock()
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        assert _acquire_lock(dir1) is True
        # After releasing, can acquire a different dir
        _release_lock()
        assert _acquire_lock(dir2) is True
        _release_lock()

    def test_release_lock_idempotent(self) -> None:
        # Should not raise even if no lock held
        _release_lock()
        _release_lock()


# ---------------------------------------------------------------------------
# NodeWatcher
# ---------------------------------------------------------------------------


class TestNodeWatcher:
    def test_init(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        watcher = NodeWatcher(dag_dir, dag_file)
        assert watcher.dag_dir == dag_dir
        assert watcher.dag_file == dag_file
        assert watcher._active == {}
        assert watcher._finished == set()
        assert watcher._scheduler is None

    def test_run_stops_on_event(self, tmp_path: Path) -> None:
        """NodeWatcher.run() should exit when stop_event is set."""
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        dag_dir.mkdir(parents=True, exist_ok=True)
        dag_file.write_text("dag:\n  name: test\nnodes: {}", encoding="utf-8")

        watcher = NodeWatcher(dag_dir, dag_file)
        stop_event = asyncio.Event()

        async def _test() -> None:
            task = asyncio.create_task(watcher.run(stop_event))
            await asyncio.sleep(0.05)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(_test())

    def test_scan_no_scheduler(self, tmp_path: Path) -> None:
        watcher = NodeWatcher(tmp_path, tmp_path / "dag.yaml")
        result = asyncio.run(watcher._scan())
        assert result == []

    def test_scan_skips_done_nodes(self, tmp_path: Path) -> None:
        # Use mkdtemp directly to avoid pytest tmp_path quirks on macOS
        tmp = Path(tempfile.mkdtemp())
        try:
            dag_dir = tmp / "dag"
            dag_file = dag_dir / "dag.yaml"
            dag_dir.mkdir(parents=True, exist_ok=True)
            dag_file.write_text("test", encoding="utf-8")
            os.makedirs(str(dag_dir / "nodes" / "a"), exist_ok=True)

            watcher = NodeWatcher(dag_dir, dag_file)
            watcher._scheduler = MagicMock()
            watcher._scheduler.dag = {"nodes": {"a": {"prompt": "task A"}}}
            watcher._scheduler.nodes_dir = dag_dir / "nodes"
            watcher._scheduler.node_is_done.return_value = True

            result = asyncio.run(watcher._scan())
            assert result == []
            # _finished should be updated when node_is_done is True
            assert "a" in watcher._finished
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scan_waits_for_deps(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        dag_dir.mkdir(parents=True, exist_ok=True)
        dag_file.write_text(
            """
dag:
  name: test
nodes:
  a:
    prompt: "task A"
  b:
    prompt: "task B"
    deps: [a]
""",
            encoding="utf-8",
        )

        watcher = NodeWatcher(dag_dir, dag_file)
        watcher._scheduler = MagicMock()
        watcher._scheduler.dag = {
            "nodes": {
                "a": {"prompt": "task A"},
                "b": {"prompt": "task B", "deps": ["a"]},
            }
        }
        watcher._scheduler.nodes_dir = dag_dir / "nodes"
        # Neither done, but a has no deps so it should be discovered
        watcher._scheduler.node_is_done.return_value = False
        watcher._scheduler.node_is_failed.return_value = False

        # Create node dirs so they exist
        (dag_dir / "nodes" / "a").mkdir(parents=True)
        (dag_dir / "nodes" / "b").mkdir(parents=True)

        result = asyncio.run(watcher._scan())
        # Only 'a' should be discovered (b's deps not satisfied)
        assert "a" in result
        assert "b" not in result

    def test_scan_respects_max_retries(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        dag_dir.mkdir(parents=True, exist_ok=True)
        dag_file.write_text(
            """
dag:
  name: test
nodes:
  a:
    prompt: "task A"
    retries: 2
""",
            encoding="utf-8",
        )

        watcher = NodeWatcher(dag_dir, dag_file)
        watcher._scheduler = MagicMock()
        watcher._scheduler.dag = {"nodes": {"a": {"prompt": "task A", "retries": 2}}}
        watcher._scheduler.nodes_dir = dag_dir / "nodes"
        watcher._scheduler.node_is_done.return_value = False
        watcher._scheduler.node_is_failed.return_value = True
        watcher._scheduler.retries = {"a": 2}  # Already at max

        (dag_dir / "nodes" / "a").mkdir(parents=True)

        result = asyncio.run(watcher._scan())
        assert result == []

    def test_run_node_catches_exception(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        watcher = NodeWatcher(dag_dir, dag_file)
        watcher._scheduler = MagicMock()
        watcher._scheduler._run_node.side_effect = RuntimeError("boom")

        # Should not raise
        asyncio.run(watcher._run_node("a"))

    def test_run_node_propagates_cancel(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "dag"
        dag_file = dag_dir / "dag.yaml"
        watcher = NodeWatcher(dag_dir, dag_file)
        watcher._scheduler = MagicMock()
        watcher._scheduler._run_node.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watcher._run_node("a"))


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_status_running(self, tmp_path: Path, monkeypatch) -> None:
        _write_pid(tmp_path)
        # _is_running will check our own PID which is alive
        assert _cmd_status(tmp_path) == 0

    def test_status_not_running(self, tmp_path: Path) -> None:
        assert _cmd_status(tmp_path) == 0


class TestCmdStop:
    def test_stop_not_running(self, tmp_path: Path) -> None:
        assert _cmd_stop(tmp_path) == 0

    def test_stop_running(self, tmp_path: Path, monkeypatch) -> None:
        _write_pid(tmp_path)
        # Mock os.kill to avoid actually signaling our own process
        kill_calls = []

        def mock_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            if sig == signal.SIGTERM:
                # Simulate process exiting after SIGTERM
                raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", mock_kill)
        assert _cmd_stop(tmp_path) == 0
        assert any(sig == signal.SIGTERM for _, sig in kill_calls)

    def test_stop_sigkill_fallback(self, tmp_path: Path, monkeypatch) -> None:
        _write_pid(tmp_path)
        kill_calls = []

        def mock_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            # Always say process exists except for SIGKILL
            if sig == signal.SIGKILL:
                raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", mock_kill)
        assert _cmd_stop(tmp_path) == 0
        assert any(sig == signal.SIGKILL for _, sig in kill_calls)
