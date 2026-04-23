from __future__ import annotations

"""安全审查（从 EVA 移植）。"""

import json
from typing import Any


class SecurityReviewer:
    """用 LLM 自己审查命令安全性。"""

    REVIEW_PROMPT = """你是一个安全专家。请审查下面的 {shell} 命令：

<command>
{command}
</command>

规则：
- 如果命令仅为只读操作（cat, ls, grep, find, head, tail, pwd, echo），输出"放行"
- 如果命令涉及写入、删除、执行、网络连接、权限修改，输出"禁止"
- 如果命令拼接了管道且难以判断，输出"禁止"

只输出"放行"或"禁止"这两个字。"""

    def __init__(self, llm_client: Any, model: str = "deepseek-chat") -> None:
        self.client = llm_client
        self.model = model

    async def review(self, command: str, shell: str = "bash") -> bool:
        """返回 True 表示放行，False 表示禁止。"""
        prompt = self.REVIEW_PROMPT.format(shell=shell, command=command)
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            text = resp.choices[0].message.content
            return "放行" in text and "禁止" not in text
        except Exception:
            return False

