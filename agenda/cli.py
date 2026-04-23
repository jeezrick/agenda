from __future__ import annotations

"""命令行入口（给 Agent 用的 CLI）。"""

import argparse
import asyncio
import json
import os
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
    """解析 DAG 路径。支持环境变量 AGENDA_DAG 兜底。"""
    dag_path = dag_arg or os.environ.get("AGENDA_DAG")
    if not dag_path:
        print("[错误] 未指定 DAG 路径。请提供路径或设置 AGENDA_DAG 环境变量。")
        sys.exit(EXIT_ARGS_ERROR)
    return Path(dag_path).expanduser().resolve()


def _resolve_models_path(models_arg: str | None) -> Path | None:
    """解析模型配置路径。支持环境变量 AGENDA_MODELS 兜底。"""
    models_path = models_arg or os.environ.get("AGENDA_MODELS")
    if models_path:
        return Path(models_path).expanduser().resolve()
    return None


def _load_scheduler(dag_path: Path, models_path: Path | None = None) -> DAGScheduler:
    """加载 DAG 调度器。"""
    if dag_path.is_file():
        dag_dir = dag_path.parent
        dag_file = dag_path
    else:
        dag_dir = dag_path
        dag_file = dag_path / "dag.yaml"

    dag_name = dag_dir.name
    scheduler = DAGScheduler(dag_dir.parent, dag_name)
    scheduler.dag_file = dag_file
    scheduler.load()

    if models_path:
        scheduler.model_registry = ModelRegistry().load(
            models_path.parent if models_path.name == "models.yaml" else None
        )
    return scheduler


