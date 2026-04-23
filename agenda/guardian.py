from __future__ import annotations

"""Guardian — 文件系统路径边界。

学 Butterfly 的设计：
- resolve_target(): 把路径 resolve 成绝对路径（相对路径 join 到 root）
- is_allowed(): 检查是否在 root 内（用 relative_to，防 symlink 逃逸）
- check(): 不允许时抛 PermissionError

所有 Agent 的文件操作工具都必须经过 Guardian 检查。
"""

from pathlib import Path


class Guardian:
    """路径边界，锚定在单个目录。

    构造时 resolve root，之后相对路径 join 到 root 后再 resolve，
    防止 symlink 逃逸和路径遍历攻击。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def __repr__(self) -> str:
        return f"Guardian(root={self.root!r})"

    def resolve(self, path: Path | str) -> Path:
        """把路径 resolve 为绝对路径。相对路径 join 到 root。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return p.resolve()

    def is_allowed(self, path: Path | str) -> bool:
        """检查路径是否在 root 内（防 symlink 逃逸）。"""
        target = self.resolve(path)
        try:
            target.relative_to(self.root)
            return True
        except ValueError:
            return False

    def check(self, path: Path | str, *, operation: str = "access") -> Path:
        """检查路径是否允许访问，不允许则抛 PermissionError。返回 resolve 后的路径。"""
        target = self.resolve(path)
        try:
            target.relative_to(self.root)
            return target
        except ValueError:
            raise PermissionError(
                f"[Guardian] {operation} to {target} denied; allowed root is {self.root}"
            )

    def check_write(self, path: Path | str) -> Path:
        """检查写路径是否允许。"""
        return self.check(path, operation="write")

    def check_read(self, path: Path | str) -> Path:
        """检查读路径是否允许。"""
        return self.check(path, operation="read")
