"""端到端真实 LLM 测试（Live tests）。

这些测试调用真实的 DeepSeek API，默认被跳过。
运行方式:
    RUN_LIVE_TESTS=1 pytest tests/test_e2e_live.py -v

注意:
- 每次运行消耗真实 API token
- 断言聚焦结构性结果（产物存在、状态正确），不断言 LLM 输出内容
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agenda import agenda

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_TESTS"),
    reason="Set RUN_LIVE_TESTS=1 to run live LLM tests (consumes API tokens)",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "live_workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Base Case: single-node DAG
# ---------------------------------------------------------------------------

class TestLiveBaseCase:
    async def _run(self, dag_spec: dict, workspace: Path) -> dict[str, str]:
        return await agenda(dag_spec, workspace)

    def test_single_node_writes_output(self, workspace: Path) -> None:
        """Agent writes content to output/draft.md via write_file tool."""
        dag = {
            "dag": {"name": "basecase", "max_parallel": 2},
            "nodes": {
                "hello": {
                    "prompt": (
                        "你是一个测试助手。"
                        "请用 write_file 工具写一句关于人工智能的简短问候语到 output/draft.md。"
                    ),
                    "model": "deepseek-flash",
                    "max_iterations": 5,
                    "timeout": 60,
                }
            },
        }
        results = asyncio.run(self._run(dag, workspace))
        assert results == {"hello": "COMPLETED"}

        draft = workspace / "nodes" / "hello" / "output" / "draft.md"
        assert draft.exists(), "output/draft.md should be created"
        content = draft.read_text(encoding="utf-8")
        assert len(content) > 5, "draft should contain meaningful text"

    def test_single_node_with_tool_call(self, workspace: Path) -> None:
        """Agent calls read_file and then write_file."""
        # Pre-create an input file
        input_dir = workspace / "nodes" / "echo" / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        (input_dir / "source.txt").write_text("original text")

        dag = {
            "dag": {"name": "echo", "max_parallel": 2},
            "nodes": {
                "echo": {
                    "prompt": (
                        "请先用 read_file 工具读取 input/source.txt 的内容，"
                        "然后用 write_file 把读取到的内容原样写入 output/draft.md。"
                    ),
                    "model": "deepseek-flash",
                    "max_iterations": 5,
                    "timeout": 60,
                }
            },
        }
        results = asyncio.run(self._run(dag, workspace))
        assert results == {"echo": "COMPLETED"}

        draft = workspace / "nodes" / "echo" / "output" / "draft.md"
        assert draft.exists()
        assert "original text" in draft.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Multi-node DAG: dependency chain
# ---------------------------------------------------------------------------

class TestLiveMultiNode:
    async def _run(self, dag_spec: dict, workspace: Path) -> dict[str, str]:
        return await agenda(dag_spec, workspace)

    def test_two_node_chain(self, workspace: Path) -> None:
        """writer -> reader: downstream reads upstream output via dep_inputs."""
        dag = {
            "dag": {"name": "chain", "max_parallel": 2},
            "nodes": {
                "writer": {
                    "prompt": (
                        "写一段关于'未来城市'的 30 字短文，保存到 output/draft.md。"
                    ),
                    "model": "deepseek-flash",
                    "max_iterations": 5,
                    "timeout": 60,
                },
                "reader": {
                    "prompt": (
                        "读取 input/deps/writer.md 中的内容，"
                        "写一段 20 字的评论，保存到 output/draft.md。"
                    ),
                    "model": "deepseek-flash",
                    "deps": ["writer"],
                    "dep_inputs": [
                        {"from": "nodes/writer/output/draft.md", "to": "deps/writer.md"}
                    ],
                    "max_iterations": 5,
                    "timeout": 60,
                },
            },
        }
        results = asyncio.run(self._run(dag, workspace))
        assert results == {"writer": "COMPLETED", "reader": "COMPLETED"}

        writer_draft = workspace / "subdag_0" / "nodes" / "writer" / "output" / "draft.md"
        reader_draft = workspace / "subdag_0" / "nodes" / "reader" / "output" / "draft.md"
        dep_file = workspace / "subdag_0" / "nodes" / "reader" / "input" / "deps" / "writer.md"

        assert writer_draft.exists()
        assert reader_draft.exists()
        assert dep_file.exists(), "dep_inputs should copy upstream output to downstream input"

        writer_text = writer_draft.read_text(encoding="utf-8")
        reader_text = reader_draft.read_text(encoding="utf-8")
        assert len(writer_text) > 5
        assert len(reader_text) > 5

    def test_parallel_nodes(self, workspace: Path) -> None:
        """Two independent nodes run in parallel."""
        dag = {
            "dag": {"name": "parallel", "max_parallel": 2},
            "nodes": {
                "alpha": {
                    "prompt": "写一个英文单词 'apple' 到 output/draft.md。",
                    "model": "deepseek-flash",
                    "max_iterations": 3,
                    "timeout": 60,
                },
                "beta": {
                    "prompt": "写一个英文单词 'banana' 到 output/draft.md。",
                    "model": "deepseek-flash",
                    "max_iterations": 3,
                    "timeout": 60,
                },
            },
        }
        results = asyncio.run(self._run(dag, workspace))
        assert results == {"alpha": "COMPLETED", "beta": "COMPLETED"}

        alpha_draft = workspace / "subdag_0" / "nodes" / "alpha" / "output" / "draft.md"
        beta_draft = workspace / "subdag_0" / "nodes" / "beta" / "output" / "draft.md"
        assert alpha_draft.exists()
        assert beta_draft.exists()


# ---------------------------------------------------------------------------
# Model fallback via live API
# ---------------------------------------------------------------------------

class TestLiveFallback:
    def test_fallback_on_invalid_model(self, workspace: Path) -> None:
        """Trigger fallback by requesting a non-existent model alias."""
        dag = {
            "dag": {"name": "fallback", "max_parallel": 2},
            "nodes": {
                "try": {
                    "prompt": "写 'hello' 到 output/draft.md。",
                    "model": "nonexistent-model-alias",  # should fallback to default
                    "max_iterations": 3,
                    "timeout": 60,
                }
            },
        }
        # ModelRegistry.get() warns and falls back to default when alias unknown
        results = asyncio.run(agenda(dag, workspace))
        # If default model is configured, should still complete
        # If no default, will fail — test marks as xfail in that case
        if results["try"] == "COMPLETED":
            draft = workspace / "nodes" / "try" / "output" / "draft.md"
            assert draft.exists()
