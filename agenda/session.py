from __future__ import annotations

"""Session — 三目录隔离 + Turn 持久化 + IPC。

目录结构：
    nodes/{node_id}/
        input/        ← 系统输入（Agent 只读）
            由 prepare_node 复制 inputs + dep_inputs
        workspace/    ← Agent 工作区（可读写）
            草稿、笔记、中间产物
        output/       ← Agent 最终产物（可写）
            默认 output/draft.md 为完成标记
        .system/      ← 系统私有（Agent 不可见）
            turns.jsonl     ← 对话历史（turn 级别，append-only）
            events.jsonl    ← IPC 事件队列
            state.json      ← 运行状态
            hints.md        ← 系统提示
        children/     ← 子 Agent 的 session
"""

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .guardian import Guardian


class Session:
    """
    一个 Session 就是一个目录。
    """

    def __init__(self, node_dir: Path) -> None:
        self.node_dir = Path(node_dir).resolve()
        self.input_dir = self.node_dir / "input"
        self.workspace_dir = self.node_dir / "workspace"
        self.output_dir = self.node_dir / "output"
        self.system_dir = self.node_dir / ".system"
        self.children_dir = self.node_dir / "children"

        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.children_dir.mkdir(parents=True, exist_ok=True)

        # Guardian 硬边界（root = node_dir）
        self.guardian = Guardian(self.node_dir)

        # 文件路径
        self._turns_path = self.system_dir / "turns.jsonl"
        self._events_path = self.system_dir / "events.jsonl"
        self._state_path = self.system_dir / "state.json"

    # --- Agent 可见操作 ---

    def read_file(self, rel_path: str) -> str:
        """Agent 读取 input/、workspace/ 或 output/ 下的文件。"""
        try:
            target = self.guardian.check_read(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        safe = self._resolve_safe(rel_path)
        if not safe:
            return self._format_path_error(rel_path)
        if not target.exists():
            return f"[错误] 文件不存在: {rel_path}\n提示: 可用 list_dir('.') 查看可用目录和文件。"
        return target.read_text(encoding="utf-8")

    def write_file(self, rel_path: str, content: str) -> str:
        """Agent 写入 workspace/ 或 output/ 目录。"""
        try:
            target = self.guardian.check_write(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        # 语义限制：只能写入 workspace/ 或 output/
        try:
            target.relative_to(self.workspace_dir)
        except ValueError:
            try:
                target.relative_to(self.output_dir)
            except ValueError:
                return (
                    f"[错误] 只能写入 workspace/ 或 output/ 目录: {rel_path}\n"
                    f"提示: workspace/ 放草稿和中间产物，output/ 放最终产物。"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[成功] 已写入 {rel_path}"

    def list_dir(self, rel_path: str = ".") -> str:
        """Agent 列出 input/、workspace/ 或 output/ 下的目录。"""
        # 对 "." 做友好映射：展示可见目录概览
        if rel_path in (".", ""):
            return self._list_root_overview()

        try:
            target = self.guardian.check_read(rel_path)
        except PermissionError as e:
            return f"[Guardian] {e}"
        safe = self._resolve_safe(rel_path)
        if not safe:
            return self._format_path_error(rel_path)
        if not target.exists():
            return f"[错误] 目录不存在: {rel_path}\n提示: 可用 list_dir('.') 查看可用目录。"
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
        """读写 .system/state.json。原子写入避免损坏。"""
        state = {}
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        state[key] = value
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)

    def get_state(self, key: str, default: Any = None) -> Any:
        if not self._state_path.exists():
            return default
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state.get(key, default)
        except (json.JSONDecodeError, OSError):
            return default

    # --- Turn 级别持久化 ---

    def save_turn(self, turn: dict) -> None:
        """追加一个 turn 到 turns.jsonl。"""
        with open(self._turns_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

    def save_partial_turn(self, messages: list[dict], iteration: int, interrupted: bool = True) -> None:
        """取消时保存已 committed 的部分 turn。"""
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
        with open(self._turns_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    turns.append(json.loads(line))
        return turns

    def replay_history(self) -> list[dict]:
        """从 turns.jsonl replay 成 flat messages 列表。"""
        messages: list[dict] = []
        for turn in self.load_turns():
            for msg in turn.get("messages", []):
                messages.append(msg)
        return messages

    def clear_turns(self) -> None:
        """清空 turns.jsonl。"""
        if self._turns_path.exists():
            self._turns_path.unlink()

    def rotate_turns(self) -> Path | None:
        """Rotate turns.jsonl 到备份文件，返回备份路径。"""
        if not self._turns_path.exists():
            return None
        # 找下一个可用的备份编号
        for i in range(1, 100):
            backup = self._turns_path.with_suffix(f".jsonl.{i}")
            if not backup.exists():
                self._turns_path.replace(backup)
                return backup
        return None

    def write_system_turn(self, prompt: str) -> None:
        """在 turns.jsonl 开头写入 system prompt 记录。

        如果文件已有内容，通过临时文件原子 prepend。
        """
        line = json.dumps({"role": "_system_prompt", "content": prompt}, ensure_ascii=False) + "\n"
        if not self._turns_path.exists() or self._turns_path.stat().st_size == 0:
            self._turns_path.write_text(line, encoding="utf-8")
            return
        tmp = self._turns_path.with_suffix(".tmp")
        tmp.write_text(line, encoding="utf-8")
        with tmp.open("a", encoding="utf-8") as tmp_f, self._turns_path.open(encoding="utf-8") as src_f:
            while True:
                chunk = src_f.read(64 * 1024)
                if not chunk:
                    break
                tmp_f.write(chunk)
        tmp.replace(self._turns_path)

    # --- IPC: events.jsonl ---

    def append_event(self, event: dict) -> None:
        event.setdefault("ts", datetime.now().isoformat())
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def poll_events(self, offset: int = 0) -> tuple[list[dict], int]:
        if not self._events_path.exists():
            return [], 0
        events: list[dict] = []
        with open(self._events_path, encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
        return events, new_offset

    def events_size(self) -> int:
        if not self._events_path.exists():
            return 0
        return self._events_path.stat().st_size

    def send_interrupt(self, source: str = "scheduler") -> None:
        self.append_event({"type": "interrupt", "from": source})

    def send_message(self, content: str, source: str) -> None:
        self.append_event({"type": "message", "from": source, "content": content})

    # --- 内部工具 ---

    def _resolve_safe(self, rel_path: str) -> Path | None:
        """语义检查：Agent 只能访问 input/、workspace/ 或 output/ 下的内容。"""
        target = self.guardian.resolve(rel_path)
        for base in (self.input_dir, self.workspace_dir, self.output_dir):
            try:
                target.relative_to(base)
                return target
            except ValueError:
                continue
        return None

    def _format_path_error(self, rel_path: str) -> str:
        return (
            f"[错误] 路径不允许: {rel_path}\n"
            f"提示: 你只能访问以下目录:\n"
            f"  - input/      ← 系统输入（大纲、计划、证据等）\n"
            f"  - workspace/  ← 工作区（草稿、笔记、中间产物）\n"
            f"  - output/     ← 最终产物\n"
            f"可用 list_dir('.') 查看目录结构。"
        )

    def _list_root_overview(self) -> str:
        """列出节点根目录下 Agent 可见的目录概览。"""
        lines = ["你的可见目录："]
        for name, desc, dir_path in [
            ("input", "系统输入（大纲、计划、证据、依赖产物）", self.input_dir),
            ("workspace", "工作区（可读写：草稿、笔记、中间产物）", self.workspace_dir),
            ("output", "最终产物", self.output_dir),
        ]:
            if dir_path.exists():
                file_count = sum(1 for _ in dir_path.rglob("*") if _.is_file())
                lines.append(f"  [目录] {name}/  ← {desc}（{file_count} 个文件）")
        return "\n".join(lines)

    @property
    def output_exists(self) -> bool:
        """output/draft.md 存在即表示节点完成（默认判定）。"""
        return (self.output_dir / "draft.md").exists()

    def is_done(self, done_file: str | None = None) -> bool:
        if done_file:
            return (self.output_dir / done_file).exists()
        return self.output_exists

    def is_failed(self) -> bool:
        return (self.system_dir / "error.log").exists()

    # --- 子 Agent 管理 ---

    def child_session(self, name: str) -> Session:
        child_dir = self.children_dir / name
        return Session(child_dir)
