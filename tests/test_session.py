"""Tests for agenda.session — directory isolation, persistence, IPC."""

import json
from pathlib import Path

from agenda.session import Session

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_directories_created(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node1")
        assert s.node_dir.exists()
        assert s.input_dir.exists()
        assert s.workspace_dir.exists()
        assert s.output_dir.exists()
        assert s.system_dir.exists()
        assert s.children_dir.exists()

    def test_node_dir_resolved(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "a" / "..")
        assert s.node_dir == tmp_path.resolve()

    def test_guardian_root_matches_node_dir(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.guardian.root == s.node_dir


# ---------------------------------------------------------------------------
# Agent-visible file ops
# ---------------------------------------------------------------------------


class TestAgentFileOps:
    def test_write_file_to_output(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.write_file("output/draft.md", "hello")
        assert "成功" in result
        assert (s.output_dir / "draft.md").read_text() == "hello"

    def test_write_file_to_workspace(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.write_file("workspace/notes.txt", "notes")
        assert "成功" in result
        assert (s.workspace_dir / "notes.txt").read_text() == "notes"

    def test_write_file_rejects_input(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.write_file("input/evil.txt", "x")
        assert "只能写入 workspace/ 或 output/" in result

    def test_write_file_rejects_system(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.write_file(".system/state.json", "x")
        assert "只能写入 workspace/ 或 output/" in result

    def test_write_file_rejects_escape(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.write_file("../escape.txt", "x")
        assert "Guardian" in result

    def test_read_file_from_input(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        (s.input_dir / "data.txt").write_text("data")
        assert s.read_file("input/data.txt") == "data"

    def test_read_file_from_workspace(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        (s.workspace_dir / "draft.md").write_text("draft")
        assert s.read_file("workspace/draft.md") == "draft"

    def test_read_file_from_output(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        (s.output_dir / "result.json").write_text("{}")
        assert s.read_file("output/result.json") == "{}"

    def test_read_file_not_found(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.read_file("input/missing.txt")
        assert "文件不存在" in result

    def test_read_file_rejects_system(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.read_file(".system/state.json")
        assert "路径不允许" in result

    def test_list_dir_shows_contents(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        (s.input_dir / "a.txt").write_text("a")
        (s.input_dir / "b").mkdir()
        result = s.list_dir("input")
        assert "[文件] a.txt" in result
        assert "[目录] b" in result

    def test_list_dir_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.list_dir("input") == "(空)"

    def test_list_dir_root_overview(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.list_dir(".")
        assert "input/" in result
        assert "workspace/" in result
        assert "output/" in result

    def test_list_dir_not_found(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        result = s.list_dir("workspace/nope")
        assert "目录不存在" in result


# ---------------------------------------------------------------------------
# System-private ops
# ---------------------------------------------------------------------------


class TestSystemOps:
    def test_write_and_read_system(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.write_system("hints.md", "be helpful")
        assert s.read_system("hints.md") == "be helpful"

    def test_read_system_missing_returns_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.read_system("missing.txt") == ""

    def test_write_system_nested_path(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.write_system("deep/nested/file.txt", "content")
        assert (s.system_dir / "deep" / "nested" / "file.txt").read_text() == "content"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestState:
    def test_set_and_get(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.set_state("key1", "value1")
        assert s.get_state("key1") == "value1"

    def test_get_default(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.get_state("missing", "default") == "default"

    def test_get_missing_no_default(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.get_state("missing") is None

    def test_overwrite(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.set_state("k", 1)
        s.set_state("k", 2)
        assert s.get_state("k") == 2

    def test_multiple_keys(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.set_state("a", 1)
        s.set_state("b", 2)
        assert s.get_state("a") == 1
        assert s.get_state("b") == 2

    def test_corrupt_state_returns_default(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s._state_path.write_text("not json")
        assert s.get_state("k", "default") == "default"


# ---------------------------------------------------------------------------
# Turn persistence
# ---------------------------------------------------------------------------


class TestTurnPersistence:
    def test_save_and_load_turns(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_turn({"type": "turn", "iteration": 1})
        s.save_turn({"type": "turn", "iteration": 2})
        turns = s.load_turns()
        assert len(turns) == 2
        assert turns[0]["iteration"] == 1
        assert turns[1]["iteration"] == 2

    def test_load_turns_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.load_turns() == []

    def test_load_turns_skips_corrupt_lines(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s._turns_path.write_text('not json\n{"valid": true}\n')
        turns = s.load_turns()
        assert len(turns) == 1
        assert turns[0]["valid"] is True

    def test_replay_history(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_turn(
            {
                "type": "turn",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
            }
        )
        s.save_turn(
            {
                "type": "turn",
                "messages": [
                    {"role": "user", "content": "bye"},
                ],
            }
        )
        msgs = s.replay_history()
        assert len(msgs) == 3
        assert msgs[0]["content"] == "hi"
        assert msgs[1]["content"] == "hello"
        assert msgs[2]["content"] == "bye"

    def test_replay_history_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.replay_history() == []

    def test_save_partial_turn(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_partial_turn([{"role": "user", "content": "x"}], iteration=5, interrupted=True)
        turns = s.load_turns()
        assert len(turns) == 1
        assert turns[0]["interrupted"] is True
        assert turns[0]["iteration"] == 5

    def test_clear_turns(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_turn({"type": "turn"})
        s.clear_turns()
        assert s.load_turns() == []

    def test_rotate_turns(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_turn({"type": "turn"})
        backup = s.rotate_turns()
        assert backup is not None
        assert backup.exists()
        assert not s._turns_path.exists()

    def test_rotate_turns_no_file(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.rotate_turns() is None

    def test_write_system_turn_to_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.write_system_turn("system prompt")
        line = s._turns_path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["role"] == "_system_prompt"
        assert data["content"] == "system prompt"

    def test_write_system_turn_prepend(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.save_turn({"type": "turn", "iteration": 1})
        s.write_system_turn("system prompt")
        lines = s._turns_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["role"] == "_system_prompt"


# ---------------------------------------------------------------------------
# IPC events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_append_and_poll(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.append_event({"type": "completed"})
        s.append_event({"type": "message", "content": "hi"})
        events, offset = s.poll_events(offset=0)
        assert len(events) == 2
        assert events[0]["type"] == "completed"
        assert events[1]["type"] == "message"
        assert offset > 0

    def test_poll_with_offset(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.append_event({"type": "first"})
        _, offset1 = s.poll_events(offset=0)
        s.append_event({"type": "second"})
        events, offset2 = s.poll_events(offset=offset1)
        assert len(events) == 1
        assert events[0]["type"] == "second"

    def test_poll_empty(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        events, offset = s.poll_events()
        assert events == []
        assert offset == 0

    def test_poll_skips_corrupt_lines(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s._events_path.write_text('not json\n{"type": "ok"}\n')
        events, _ = s.poll_events(offset=0)
        assert len(events) == 1
        assert events[0]["type"] == "ok"

    def test_events_size(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.events_size() == 0
        s.append_event({"type": "x"})
        assert s.events_size() > 0

    def test_send_interrupt(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.send_interrupt(source="scheduler")
        events, _ = s.poll_events()
        assert events[0]["type"] == "interrupt"
        assert events[0]["from"] == "scheduler"
        assert "ts" in events[0]

    def test_send_message(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        s.send_message("hello", source="parent")
        events, _ = s.poll_events()
        assert events[0]["type"] == "message"
        assert events[0]["content"] == "hello"
        assert events[0]["from"] == "parent"


# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------


class TestStatusChecks:
    def test_output_exists(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.output_exists is False
        s.write_file("output/draft.md", "done")
        assert s.output_exists is True

    def test_is_done_default(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.is_done() is False
        s.write_file("output/draft.md", "done")
        assert s.is_done() is True

    def test_is_done_custom_file(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.is_done("report.md") is False
        s.write_file("output/report.md", "done")
        assert s.is_done("report.md") is True

    def test_is_failed(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "node")
        assert s.is_failed() is False
        s.write_system("error.log", "error")
        assert s.is_failed() is True


# ---------------------------------------------------------------------------
# Child session
# ---------------------------------------------------------------------------


class TestChildSession:
    def test_child_session_creates_directories(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "parent")
        child = s.child_session("analyzer")
        assert child.node_dir == s.children_dir / "analyzer"
        assert child.input_dir.exists()
        assert child.system_dir.exists()

    def test_child_is_independent(self, tmp_path: Path) -> None:
        s = Session(tmp_path / "parent")
        child = s.child_session("analyzer")
        child.write_file("output/draft.md", "child result")
        assert s.output_exists is False
        assert child.output_exists is True
