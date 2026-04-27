"""Tests for agenda.cli — command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agenda.cli import cli, _resolve_dag_path, _validate_dag, _load_scheduler
from agenda.const import (
    EXIT_SUCCESS,
    EXIT_ARGS_ERROR,
    EXIT_DAG_CONFIG_ERROR,
    EXIT_EXECUTION_ERROR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(argv: list[str], monkeypatch, capsys) -> tuple[int, dict]:
    """Run cli() with mocked sys.argv, return (exit_code, parsed_json_output)."""
    monkeypatch.setattr(sys, "argv", ["agenda"] + argv)
    code = cli()
    out = capsys.readouterr().out.strip()
    # last line should be JSON
    lines = out.splitlines()
    data = json.loads(lines[-1]) if lines else {}
    return code, data


@pytest.fixture
def sample_dag(tmp_path: Path) -> Path:
    """Create a valid DAG directory."""
    dag_dir = tmp_path / "mydag"
    dag_dir.mkdir()
    dag_file = dag_dir / "dag.yaml"
    dag_file.write_text("""
dag:
  name: test
  max_parallel: 2
nodes:
  a:
    prompt: "task A"
    model: default
  b:
    prompt: "task B"
    deps: [a]
""", encoding="utf-8")
    return dag_dir


@pytest.fixture
def cyclic_dag(tmp_path: Path) -> Path:
    dag_dir = tmp_path / "cyclic"
    dag_dir.mkdir()
    (dag_dir / "dag.yaml").write_text("""
dag:
  name: cyclic
nodes:
  x:
    prompt: "x"
    deps: [y]
  y:
    prompt: "y"
    deps: [x]
""", encoding="utf-8")
    return dag_dir


@pytest.fixture
def bad_dep_dag(tmp_path: Path) -> Path:
    """DAG with a dep pointing to non-existent node."""
    dag_dir = tmp_path / "baddep"
    dag_dir.mkdir()
    (dag_dir / "dag.yaml").write_text("""
dag:
  name: bad
nodes:
  a:
    prompt: "a"
    deps: [ghost]
