from __future__ import annotations

"""Guardian — 文件系统路径边界守卫。

## 设计理念

学 Butterfly 的路径沙箱设计。Guardian 是一个纯路径层面的安全层：
    - 所有 Agent 的文件操作都锚定在 node_dir 内
    - 相对路径默认 join 到 root，防止读取系统文件
    - resolve() 后检查 relative_to，防止 symlink 逃逸
    - 两种级别的检查：root 边界（check）和语义边界（Session._resolve_safe）

## 安全模型

    Guardian.check("../../etc/passwd")
      → resolve → ~/project/nodes/x/../../etc/passwd → /etc/passwd
      → relative_to(root) → ValueError → PermissionError

    Guardian.check("input/evil_link")
      → 如果 symlink 指向 /etc/passwd，resolve 会跟过去
      → relative_to(root) → ValueError → PermissionError

不需要文件系统权限、不需要 chroot、不需要容器。
纯 Python Path 操作实现的安全边界。
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
        except ValueError as _guardian_err:
            raise PermissionError(
                f"[Guardian] {operation} to {target} denied; allowed root is {self.root}"
            ) from _guardian_err

    def check_write(self, path: Path | str) -> Path:
        """检查写路径是否允许。"""
        return self.check(path, operation="write")

    def check_read(self, path: Path | str) -> Path:
        """检查读路径是否允许。"""
        return self.check(path, operation="read")