def _json_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _ndjson_out(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now().isoformat()


def cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Agenda — 给 Agent 调度 Agent 的极简运行时 v0.0.4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  AGENDA_DAG          默认 DAG 路径
  AGENDA_MODELS       默认模型配置路径
  AGENDA_MAX_PARALLEL 默认最大并行度

退出码:
  0  成功
  1  参数/命令错误
  2  DAG 配置错误
  3  节点执行失败
  4  依赖失败导致无法继续
  130 用户中断 (Ctrl+C)
        """,
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.0.4")

    subparsers = parser.add_subparsers(dest="cmd", help="命令")

    # ============================================================
    # dag 命令组
    # ============================================================
    dag_parser = subparsers.add_parser("dag", help="DAG 管理")
    dag_sub = dag_parser.add_subparsers(dest="dag_cmd", help="DAG 子命令")

    # dag init
    dag_init = dag_sub.add_parser("init", help="初始化 DAG 工作区")
    dag_init.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_init.add_argument("--from-template", help="从模板初始化")

    # dag validate
    dag_validate = dag_sub.add_parser("validate", help="验证 DAG 配置")
    dag_validate.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_validate.add_argument("--json", action="store_true", help="JSON 输出")

    # dag inspect
    dag_inspect = dag_sub.add_parser("inspect", help="查看 DAG 拓扑结构")
    dag_inspect.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_inspect.add_argument("--json", action="store_true", help="JSON 输出")

    # dag run
    dag_run = dag_sub.add_parser("run", help="运行 DAG")
    dag_run.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_run.add_argument("--models", help="模型配置文件路径（默认 AGENDA_MODELS）")
    dag_run.add_argument("--max-parallel", type=int, help="最大并行度")
    dag_run.add_argument("--dry-run", action="store_true", help="预演模式（不实际执行）")

    # dag status
    dag_status = dag_sub.add_parser("status", help="查看 DAG 运行状态")
    dag_status.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    dag_status.add_argument("--json", action="store_true", help="JSON 输出")
    dag_status.add_argument("--watch", action="store_true", help="实时监听状态变化")

    # dag stop
    dag_stop = dag_sub.add_parser("stop", help="停止正在运行的 DAG")
    dag_stop.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")

    # ============================================================
    # node 命令组
    # ============================================================
    node_parser = subparsers.add_parser("node", help="节点管理")
    node_sub = node_parser.add_subparsers(dest="node_cmd", help="节点子命令")

    # node run
    node_run = node_sub.add_parser("run", help="运行单个节点")
    node_run.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_run.add_argument("--node", required=True, help="节点 ID")
    node_run.add_argument("--models", help="模型配置文件路径")
    node_run.add_argument("--force", action="store_true", help="强制重新运行（重置后再跑）")

    # node reset
    node_reset = node_sub.add_parser("reset", help="重置节点")
    node_reset.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_reset.add_argument("--node", required=True, help="节点 ID")

    # node logs
    node_logs = node_sub.add_parser("logs", help="查看节点日志")
    node_logs.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_logs.add_argument("--node", required=True, help="节点 ID")
    node_logs.add_argument("--tail", type=int, default=50, help="显示最后 N 行")

    # node history
    node_history = node_sub.add_parser("history", help="查看节点对话历史")
    node_history.add_argument("path", nargs="?", help="DAG 文件路径（默认 AGENDA_DAG）")
    node_history.add_argument("--node", required=True, help="节点 ID")
    node_history.add_argument("--json", action="store_true", help="JSON 输出")

    # ============================================================
    # models 命令组
    # ============================================================
    models_parser = subparsers.add_parser("models", help="模型管理")
    models_sub = models_parser.add_subparsers(dest="models_cmd", help="模型子命令")

    # models list
    models_list = models_sub.add_parser("list", help="列出可用模型")
    models_list.add_argument("--config", help="模型配置文件路径（默认 AGENDA_MODELS）")
    models_list.add_argument("--json", action="store_true", help="JSON 输出")

    # models validate
    models_validate = models_sub.add_parser("validate", help="验证模型配置")
    models_validate.add_argument("--config", help="模型配置文件路径（默认 AGENDA_MODELS）")

    # ============================================================
    # 解析命令
    # ============================================================
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return EXIT_SUCCESS

    # ============================================================
    # dag 子命令处理
    # ============================================================
    if args.cmd == "dag":
        if not args.dag_cmd:
            dag_parser.print_help()
            return EXIT_SUCCESS

        # dag init
        if args.dag_cmd == "init":
            dag_path = _resolve_dag_path(args.path) if args.path else Path(os.environ.get("AGENDA_DAG", "./dag.yaml"))
            dag_dir = dag_path.parent
            dag_dir.mkdir(parents=True, exist_ok=True)
            (dag_dir / "nodes").mkdir(exist_ok=True)

            if not dag_path.exists():
                dag_path.write_text(
                    "dag:\n  name: untitled\n  max_parallel: 4\nnodes:\n",
                    encoding="utf-8",
                )
            print(f"[dag init] 已初始化 DAG: {dag_path}")
            return EXIT_SUCCESS

        # dag validate
        if args.dag_cmd == "validate":
            dag_path = _resolve_dag_path(args.path)
            try:
                scheduler = _load_scheduler(dag_path)
                nodes = scheduler.dag.get("nodes", {})

                # 环检测
                cycle = scheduler._detect_cycle()
                cycle_error = None
                if cycle:
                    cycle_error = f"检测到循环依赖: {' -> '.join(cycle)} -> {cycle[0]}"

                # 检查模型配置
                models_used = set()
                for node_id, config in nodes.items():
                    model = config.get("model")
                    if model:
                        models_used.add(model)

                # 检查输入文件是否存在
                warnings = []
                for node_id, config in nodes.items():
                    for inp in config.get("inputs", []):
                        src = scheduler.dag_dir / inp.split("#")[0].lstrip("/")
                        if not src.exists():
                            warnings.append(f"节点 {node_id} 的输入文件不存在: {inp}")

                result = {
                    "valid": cycle is None and len(warnings) == 0,
                    "path": str(dag_path),
                    "nodes": len(nodes),
                    "models": sorted(models_used),
                    "warnings": warnings,
                }
                if cycle_error:
                    result["cycle_error"] = cycle_error
                    result["valid"] = False

                if args.json:
                    _json_out(result)
                else:
                    print(f"DAG: {dag_path}")
                    print(f"  节点数: {result['nodes']}")
                    print(f"  使用模型: {', '.join(result['models']) or 'default'}")
                    if cycle_error:
                        print(f"  ❌ {cycle_error}")
                    print(f"  警告: {len(warnings)}")
                    for w in warnings:
                        print(f"    ⚠️ {w}")
                    print(f"  验证结果: {'✅ 通过' if result['valid'] else '❌ 失败'}")

                return EXIT_SUCCESS if result["valid"] else EXIT_DAG_CONFIG_ERROR

            except Exception as e:
                if args.json:
                    _json_out({"valid": False, "error": str(e)})
                else:
                    print(f"[错误] 验证失败: {e}")
                return EXIT_DAG_CONFIG_ERROR

        # dag inspect
        if args.dag_cmd == "inspect":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            nodes = scheduler.dag.get("nodes", {})

            # 拓扑排序
            topo = scheduler.topological_sort()

            # 计算拓扑深度
            depth = {n: 0 for n in nodes}
            changed = True
            while changed:
                changed = False
                for n, cfg in nodes.items():
                    for dep in cfg.get("deps", []):
                        if dep in depth and depth[dep] + 1 > depth[n]:
                            depth[n] = depth[dep] + 1
                            changed = True

            # 关键路径（最长路径）
            critical_path = []
            if topo:
                # 找最长路径的简单方法：从深度最大的节点回溯
                max_node = max(depth, key=depth.get)
                critical_path = [max_node]
                while True:
                    current = critical_path[-1]
                    deps = nodes.get(current, {}).get("deps", [])
                    if not deps:
                        break
                    # 找深度最大的依赖
                    next_node = max((d for d in deps if d in depth), key=lambda d: depth[d], default=None)
                    if next_node is None:
                        break
                    critical_path.append(next_node)
                critical_path.reverse()

            result = {
                "path": str(dag_path),
                "topological_order": topo,
                "nodes": {
                    n: {
                        "deps": cfg.get("deps", []),
                        "model": cfg.get("model", "default"),
                        "depth": depth.get(n, 0),
                    }
                    for n, cfg in nodes.items()
                },
                "max_depth": max(depth.values()) if depth else 0,
                "critical_path": critical_path,
            }

            if args.json:
                _json_out(result)
            else:
                print(f"DAG: {dag_path}")
                print(f"  总节点: {len(nodes)}")
                print(f"  拓扑排序: {' -> '.join(topo)}")
                print(f"  最大深度: {result['max_depth']}")
                print(f"  关键路径: {' -> '.join(critical_path) or 'N/A'}")
                print(f"  节点列表:")
                for n in topo:
                    info = result["nodes"][n]
                    deps = f" 依赖: {', '.join(info['deps'])}" if info["deps"] else ""
                    print(f"    [{info['depth']}] {n} (模型: {info['model']}){deps}")

            return EXIT_SUCCESS

        # dag run
        if args.dag_cmd == "run":
            dag_path = _resolve_dag_path(args.path)
            models_path = _resolve_models_path(args.models)
            max_parallel = args.max_parallel or int(os.environ.get("AGENDA_MAX_PARALLEL", "4"))

            if args.dry_run:
                scheduler = _load_scheduler(dag_path, models_path)
                print(f"[dry-run] DAG: {dag_path}")
                print(f"[dry-run] 模型: {models_path or 'default'}")
                print(f"[dry-run] 最大并行: {max_parallel}")
                topo = scheduler.topological_sort()
                print(f"[dry-run] 拓扑顺序: {' -> '.join(topo)}")
                cycle = scheduler._detect_cycle()
                if cycle:
                    print(f"[dry-run] ⚠️ 检测到环: {' -> '.join(cycle)}")
                print(f"[dry-run] 节点:")
                for n, cfg in scheduler.dag.get("nodes", {}).items():
                    print(f"  {n}: model={cfg.get('model', 'default')}, deps={cfg.get('deps', [])}")
                return EXIT_SUCCESS

            try:
                scheduler = _load_scheduler(dag_path, models_path)
                scheduler.dag["dag"]["max_parallel"] = max_parallel

                results = asyncio.run(scheduler.run(
                    tools_factory=lambda session: build_tools(session),
                ))

                failed = [n for n, s in results.items() if s == "FAILED"]
                if failed:
                    print(f"[dag run] 失败节点: {', '.join(failed)}")
                    return EXIT_DEPENDENCY_ERROR

                print(f"[dag run] 全部完成: {len(results)} 个节点")
                return EXIT_SUCCESS

            except KeyboardInterrupt:
                print("\n[dag run] 已中断")
                return 130
            except Exception as e:
                print(f"[dag run] 错误: {e}")
                return EXIT_EXECUTION_ERROR

        # dag status
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

                        event = {
                            "ts": _now_iso(),
                            "dag": str(dag_path),
                            "completed": len(completed),
                            "total": len(nodes),
                            "running": running,
                            "failed": failed,
                            "pending": pending,
                        }

                        if args.json:
                            _ndjson_out(event)
                        else:
                            print(f"\r[{len(completed)}/{len(nodes)}] 运行中: {', '.join(running) or '无'}  失败: {', '.join(failed) or '无'}", end="", flush=True)

                        if len(completed) + len(failed) == len(nodes):
                            if not args.json:
                                print()
                            break

                        time.sleep(1)
                    return EXIT_SUCCESS
                except KeyboardInterrupt:
                    if not args.json:
                        print()
                    print("[status] 监听已停止")
                    return 130

            # 单次查询模式
            completed = [n for n in nodes if scheduler.node_is_done(n)]
            failed = [n for n in nodes if scheduler.node_is_failed(n)]
            running = [n for n in nodes if scheduler.node_is_running(n)]
            pending = [n for n in nodes if n not in completed and n not in failed and n not in running]

            result = {
                "dag": scheduler.dag.get("dag", {}).get("name", "untitled"),
                "path": str(dag_path),
                "completed": len(completed),
                "total": len(nodes),
                "running": [{"node": n, "model": nodes[n].get("model", "default")} for n in running],
                "failed": [{"node": n, "model": nodes[n].get("model", "default")} for n in failed],
                "pending": [{"node": n, "model": nodes[n].get("model", "default")} for n in pending],
            }

            if args.json:
                _json_out(result)
            else:
                print(f"DAG: {result['dag']}")
                print(f"  总节点: {result['total']}")
                print(f"  已完成: {result['completed']}")
                print(f"  运行中: {len(result['running'])}")
                for n in result["running"]:
                    print(f"    ⏳ {n['node']} (模型: {n['model']})")
                print(f"  失败: {len(result['failed'])}")
                for n in result["failed"]:
                    print(f"    ❌ {n['node']} (模型: {n['model']})")
                print(f"  等待中: {len(result['pending'])}")
                for n in result["pending"]:
                    print(f"    📋 {n['node']} (模型: {n['model']})")

            return EXIT_SUCCESS

        # dag stop
        if args.dag_cmd == "stop":
            dag_path = _resolve_dag_path(args.path)
            scheduler = _load_scheduler(dag_path)
            scheduler.cancel()
            print(f"[dag stop] 已发送取消信号到 DAG: {dag_path}")
            return EXIT_SUCCESS

    # ============================================================
    # node 子命令处理
    # ============================================================
    if args.cmd == "node":
        if not args.node_cmd:
            node_parser.print_help()
            return EXIT_SUCCESS

        dag_path = _resolve_dag_path(args.path)
        scheduler = _load_scheduler(dag_path)
        node_id = args.node

        # node run
        if args.node_cmd == "run":
            models_path = _resolve_models_path(args.models)
            if args.force:
                node_dir = scheduler.nodes_dir / node_id
                if node_dir.exists():
                    shutil.rmtree(node_dir)
                    print(f"[node run] 已重置节点: {node_id}")

            try:
                results = asyncio.run(scheduler._run_node(
                    node_id,
                    tools_factory=lambda session: build_tools(session),
                    hooks_factory=None,
                ))
                return EXIT_SUCCESS
            except Exception as e:
                print(f"[node run] 错误: {e}")
                return EXIT_EXECUTION_ERROR

        # node reset
        if args.node_cmd == "reset":
            node_dir = scheduler.nodes_dir / node_id
            if node_dir.exists():
                shutil.rmtree(node_dir)
                print(f"[node reset] 已重置节点: {node_id}")
            else:
                print(f"[node reset] 节点不存在: {node_id}")
            return EXIT_SUCCESS

        # node logs
        if args.node_cmd == "logs":
            error_log = scheduler.nodes_dir / node_id / ".system" / "error.log"
            if error_log.exists():
                lines = error_log.read_text(encoding="utf-8").splitlines()
                for line in lines[-args.tail:]:
                    print(line)
            else:
                print("(无错误日志)")
            return EXIT_SUCCESS

        # node history
        if args.node_cmd == "history":
            session = Session(scheduler.nodes_dir / node_id)
            messages = session.load_messages()

            if not messages:
                print("(无对话历史)")
                return EXIT_SUCCESS

            if args.json:
                _json_out({"node": node_id, "messages": messages})
            else:
                print(f"节点 {node_id} 的对话历史 ({len(messages)} 条):")
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = json.dumps(content, ensure_ascii=False)[:200]
                    else:
                        content = str(content)[:200]
                    print(f"  [{role}] {content}...")

            return EXIT_SUCCESS

    # ============================================================
    # models 子命令处理
    # ============================================================
    if args.cmd == "models":
        if not args.models_cmd:
            models_parser.print_help()
            return EXIT_SUCCESS

        models_path = _resolve_models_path(args.config)

        # models list
        if args.models_cmd == "list":
            registry = ModelRegistry()
            if models_path and models_path.exists():
                registry.load(models_path.parent)
            else:
                registry.load()

            result = []
            for name, cfg in registry._models.items():
                result.append({
                    "name": name,
                    "model": cfg.model,
                    "base_url": cfg.base_url,
                    "token_cap": cfg.token_cap,
                })

            if args.json:
                _json_out({"models": result})
            else:
                print("可用模型:")
                for m in result:
                    print(f"  {m['name']}: {m['model']} @ {m['base_url']} (token_cap: {m['token_cap']})")

            return EXIT_SUCCESS

        # models validate
        if args.models_cmd == "validate":
            registry = ModelRegistry()
            if models_path and models_path.exists():
                registry.load(models_path.parent)
            else:
                registry.load()

            print(f"验证 {len(registry._models)} 个模型配置...")
            for name, cfg in registry._models.items():
                if not cfg.api_key:
                    print(f"  ❌ {name}: API key 未设置")
                else:
                    print(f"  ✅ {name}: {cfg.model} @ {cfg.base_url}")

            return EXIT_SUCCESS

    # 未知命令
    print(f"[错误] 未知命令: {args.cmd}")
    return EXIT_ARGS_ERROR

