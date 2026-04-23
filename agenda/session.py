from __future__ import annotations

"""Session — 双目录隔离 + Turn 持久化 + IPC。

学 Butterfly 的关键设计：
- turns.jsonl: turn 级别持久化（每轮 LLM 运行打包保存）
- events.jsonl: IPC 事件队列（其他 Agent 可写入消息/中断）
- 取消时 save_partial_turn，不丢失已 committed 的中间结果
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .guardian import Guardian


class Session:
    """
    一个 Session 就是一个目录。

    目录结构：
        nodes/{node_id}/
            .context/     ← Agent 可见（读/写）
            .system/      ← 系统私有（Agent 不可见）
                turns.jsonl     ← 对话历史（turn 级别，append-only）
                events.jsonl    ← IPC 事件队列（其他 Agent 可写入）
                state.json      ← 运行状态
                session.jsonl   ← 运行时日志
            output/       ← Agent 产物
            children/     ← 子 Agent 的 session
    """

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir).resolve()
        self.context_dir = self.node_dir / ".context"
        self.system_dir = self.node_dir / ".system"
        self.output_dir = self.node_dir / "output"
        self.children_dir = self.node_dir / "children"

        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.children_dir.mkdir(parents=True, exist_ok=True)

        # Guardian 硬边界（root = node_dir）
        self.guardian = Guardian(self.node_dir)

        # 文件路径
        self._turns_path = self.system_dir / "turns.jsonl"
        self._events_path = self.system_dir / "events.jsonl"
        self._session_log_path = self.system_dir / "session.jsonl"
        self._state_path = self.system_dir / "state.json"

    # --- Agent 可见操作 ---

    def read_context(self, rel_path: str) -> str:
        """Agent 读取 .context/ 或 output/ 下的文件。"""
        try:
            target = self.guardian.check_read(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        safe = self._resolve_safe(rel_path)
        if not safe:
            return f"[错误] 路径不允许: {rel_path}"
        if not target.exists():
            return f"[错误] 文件不存在: {rel_path}"
        return target.read_text(encoding="utf-8")

    def write_output(self, rel_path: str, content: str) -> str:
        """Agent 写入 output/ 目录。"""
        try:
            target = self.guardian.check_write(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        # 语义限制：只能写入 output/
        try:
            target.relative_to(self.output_dir)
        except ValueError:
            return f"[错误] 只能写入 output/ 目录: {rel_path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[成功] 已写入 {rel_path}"

    def list_context(self, rel_path: str = ".") -> str:
        """Agent 列出 .context/ 或 output/ 下的目录。"""
        try:
            target = self.guardian.check_read(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        safe = self._resolve_safe(rel_path)
        if not safe:
            return f"[错误] 路径不允许: {rel_path}"
        if not target.exists():
            return f"[错误] 目录不存在: {rel_path}"
        lines = []
        for item in sorted(target.iterdir()):
            t = "[目录]" if item.is_dir() else "[文件]"
            lines.append(f"{t} {item.name}")
        return "\n".join(lines) or "(空)"

    # --- 系统私有操作 ---

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

    # --- Turn 级别持久化（学 Butterfly 的 context.jsonl turn 事件） ---

    def save_turn(self, turn: dict) -> None:
        """追加一个 turn 到 turns.jsonl。

        turn 格式: {"type": "turn", "messages": [...], "usage": {...}, "ts": "..."}
        """
        with open(self._turns_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

    def save_partial_turn(self, messages: list[dict], iteration: int, interrupted: bool = True) -> None:
        """取消时保存已 committed 的部分 turn（Butterfly v2.0.34 式修复）。"""
        turn = {
            "type": "turn",
            "messages": list(messages),
            "iteration": iteration,
            "interrupted": interrupted,
            "ts": datetime.now().isoformat(),
        }
        self.save_turn(turn)

    def load_turns(self) -> list[dict]:
        """从 turns.jsonl 恢复所有 turn。"""
        turns: list[dict] = []
        if not self._turns_path.exists():
            return turns
        with open(self._turns_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return turns

    def replay_history(self) -> list[dict]:
        """从 turns.jsonl replay 成 flat messages 列表（用于恢复 AgentLoop）。"""
        messages: list[dict] = []
        for turn in self.load_turns():
            for msg in turn.get("messages", []):
                messages.append(msg)
        return messages

    def clear_turns(self) -> None:
        """清空 turns.jsonl（重置节点时调用）。"""
        if self._turns_path.exists():
            self._turns_path.unlink()

    # --- IPC: events.jsonl（进程间通信） ---

    def append_event(self, event: dict) -> None:
        """向 events.jsonl 追加事件。其他进程/Agent 可读。"""
        event.setdefault("ts", datetime.now().isoformat())
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def poll_events(self, offset: int = 0) -> tuple[list[dict], int]:
        """从 offset（字节偏移）开始读取新事件。

        Returns: (events, new_offset)
        """
        if not self._events_path.exists():
            return [], 0
        events: list[dict] = []
        with open(self._events_path, "r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return events, new_offset

    def events_size(self) -> int:
        """当前 events.jsonl 的字节大小（用于初始化 offset）。"""
        if not self._events_path.exists():
            return 0
        return self._events_path.stat().st_size

    def send_interrupt(self, source: str = "scheduler") -> None:
        """向该 session 发送中断信号。"""
        self.append_event({"type": "interrupt", "from": source})

    def send_message(self, content: str, source: str) -> None:
        """向该 session 发送消息（模拟用户输入）。"""
        self.append_event({"type": "message", "from": source, "content": content})

    # --- 运行时日志（保留向后兼容） ---

    def log_message(self, message: dict) -> None:
        """追加消息到 .system/session.jsonl（append-only 运行时日志）。"""
        with open(self._session_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    # --- 内部工具 ---

    def _resolve_safe(self, rel_path: str) -> Path | None:
        """语义检查：Agent 只能访问 .context/ 或 output/ 下的内容。

        底层路径安全由 Guardian 负责（resolve + relative_to 防逃逸）。
        这里只做语义范围限制。
        """
        target = self.guardian.resolve(rel_path)
        for base in (self.context_dir, self.output_dir):
            try:
                target.relative_to(base)
                return target
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
