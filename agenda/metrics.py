from __future__ import annotations

"""Metrics Hook — 节点指标收集（基于 HookRegistry）。

## 设计

MetricsHook 是一个标准钩子消费者：注册到 HookRegistry 后自动
接收节点生命周期事件，写入 .system/metrics.jsonl。

每条记录是一个 JSON 行，包含时间戳：
    {"event": "node_start", "node_id": "research", "ts": "..."}
    {"event": "node_complete", "node_id": "research", "duration_s": 1.23, "ts": "..."}
    {"event": "compaction", "node_id": "...", "pre_tokens": 10000, "post_tokens": 2000, "success": true, "ts": "..."}

## 实现细节

- _start_times 用 time.monotonic()（不受系统时钟调整影响）
- 如果 on_node_complete 在 on_node_start 之前被调用（异常情况），elapsed = 0
- 不保证原子写入（metrics 不是关键数据，允许部分损坏）
"""

import json
import time
from datetime import datetime
from pathlib import Path

from .hook import HookRegistry


class MetricsHook:
    """指标收集钩子。注册到 HookRegistry 后自动记录指标。"""

    def __init__(self, system_dir: Path) -> None:
        self.system_dir = Path(system_dir)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.system_dir / "metrics.jsonl"
        self._start_times: dict[str, float] = {}

    def register_all(self, hooks: HookRegistry) -> None:
        """注册所有指标钩子事件。"""
        hooks.register("on_node_start", self._on_node_start)
        hooks.register("on_node_complete", self._on_node_complete)
        hooks.register("on_node_error", self._on_node_error)
        hooks.register("on_compaction", self._on_compaction)

    def _write(self, data: dict) -> None:
        data["ts"] = datetime.now().isoformat()
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _on_node_start(self, **kwargs: object) -> None:
        node_id = str(kwargs.get("node_id", "?"))
        self._start_times[node_id] = time.monotonic()
        self._write({"event": "node_start", "node_id": node_id})

    def _on_node_complete(self, **kwargs: object) -> None:
        node_id = str(kwargs.get("node_id", "?"))
        elapsed = time.monotonic() - self._start_times.pop(node_id, time.monotonic())
        self._write(
            {
                "event": "node_complete",
                "node_id": node_id,
                "duration_s": round(elapsed, 2),
            }
        )

    def _on_node_error(self, **kwargs: object) -> None:
        node_id = str(kwargs.get("node_id", "?"))
        elapsed = time.monotonic() - self._start_times.pop(node_id, time.monotonic())
        error = kwargs.get("error")
        self._write(
            {
                "event": "node_error",
                "node_id": node_id,
                "duration_s": round(elapsed, 2),
                "error": str(error) if error else None,
            }
        )

    def _on_compaction(self, **kwargs: object) -> None:
        self._write(
            {
                "event": "compaction",
                "node_id": kwargs.get("node_id"),
                "pre_tokens": kwargs.get("pre_tokens"),
                "post_tokens": kwargs.get("post_tokens"),
                "success": kwargs.get("success"),
                "fallback": kwargs.get("fallback"),
            }
        )
