from __future__ import annotations

"""模型注册表 — 多模型配置。"""

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass

try:
    import yaml
except ImportError:
    print("[错误] 需要安装 PyYAML: pip install pyyaml")
    sys.exit(1)

from .const import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_NODE_TIMEOUT,
    MAX_SUB_AGENT_DEPTH,
)


@dataclass
class ModelConfig:
    """单个模型的配置。"""
    name: str                # 模型别名，如 "deepseek"、"kimi"
    base_url: str            # API 端点
    api_key: str             # API 密钥
    model: str               # 实际模型名，如 "deepseek-chat"
    token_cap: int = 32000   # 上下文窗口上限
    provider: str = "openai" # 预留：未来支持非 OpenAI 接口
    fallback_model: str | None = None   # 备用模型名（主模型失败时切换）
    fallback_provider: str | None = None # 备用 provider
    temperature: float = 1.0 # 采样温度（DeepSeek 建议 Agent 用 1.0）
    max_tokens: int | None = None  # 最大输出 token 数
    extra_params: dict | None = None  # provider-specific 参数（如 thinking、reasoning_effort）


class ModelRegistry:
    """
    模型注册表：管理多个 LLM 配置。
    支持从 DAG 工作区内 models.yaml、~/.agenda/models.yaml、环境变量加载。
    """

    _GLOBAL_PATH = Path.home() / ".agenda" / "models.yaml"

    def __init__(self) -> None:
        self._models: dict[str, ModelConfig] = {}

    def load(self, dag_dir: Path | None = None) -> ModelRegistry:
        """加载模型配置。"""
        # 1. 先尝试 DAG 工作区内的 models.yaml
        if dag_dir:
            local_file = dag_dir / "models.yaml"
            if local_file.exists():
                self._load_file(local_file)
                return self

        # 2. 再尝试全局配置
        if self._GLOBAL_PATH.exists():
            self._load_file(self._GLOBAL_PATH)
            return self

        # 3. fallback：从环境变量创建默认模型
        self._models["default"] = ModelConfig(
            name="default",
            base_url=os.environ.get("AGENDA_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("AGENDA_API_KEY", ""),
            model=os.environ.get("AGENDA_MODEL", "gpt-4"),
            token_cap=int(os.environ.get("AGENDA_TOKEN_CAP", "32000")),
        )
        return self

    def _load_file(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, cfg in (raw.get("models") or {}).items():
            if not isinstance(cfg, dict):
                continue
            # 标准字段
            standard_fields = {
                "base_url", "api_key", "model", "token_cap", "provider",
                "fallback_model", "fallback_provider", "temperature", "max_tokens",
            }
            extra = {k: v for k, v in cfg.items() if k not in standard_fields}
            self._models[name] = ModelConfig(
                name=name,
                base_url=self._resolve_value(cfg.get("base_url", "")),
                api_key=self._resolve_value(cfg.get("api_key", "")),
                model=self._resolve_value(cfg.get("model", "")),
                token_cap=int(cfg.get("token_cap", 32000)),
                provider=cfg.get("provider", "openai"),
                fallback_model=cfg.get("fallback_model"),
                fallback_provider=cfg.get("fallback_provider"),
                temperature=float(cfg.get("temperature", 1.0)),
                max_tokens=cfg.get("max_tokens"),
                extra_params=extra if extra else None,
            )

    def _resolve_value(self, value: str) -> str:
        """解析 ${ENV_VAR} 格式的值。"""
        if not isinstance(value, str):
            return str(value)
        match = re.match(r'^\$\{([^}]+)\}$', value.strip())
        if match:
            env_name = match.group(1)
            env_val = os.environ.get(env_name, "")
            if not env_val:
                print(f"[警告] 环境变量未设置: {env_name}")
            return env_val
        return value

    def get(self, name: str | None) -> ModelConfig:
        """获取模型配置。如果 name 为 None 或不存在，返回 default。"""
        if not name:
            return self._models.get("default", self._default_fallback())
        if name not in self._models:
            # 尝试匹配 model 字段（兼容直接写 model id）
            for cfg in self._models.values():
                if cfg.model == name:
                    return cfg
            print(f"[警告] 未知模型别名 '{name}'，使用 default")
            return self._models.get("default", self._default_fallback())
        return self._models[name]

    def _default_fallback(self) -> ModelConfig:
        return ModelConfig(
            name="default",
            base_url=os.environ.get("AGENDA_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("AGENDA_API_KEY", ""),
            model=os.environ.get("AGENDA_MODEL", "gpt-4"),
            token_cap=32000,
            temperature=1.0,
        )

    def list_models(self) -> list[str]:
        return list(self._models.keys())

