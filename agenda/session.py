from __future__ import annotations

"""Session — 双目录隔离 + 持久化。"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


class Session:
    """
    一个 Session 就是一个目录。

    目录结构：
        nodes/{node_id}/
            .context/     ← Agent 可见（读/写）
            .system/      ← 系统私有（Agent 不可见）
            output/       ← Agent 产物
            children/     ← 子 Agent 的 session（Agent 不可见）

    持久化（学 Butterfly 的 append-only JSONL）：
        .system/messages.jsonl  ← 对话历史（append-only，每轮后 flush）
        .system/state.json      ← 运行状态（running/completed/failed）
        .system/session.jsonl   ← 运行时事件日志
    """

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir).resolve()
        self.context_dir = self.node_dir / ".context"
        self.system_dir = self.node_dir / ".system"
        self.output_dir = self.node_dir / "output"
        self.children_dir = self.node_dir / "children"

        # 自动创建目录
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.children_dir.mkdir(parents=True, exist_ok=True)

        # 文件路径
        self._messages_path = self.system_dir / "messages.jsonl"
        self._events_path = self.system_dir / "session.jsonl"
        self._state_path = self.system_dir / "state.json"

    # --- Agent 可见操作 ---

    def read_context(self, rel_path: str) -> str:
        """Agent 读取 .context/ 或 output/ 下的文件。"""
        target = self._resolve_safe(rel_path)
        if not target or not target.exists():
            return f"[错误] 文件不存在: {rel_path}"
        return target.read_text(encoding="utf-8")

    def write_output(self, rel_path: str, content: str) -> str:
        """Agent 写入 output/ 目录。"""
        target = self.output_dir / rel_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[成功] 已写入 {rel_path}"

    def list_context(self, rel_path: str = ".") -> str:
        """Agent 列出 .context/ 或 output/ 下的目录。"""
        target = self._resolve_safe(rel_path)
        if not target or not target.exists():
            return f"[错误] 目录不存在: {rel_path}"
        lines = []
        for item in sorted(target.iterdir()):
            t = "[目录]" if item.is_dir() else "[文件]"
            lines.append(f"{t} {item.name}")
        return "\n".join(lines) or "(空)"

    # --- 系统私有操作 ---

    def log_message(self, message: dict) -> None:
        """追加消息到 .system/session.jsonl（append-only 运行时日志）。"""
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def write_system(self, rel_path: str, content: str) -> None:
        """系统写入 .system/ 目录（Agent 不可见）。"""
        target = self.system_dir / rel_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_system(self, rel_path: str) -> str:
        """系统读取 .system/ 目录。"""
        target = self.system_dir / rel_path.lstrip("/")
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")

    def set_state(self, key: str, value: Any) -> None:
        """读写 .system/state.json。"""
        state = {}
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        state[key] = value
        self._state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_state(self, key: str, default: Any = None) -> Any:
        if not self._state_path.exists():
            return default
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state.get(key, default)
        except (json.JSONDecodeError, OSError):
            return default

    # --- 持久化：messages.jsonl（学 Butterfly 的 context.jsonl） ---

    def save_message(self, message: dict) -> None:
        """追加单条消息到 messages.jsonl。"""
        with open(self._messages_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load_messages(self) -> list[dict]:
        """从 messages.jsonl 恢复对话历史。"""
        messages: list[dict] = []
        if not self._messages_path.exists():
            return messages
        with open(self._messages_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return messages

    def clear_messages(self) -> None:
        """清空 messages.jsonl（重置节点时调用）。"""
        if self._messages_path.exists():
            self._messages_path.unlink()

    # --- 内部工具 ---

    def _resolve_safe(self, rel_path: str) -> Path | None:
        """解析路径，确保只在 .context/ 或 output/ 内。"""
        raw = Path(rel_path.lstrip("/"))
        for base in (self.context_dir, self.output_dir):
            candidate = (base / raw).resolve()
            try:
                candidate.relative_to(base.resolve())
                return candidate
            except ValueError:
                continue
        return None

    @property
    def output_exists(self) -> bool:
        """output/draft.md 存在即表示节点完成（默认判定）。"""
        return (self.output_dir / "draft.md").exists()

    def is_done(self, done_file: str | None = None) -> bool:
        """检查节点是否完成，支持自定义完成标记文件。"""
        if done_file:
            return (self.output_dir / done_file).exists()
        return self.output_exists

    def is_failed(self) -> bool:
        """检查节点是否失败：.system/error.log 存在。"""
        return (self.system_dir / "error.log").exists()

    # --- 子 Agent 管理 ---

    def child_session(self, name: str) -> "Session":
        """获取/创建子 Agent 的 session。"""
        child_dir = self.children_dir / name
        return Session(child_dir)