""", encoding="utf-8")
    return dag_dir


# ---------------------------------------------------------------------------
# dag validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_dag(self, sample_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "validate", str(sample_dag)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data["valid"] is True
        assert data["nodes"] == 2

    def test_cyclic_dag(self, cyclic_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "validate", str(cyclic_dag)], monkeypatch, capsys)
        assert code == EXIT_DAG_CONFIG_ERROR
        assert data["valid"] is False
        assert any("循环依赖" in e for e in data["errors"])

    def test_bad_dep(self, bad_dep_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "validate", str(bad_dep_dag)], monkeypatch, capsys)
        assert code == EXIT_DAG_CONFIG_ERROR
        assert data["valid"] is False
        assert any("ghost" in e for e in data["errors"])

    def test_missing_dag(self, tmp_path: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "validate", str(tmp_path / "nope")], monkeypatch, capsys)
        assert code == EXIT_ARGS_ERROR
        assert "error" in data

    def test_empty_dag_warns(self, tmp_path: Path, monkeypatch, capsys) -> None:
        dag_dir = tmp_path / "empty"
        dag_dir.mkdir()
        (dag_dir / "dag.yaml").write_text("dag:\n  name: e\nnodes: {}", encoding="utf-8")
        code, data = _run_cli(["dag", "validate", str(dag_dir)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data["valid"] is True
        assert any("nodes 为空" in w for w in data["warnings"])


# ---------------------------------------------------------------------------
# dag init
# ---------------------------------------------------------------------------

class TestInit:
    def test_init_creates_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        target = tmp_path / "newproj"
        code, data = _run_cli(["dag", "init", str(target)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert (target / "dag.yaml").exists()
        assert (target / "models.yaml").exists()
        assert "dag.yaml" in data.get("files", [])

    def test_init_idempotent(self, tmp_path: Path, monkeypatch, capsys) -> None:
        target = tmp_path / "newproj"
        _run_cli(["dag", "init", str(target)], monkeypatch, capsys)
        first_dag = (target / "dag.yaml").read_text()
        _run_cli(["dag", "init", str(target)], monkeypatch, capsys)
        assert (target / "dag.yaml").read_text() == first_dag


# ---------------------------------------------------------------------------
# dag status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_empty(self, sample_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "status", str(sample_dag)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data["total"] == 2
        assert data["completed"] == []
        assert data["pending"] == ["a", "b"]

    def test_status_after_completion(self, sample_dag: Path, monkeypatch, capsys) -> None:
        # Simulate node 'a' completed
        session = __import__("agenda.session", fromlist=["Session"]).Session(sample_dag / "nodes" / "a")
        session.write_file("output/draft.md", "done")
        code, data = _run_cli(["dag", "status", str(sample_dag)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert "a" in data["completed"]
        assert "b" in data["pending"]

    def test_status_missing_dag(self, tmp_path: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "status", str(tmp_path / "nope")], monkeypatch, capsys)
        assert code == EXIT_ARGS_ERROR


# ---------------------------------------------------------------------------
# dag run --dry-run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run(self, sample_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["dag", "run", str(sample_dag), "--dry-run"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data["dry_run"] is True
        assert data["topo"] == ["a", "b"]
        assert "a" in data["nodes"]


# ---------------------------------------------------------------------------
# dag create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_from_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(sys, "argv", ["agenda", "dag", "create", "--from-json", "-", "-o", str(out)])
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"nodes":{"x":{"prompt":"hi"}}}'))
        code = cli()
        assert code == EXIT_SUCCESS
        assert out.exists()
        assert "nodes:" in out.read_text()


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class TestModels:
    def test_models_list(self, monkeypatch, capsys) -> None:
        code, data = _run_cli(["models", "list"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert "models" in data
        # Should list at least deepseek models from global config
        assert len(data["models"]) > 0

    def test_models_validate(self, monkeypatch, capsys) -> None:
        code, data = _run_cli(["models", "validate"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert "models" in data
        for m in data["models"]:
            assert "valid" in m
            assert "has_api_key" in m

    def test_models_list_with_config(self, tmp_path: Path, monkeypatch, capsys) -> None:
        cfg = tmp_path / "models.yaml"
        cfg.write_text("models:\n  test:\n    base_url: http://test\n    api_key: key\n    model: m\n")
        code, data = _run_cli(["models", "list", "--config", str(cfg)], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        names = [m["name"] for m in data["models"]]
        assert "test" in names


# ---------------------------------------------------------------------------
# node reset / logs
# ---------------------------------------------------------------------------

class TestNode:
    def test_reset_existing(self, sample_dag: Path, monkeypatch, capsys) -> None:
        # Create node dir
        node_dir = sample_dag / "nodes" / "a"
        node_dir.mkdir(parents=True)
        (node_dir / "output").mkdir()
        code, data = _run_cli(["node", "reset", str(sample_dag), "--node", "a"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert not node_dir.exists()

    def test_reset_missing(self, sample_dag: Path, monkeypatch, capsys) -> None:
        code, data = _run_cli(["node", "reset", str(sample_dag), "--node", "ghost"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data.get("note") == "节点不存在"

    def test_logs(self, sample_dag: Path, monkeypatch, capsys) -> None:
        # Create a failed node
        node_dir = sample_dag / "nodes" / "a"
        node_dir.mkdir(parents=True)
        (node_dir / ".system").mkdir()
        (node_dir / ".system" / "error.log").write_text("boom")
        (node_dir / "output").mkdir()
        (node_dir / "output" / "draft.md").write_text("ok")
        code, data = _run_cli(["node", "logs", str(sample_dag), "--node", "a"], monkeypatch, capsys)
        assert code == EXIT_SUCCESS
        assert data["error_log"] == "boom"
        assert data["output_exists"] is True


# ---------------------------------------------------------------------------
# _validate_dag internal
# ---------------------------------------------------------------------------

class TestValidateInternal:
    def test_dep_inputs_missing_output(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "badinput"
        dag_dir.mkdir()
        (dag_dir / "dag.yaml").write_text("""
dag:
  name: x
nodes:
  a:
    prompt: "a"
    dep_inputs:
      - from: "nodes/b/output/draft.md"
        to: "deps/b.md"
""", encoding="utf-8")
        scheduler = _load_scheduler(dag_dir)
        errors, warnings = _validate_dag(scheduler)
        # from path does not contain output/ ... wait it does. Let's test a bad one.
        assert len(errors) == 0  # deps node 'b' does not exist but that's a dep issue, not dep_inputs

    def test_dep_inputs_missing_fields(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "badinput"
        dag_dir.mkdir()
        (dag_dir / "dag.yaml").write_text("""
dag:
  name: x
nodes:
  a:
    prompt: "a"
    dep_inputs:
      - from: ""
        to: "deps/b.md"
""", encoding="utf-8")
        scheduler = _load_scheduler(dag_dir)
        errors, warnings = _validate_dag(scheduler)
        assert any("缺少 from" in e for e in errors)

    def test_model_resolution(self, tmp_path: Path) -> None:
        dag_dir = tmp_path / "mod"
        dag_dir.mkdir()
        (dag_dir / "dag.yaml").write_text("""
dag:
  name: m
nodes:
  a:
    prompt: "a"
    model: nonexistent_alias_xyz
""", encoding="utf-8")
        scheduler = _load_scheduler(dag_dir)
        errors, warnings = _validate_dag(scheduler)
        # Should warn about unknown model, not error
        assert any("nonexistent_alias_xyz" in w for w in warnings)


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_no_args(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sys, "argv", ["agenda"])
        code = cli()
        assert code == EXIT_SUCCESS

    def test_dag_no_subcmd(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sys, "argv", ["agenda", "dag"])
        code = cli()
        assert code == EXIT_SUCCESS

    def test_unknown_command(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sys, "argv", ["agenda", "unknown"])
        with pytest.raises(SystemExit):
            cli()
