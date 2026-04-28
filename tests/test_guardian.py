"""Tests for agenda.guardian — path boundary security."""

from pathlib import Path

import pytest

from agenda.guardian import Guardian


class TestResolve:
    """Guardian.resolve() converts paths to absolute resolved paths."""

    def test_relative_path_joined_to_root(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.resolve("file.txt") == tmp_path / "file.txt"

    def test_nested_relative_path(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.resolve("a/b/c.txt") == tmp_path / "a" / "b" / "c.txt"

    def test_absolute_path_unchanged(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        abs_path = "/tmp/some/file.txt"
        # resolve() keeps absolute paths absolute
        assert g.resolve(abs_path) == Path(abs_path).resolve()

    def test_dot_and_dotdot_normalized(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.resolve("a/../b.txt") == tmp_path / "b.txt"
        assert g.resolve("./c.txt") == tmp_path / "c.txt"

    def test_root_resolved_at_construction(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path / "a" / "..")
        assert g.root == tmp_path.resolve()


class TestIsAllowed:
    """Guardian.is_allowed() checks whether a path stays inside root."""

    def test_path_inside_root(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.is_allowed("file.txt") is True
        assert g.is_allowed("sub/dir/file.txt") is True

    def test_path_traversal_escape(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.is_allowed("../escape.txt") is False
        assert g.is_allowed("a/../../escape.txt") is False

    def test_absolute_path_outside_root(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.is_allowed("/etc/passwd") is False

    def test_symlink_escape(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        # Create a symlink pointing outside root
        link = tmp_path / "link"
        link.symlink_to("/tmp")
        # resolve() follows symlink -> /tmp which is outside tmp_path
        assert g.is_allowed("link/file.txt") is False

    def test_symlink_inside_root(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        assert g.is_allowed("link/file.txt") is True


class TestCheck:
    """Guardian.check() returns resolved path or raises PermissionError."""

    def test_allowed_returns_resolved_path(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        result = g.check("file.txt")
        assert result == tmp_path / "file.txt"

    def test_denied_raises_permission_error(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        with pytest.raises(PermissionError) as exc_info:
            g.check("../escape.txt")
        assert "[Guardian]" in str(exc_info.value)
        assert "denied" in str(exc_info.value)

    def test_custom_operation_in_message(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        with pytest.raises(PermissionError) as exc_info:
            g.check("../x", operation="read")
        assert "read to" in str(exc_info.value)

    def test_check_read(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.check_read("file.txt") == tmp_path / "file.txt"
        with pytest.raises(PermissionError) as exc_info:
            g.check_read("../x")
        assert "read to" in str(exc_info.value)

    def test_check_write(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert g.check_write("file.txt") == tmp_path / "file.txt"
        with pytest.raises(PermissionError) as exc_info:
            g.check_write("../x")
        assert "write to" in str(exc_info.value)


class TestRepr:
    def test_repr(self, tmp_path: Path) -> None:
        g = Guardian(tmp_path)
        assert "Guardian" in repr(g)
        assert str(tmp_path.resolve()) in repr(g)
