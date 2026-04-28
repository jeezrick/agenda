from __future__ import annotations

"""Daemon 模式 — 长期运行的 Agenda Server。

学 Butterfly 的 server.py + watcher.py 设计：
- PID 文件 + flock 单例保护
- SIGTERM/SIGINT 优雅关闭
- SessionWatcher 扫描 DAG 目录，管理节点 task
- 自动恢复 crashed session
"""

import asyncio
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

from .scheduler import DAGScheduler
from .tools import build_tools

# ── PID / Lock 文件 ─────────────────────────────────────────────────────────

_DEFAULT_DAG_DIR = Path.cwd()


def _pid_file(dag_dir: Path) -> Path:
    return dag_dir / ".system" / "agenda.pid"


def _lock_file(dag_dir: Path) -> Path:
    return dag_dir / ".system" / "agenda.lock"


def _log_file(dag_dir: Path) -> Path:
    return dag_dir / ".system" / "agenda.log"


_lock_fd: IO | None = None


def _acquire_lock(dag_dir: Path) -> bool:
    """尝试获取文件锁。返回 True 表示成功。"""
    global _lock_fd
    if _lock_fd is not None:
        return True
    path = _lock_file(dag_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a")  # noqa: SIM115
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fd.close()
        return False
    _lock_fd = fd
    return True


def _release_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        _lock_fd.close()
    _lock_fd = None


def _write_pid(dag_dir: Path) -> None:
    pf = _pid_file(dag_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()))


def _read_pid(dag_dir: Path) -> int | None:
    pf = _pid_file(dag_dir)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _clear_pid(dag_dir: Path) -> None:
    with contextlib.suppress(OSError):
        _pid_file(dag_dir).unlink(missing_ok=True)


def _is_running(dag_dir: Path) -> int | None:
    """返回 PID 如果 daemon 在运行，否则 None。"""
    pid = _read_pid(dag_dir)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        _clear_pid(dag_dir)
        return None


# ── NodeWatcher ─────────────────────────────────────────────────────────────

class NodeWatcher:
    """扫描 DAG 节点目录，管理正在运行的节点 task。

    类似于 Butterfly 的 SessionWatcher，但管理的是 DAG 节点而非 session。
    """

    def __init__(self, dag_dir: Path, dag_file: Path) -> None:
        self.dag_dir = dag_dir
        self.dag_file = dag_file
        self._active: dict[str, asyncio.Task] = {}  # node_id → task
        self._finished: set[str] = set()
        self._scheduler: DAGScheduler | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        """主循环，直到 stop_event 被设置。"""
        print(f"[daemon] Watching DAG: {self.dag_dir}")

        # 初始化 scheduler
        self._scheduler = DAGScheduler(self.dag_dir.parent, self.dag_dir.name)
        self._scheduler.dag_file = self.dag_file
        self._scheduler.load()

        # 初始扫描：恢复已存在的节点
        discovered = await self._scan()
        if discovered:
            print(f"[daemon] Discovered {len(discovered)} pending nodes: {', '.join(discovered)}")

        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            new = await self._scan()
            for node_id in new:
                print(f"[daemon] Discovered: {node_id}")

        # 优雅关闭：取消所有活动 task
        tasks = list(self._active.values())
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        print("[daemon] All node tasks stopped.")

    async def _scan(self) -> list[str]:
        """扫描节点目录，启动需要运行的节点。"""
        if self._scheduler is None:
            return []

        scheduler = self._scheduler
        nodes = scheduler.dag.get("nodes", {})
        discovered: list[str] = []

        for node_id in nodes:
            node_dir = scheduler.nodes_dir / node_id
            if not node_dir.exists():
                continue

            # 跳过已完成的
            if scheduler.node_is_done(node_id):
                if node_id not in self._finished:
                    self._finished.add(node_id)
                continue

            # 清理已完成的 task
            if node_id in self._active:
                task = self._active[node_id]
                if task.done():
                    exc = task.exception() if not task.cancelled() else None
                    if exc:
                        print(f"[daemon] Node {node_id} error: {exc}")
                    else:
                        print(f"[daemon] Node finished: {node_id}")
                    del self._active[node_id]
                    self._finished.add(node_id)
                continue  # 已在运行

            # 检查依赖是否满足
            deps = nodes[node_id].get("deps", [])
            if not all(scheduler.node_is_done(d) for d in deps):
                continue

            # 检查是否失败过且重试次数未耗尽
            if scheduler.node_is_failed(node_id):
                retries = scheduler.retries.get(node_id, 0)
                max_retry = nodes[node_id].get("retries", 3)
                if retries >= max_retry:
                    continue
                # 清除错误标记，准备重试
                error_log = node_dir / ".system" / "error.log"
                if error_log.exists():
                    error_log.unlink()
                print(f"[daemon] Node {node_id} retry ({retries + 1}/{max_retry})")

            discovered.append(node_id)
            task = asyncio.create_task(
                self._run_node(node_id),
                name=f"node-{node_id}",
            )
            self._active[node_id] = task

        return discovered

    async def _run_node(self, node_id: str) -> None:
        """运行单个节点（和 scheduler._run_node 类似，但适合 daemon 调用）。"""
        if self._scheduler is None:
            return
        try:
            await self._scheduler._run_node(
                node_id,
                tools_factory=lambda session: build_tools(session),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[daemon] Node {node_id} crashed: {exc}")


# ── Server core ─────────────────────────────────────────────────────────────

async def _run(dag_dir: Path, dag_file: Path) -> None:
    """Daemon 主循环。"""
    dag_dir.mkdir(parents=True, exist_ok=True)

    if not _acquire_lock(dag_dir):
        existing = _read_pid(dag_dir)
        hint = f" (pid={existing})" if existing else ""
        print(
            f"Error: agenda daemon already running{hint} for {dag_dir}.\n"
            f"Hint: run `agenda daemon stop`, or check `ps ax | grep agenda.daemon`",
            file=sys.stderr,
            flush=True,
        )
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    _write_pid(dag_dir)
    print(f"agenda daemon started (pid={os.getpid()}). DAG: {dag_dir.absolute()}")

    watcher = NodeWatcher(dag_dir, dag_file)
    watcher_task = asyncio.create_task(watcher.run(stop_event))
    tasks: list[asyncio.Task] = [watcher_task]

    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION,
        )
        for p in pending:
            p.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for d in done:
            exc = d.exception()
            if exc is not None:
                raise exc
    finally:
        _clear_pid(dag_dir)
        _release_lock()
    print("agenda daemon stopped.")


