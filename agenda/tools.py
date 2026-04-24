from __future__ import annotations

"""工具注册表与工具工厂。"""

import asyncio
import inspect
import json
from typing import Any, Callable

from .session import Session



ToolFunc = Callable[..., str]


class ToolRegistry:
    """Agent 可调用的工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFunc] = {}

    def register(self, name: str, func: ToolFunc | None = None) -> ToolFunc | Callable[[ToolFunc], ToolFunc]:
        if func is None:
            # 装饰器用法: @register("name")
            def decorator(f: ToolFunc) -> ToolFunc:
                self._tools[name] = f
                return f
            return decorator
        self._tools[name] = func
        return func

    def get(self, name: str) -> ToolFunc | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        """生成 OpenAI function calling 格式的 schemas。"""
        schemas = []
        for name, func in self._tools.items():
            sig = self._infer_schema(func)
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (func.__doc__ or "").strip(),
                    "parameters": sig,
                },
            })
        return schemas

    @staticmethod
    def _infer_schema(func: ToolFunc) -> dict:
        """从函数签名简单推断 JSON Schema（支持常见类型标注）。"""
        import inspect
        sig = inspect.signature(func)
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            if pname in ("session",):
                continue
            ann = param.annotation
            pschema: dict = {"type": "string"}
            if ann is int:
                pschema = {"type": "integer"}
            elif ann is bool:
                pschema = {"type": "boolean"}
            elif ann is float:
                pschema = {"type": "number"}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                pschema["default"] = param.default
            props[pname] = pschema
        return {"type": "object", "properties": props, "required": required}



def build_tools(session: Session, allow_shell: bool = False) -> ToolRegistry:
    """
    为给定 Session 创建工具注册表。
    工具被限制在该 Session 的 input/、workspace/ 和 output/ 内。
    """
    tools = ToolRegistry()

    @tools.register("read_file")
    def read_file(path: str) -> str:
        """读取 input/、workspace/ 或 output/ 下的文件内容。"""
        return session.read_file(path)

    @tools.register("write_file")
    def write_file(path: str, content: str) -> str:
        """写入 workspace/ 或 output/ 目录。"""
        return session.write_file(path, content)

    @tools.register("list_dir")
    def list_dir(path: str = ".") -> str:
        """列出 input/、workspace/ 或 output/ 下的目录内容。"""
        return session.list_dir(path)

    @tools.register("done_compact")
    def done_compact() -> str:
        """通知系统记忆压缩已完成。"""
        return "[系统] 记忆压缩完成"

    # 可选：shell 工具（受 Guardian 路径边界保护）
    if allow_shell:

        @tools.register("run_shell")
        def run_shell(command: str, timeout: int = 30) -> str:
            """执行 shell 命令。Guardian 保证文件操作在 node_dir 内。"""
            import subprocess

            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(session.node_dir),
                )
                output = result.stdout or ""
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"
                return output[:4000]
            except subprocess.TimeoutExpired:
                return f"[超时] 命令执行超过 {timeout} 秒"
            except Exception as e:
                return f"[错误] {type(e).__name__}: {e}"

    return tools

