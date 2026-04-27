from __future__ import annotations

"""命令行入口（给 Agent 用的 CLI）。默认 JSON 输出。"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from .const import (
    EXIT_SUCCESS,
    EXIT_ARGS_ERROR,
    EXIT_DAG_CONFIG_ERROR,
    EXIT_EXECUTION_ERROR,
    EXIT_DEPENDENCY_ERROR,
)
from .models import ModelRegistry
from .session import Session
from .scheduler import DAGScheduler
from .tools import build_tools


def _resolve_dag_path(dag_arg: str | None) -> Path:
    dag_path = dag_arg or os.environ.get("AGENDA_DAG")
    if not dag_path:
        _json_out({"error": "未指定 DAG 路径。提供路径或设置 AGENDA_DAG"})
        sys.exit(EXIT_ARGS_ERROR)
    return Path(dag_path).expanduser().resolve()


def _resolve_models_path(models_arg: str | None) -> Path | None:
    models_path = models_arg or os.environ.get("AGENDA_MODELS")
    if models_path:
        return Path(models_path).expanduser().resolve()
    return None


def _load_scheduler(dag_path: Path, models_path: Path | None = None) -> DAGScheduler:
    if dag_path.is_file():
        dag_dir, dag_file = dag_path.parent, dag_path
    else:
        dag_dir, dag_file = dag_path, dag_path / "dag.yaml"
    scheduler = DAGScheduler(dag_dir.parent, dag_dir.name)
    scheduler.dag_file = dag_file
    scheduler.load()
    if models_path:
        scheduler.model_registry = ModelRegistry().load(
            models_path.parent if models_path.name == "models.yaml" else None
        )
    return scheduler


def _json_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now().isoformat()

def cli() -> int:
    parser = argparse.ArgumentParser(
        description="Agenda — 给 Agent 调度 Agent 的极简运行时 v0.0.6",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.0.6")
    subparsers = parser.add_subparsers(dest="cmd", help="命令")

    dag_parser = subparsers.add_parser("dag", help="DAG 管理")
    dag_sub = dag_parser.add_subparsers(dest="dag_cmd")

    dag_create = dag_sub.add_parser("create", help="JSON -> YAML")
    dag_create.add_argument("--from-json", required=True)
    dag_create.add_argument("-o", "--output", required=True)

    dag_validate = dag_sub.add_parser("validate", help="验证 DAG")
    dag_validate.add_argument("path", nargs="?")

    dag_run = dag_sub.add_parser("run", help="运行 DAG")
    dag_run.add_argument("path", nargs="?")
    dag_run.add_argument("--models")
    dag_run.add_argument("--max-parallel", type=int)
    dag_run.add_argument("--dry-run", action="store_true")

    dag_status = dag_sub.add_parser("status", help="DAG 状态")
    dag_status.add_argument("path", nargs="?")
    dag_status.add_argument("--watch", action="store_true")

    node_parser = subparsers.add_parser("node", help="节点管理")
    node_sub = node_parser.add_subparsers(dest="node_cmd")

    node_run = node_sub.add_parser("run", help="运行单个节点")
    node_run.add_argument("path", nargs="?")
    node_run.add_argument("--node", required=True)
    node_run.add_argument("--models")
    node_run.add_argument("--force", action="store_true")

    node_reset = node_sub.add_parser("reset", help="重置节点")
    node_reset.add_argument("path", nargs="?")
    node_reset.add_argument("--node", required=True)

    node_history = node_sub.add_parser("history", help="节点对话历史")
    node_history.add_argument("path", nargs="?")
    node_history.add_argument("--node", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Daemon")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_cmd")

    daemon_start = daemon_sub.add_parser("start")
    daemon_start.add_argument("path", nargs="?")
    daemon_start.add_argument("--foreground", action="store_true")

    daemon_stop = daemon_sub.add_parser("stop")
    daemon_stop.add_argument("path", nargs="?")
    daemon_status = daemon_sub.add_parser("status")
    daemon_status.add_argument("path", nargs="?")

    models_parser = subparsers.add_parser("models", help="模型管理")
    models_sub = models_parser.add_subparsers(dest="models_cmd")
    models_sub.add_parser("list")
    models_sub.add_parser("validate")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return EXIT_SUCCESS

    if args.cmd == "dag":
        if not args.dag_cmd:
            dag_parser.print_help()
            return EXIT_SUCCESS

        if args.dag_cmd == "create":
            import yaml
            src = args.from_json
            data = json.loads(sys.stdin.read() if src == "-" else Path(src).read_text("utf-8"))
            if "nodes" not in data:
                _json_out({"error": "JSON 缺少 nodes 字段"})
                return EXIT_ARGS_ERROR
            data.setdefault("dag", {"name": "untitled", "max_parallel": 4})
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
            _json_out({"created": str(out)})
            return EXIT_SUCCESS

        if args.dag_cmd == "validate":
            try:
                scheduler = _load_scheduler(_resolve_dag_path(args.path))
                nodes = scheduler.dag.get("nodes", {})
                cycle = scheduler._detect_cycle()
                warnings = []
                for nid, cfg in nodes.items():
                    for inp in cfg.get("inputs", []):
                        if not (scheduler.dag_dir / inp.split("#")[0].lstrip("/")).exists():
                            warnings.append(f"节点 {nid} 输入文件不存在: {inp}")
                result = {
                    "valid": cycle is None and len(warnings) == 0,
                    "path": str(scheduler.dag_file),
                    "nodes": len(nodes),
                    "models": sorted({c.get("model") for c in nodes.values() if c.get("model")}),
                    "warnings": warnings,
                }
                if cycle:
                    result["cycle_error"] = "循环依赖: " + " -> ".join(cycle) + " -> " + cycle[0]
                    result["valid"] = False
                _json_out(result)
                return EXIT_SUCCESS if result["valid"] else EXIT_DAG_CONFIG_ERROR
            except Exception as e:
                _json_out({"valid": False, "error": str(e)})
                return EXIT_DAG_CONFIG_ERROR

        if args.dag_cmd == "run":
            dag_path = _resolve_dag_path(args.path)
            models_path = _resolve_models_path(args.models)
            max_parallel = args.max_parallel or int(os.environ.get("AGENDA_MAX_PARALLEL", "4"))

            if args.dry_run:
                scheduler = _load_scheduler(dag_path, models_path)
                _json_out({
                    "dry_run": True,
                    "dag": str(dag_path),
                    "max_parallel": max_parallel,
                    "topo": scheduler.topological_sort(),
                    "nodes": {
                        n: {"model": c.get("model", "default"), "deps": c.get("deps", [])}
                        for n, c in scheduler.dag.get("nodes", {}).items()
                    },
                })
                return EXIT_SUCCESS

            try:
                scheduler = _load_scheduler(dag_path, models_path)
                scheduler.dag["dag"]["max_parallel"] = max_parallel

                # 统一走 agenda() 顶层入口，替代直接 Scheduler.run()
                # Base Case: 单节点自动退化 AgentLoop.run()
                # Recursive Step: 多节点走 Scheduler.run()
                from . import agenda as _agenda

                results = asyncio.run(_agenda(
                    dag_spec=scheduler.dag,
                    workspace=scheduler.dag_dir,
                    model_registry=scheduler.model_registry,
                    tools_factory=lambda session: build_tools(session),
                ))
                _json_out({"results": results})
                failed = [n for n, s in results.items() if s == "FAILED"]
                return EXIT_DEPENDENCY_ERROR if failed else EXIT_SUCCESS
            except KeyboardInterrupt:
                _json_out({"interrupted": True})
                return 130
            except Exception as e:
                _json_out({"error": str(e)})
                return EXIT_EXECUTION_ERROR

        if args.dag_cmd == "status":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            nodes = scheduler.dag.get("nodes", {})

            if args.watch:
                try:
                    while True:
                        completed = [n for n in nodes if scheduler.node_is_done(n)]
                        failed = [n for n in nodes if scheduler.node_is_failed(n)]
                        running = [n for n in nodes if scheduler.node_is_running(n)]
                        pending = [n for n in nodes if n not in completed and n not in failed and n not in running]
                        _json_out({
                            "ts": _now_iso(),
                            "dag": str(dag_path),
                            "completed": completed,
                            "failed": failed,
                            "running": running,
                            "pending": pending,
                        })
                        if len(completed) + len(failed) == len(nodes):
                            break
                        time.sleep(1)
                    return EXIT_SUCCESS
                except KeyboardInterrupt:
                    return 130

            completed = [n for n in nodes if scheduler.node_is_done(n)]
            failed = [n for n in nodes if scheduler.node_is_failed(n)]
            running = [n for n in nodes if scheduler.node_is_running(n)]
            pending = [n for n in nodes if n not in completed and n not in failed and n not in running]
            _json_out({
                "dag": scheduler.dag.get("dag", {}).get("name", "untitled"),
                "path": str(dag_path),
                "completed": completed,
                "failed": failed,
                "running": running,
                "pending": pending,
            })
            return EXIT_SUCCESS

    if args.cmd == "node":
        if not args.node_cmd:
            node_parser.print_help()
            return EXIT_SUCCESS

        dag_path = _resolve_dag_path(args.path)
        scheduler = _load_scheduler(dag_path)
        node_id = args.node

        if args.node_cmd == "run":
            models_path = _resolve_models_path(args.models)
            if args.force:
                node_dir = scheduler.nodes_dir / node_id
                if node_dir.exists():
                    shutil.rmtree(node_dir)
                    _json_out({"reset": node_id})

            try:
                asyncio.run(scheduler._run_node(
                    node_id,
                    tools_factory=lambda session: build_tools(session),
                ))
                return EXIT_SUCCESS
            except Exception as e:
                _json_out({"error": str(e)})
                return EXIT_EXECUTION_ERROR

        if args.node_cmd == "reset":
            node_dir = scheduler.nodes_dir / node_id
            if node_dir.exists():
                shutil.rmtree(node_dir)
                _json_out({"reset": node_id})
            else:
                _json_out({"reset": node_id, "note": "节点不存在"})
            return EXIT_SUCCESS

        if args.node_cmd == "history":
            session = Session(scheduler.nodes_dir / node_id)
            turns = session.load_turns()
            _json_out({"node": node_id, "turns": turns})
            return EXIT_SUCCESS

    if args.cmd == "daemon":
        if not args.daemon_cmd:
            daemon_parser.print_help()
            return EXIT_SUCCESS

        from .daemon import _start_foreground, _start_daemon, _cmd_stop, _cmd_status

        dag_path = _resolve_dag_path(args.path) if hasattr(args, "path") and args.path else Path(os.environ.get("AGENDA_DAG", "."))
        if dag_path.is_file():
            dag_dir = dag_path.parent
        else:
            dag_dir = dag_path

        if args.daemon_cmd == "start":
            if args.foreground:
                return _start_foreground(dag_dir, dag_dir / "dag.yaml")
            return _start_daemon(dag_dir, dag_dir / "dag.yaml")

        if args.daemon_cmd == "stop":
            return _cmd_stop(dag_dir)

        if args.daemon_cmd == "status":
            return _cmd_status(dag_dir)

    if args.cmd == "models":
        if not args.models_cmd:
            models_parser.print_help()
            return EXIT_SUCCESS

        models_path = _resolve_models_path(args.config if hasattr(args, "config") else None)
        registry = ModelRegistry()
        if models_path and models_path.exists():
            registry.load(models_path.parent)
        else:
            registry.load()

        if args.models_cmd == "list":
            result = []
            for name, cfg in registry._models.items():
                result.append({
                    "name": name,
                    "model": cfg.model,
                    "base_url": cfg.base_url,
                    "token_cap": cfg.token_cap,
                })
            _json_out({"models": result})
            return EXIT_SUCCESS

        if args.models_cmd == "validate":
            result = []
            for name, cfg in registry._models.items():
                result.append({
                    "name": name,
                    "valid": bool(cfg.api_key),
                    "model": cfg.model,
                    "base_url": cfg.base_url,
                })
            _json_out({"models": result})
            return EXIT_SUCCESS

    _json_out({"error": f"未知命令: {args.cmd}"})
    return EXIT_ARGS_ERROR