def _start_foreground(dag_dir: Path, dag_file: Path) -> int:
    """前台运行 daemon。"""
    asyncio.run(_run(dag_dir, dag_file))
    return 0


def _start_daemon(dag_dir: Path, dag_file: Path) -> int:
    """后台运行 daemon。"""
    existing = _is_running(dag_dir)
    if existing:
        print(f"agenda daemon already running (pid={existing}).")
        return 0

    dag_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "agenda.daemon",
        "--foreground",
        "--dag-dir", str(dag_dir),
    ]
    lf = _log_file(dag_dir)
    lf.parent.mkdir(parents=True, exist_ok=True)
    with open(lf, "a") as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path.cwd()),
        )

    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"Error: daemon exited immediately (code={proc.returncode}). Check {lf}")
        return 1

    print(f"agenda daemon started in background (pid={proc.pid}). Log: {lf}")
    return 0


def _cmd_stop(dag_dir: Path) -> int:
    pid = _is_running(dag_dir)
    if pid is None:
        print("agenda daemon is not running.")
        return 0
    print(f"Stopping agenda daemon (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(dag_dir)
        print("Daemon already stopped.")
        return 0
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _clear_pid(dag_dir)
            print("Daemon stopped.")
            return 0
    print("Warning: daemon did not stop within 2s. Sending SIGKILL...")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    _clear_pid(dag_dir)
    print("Daemon killed.")
    return 0


def _cmd_status(dag_dir: Path) -> int:
    pid = _is_running(dag_dir)
    if pid:
        print(f"agenda daemon is running (pid={pid}).")
    else:
        print("agenda daemon is not running.")
    return 0


# ── CLI entry ───────────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Agenda daemon — 长期运行节点调度",
    )
    parser.add_argument("--foreground", action="store_true",
                        help="Run in foreground (don't daemonize)")
    parser.add_argument("--dag-dir", type=str, default=str(Path.cwd()),
                        help="DAG directory to watch")
    parser.add_argument("command", nargs="?", choices=["start", "stop", "status"],
                        help="Daemon command")

    args = parser.parse_args()
    dag_dir = Path(args.dag_dir).resolve()
    dag_file = dag_dir / "dag.yaml"

    if args.foreground:
        return _start_foreground(dag_dir, dag_file)

    if args.command == "stop":
        return _cmd_stop(dag_dir)
    elif args.command == "status":
        return _cmd_status(dag_dir)
    else:  # start (default)
        return _start_daemon(dag_dir, dag_file)


if __name__ == "__main__":
    sys.exit(main())
