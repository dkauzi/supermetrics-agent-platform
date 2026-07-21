"""Configuration loading. One place that knows where settings come from.

Precedence: environment variable > config/platform.yaml > code default.
Nothing else in the codebase reads os.environ directly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

load_dotenv(REPO_ROOT / ".env")


class Config:
    """Dotted-path access over the merged YAML + env configuration."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        value = self.get(path)
        if value is None:
            raise ConfigError(f"Missing required config key: {path}")
        return value

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed. Fails loud at startup."""


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Env vars win over YAML so an operator can change behaviour without a deploy."""
    if model := os.getenv("LLM_MODEL"):
        data.setdefault("llm", {})["model_chain"] = [model]

    if warehouse := os.getenv("WAREHOUSE"):
        data.setdefault("platform", {})["warehouse"] = warehouse

    if rate := os.getenv("TOOL_FAILURE_INJECTION_RATE"):
        data.setdefault("tools", {})["failure_injection_rate"] = float(rate)

    return data


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    config_path = path or CONFIG_DIR / "platform.yaml"
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open() as handle:
        data = yaml.safe_load(handle) or {}

    return Config(_apply_env_overrides(data))


def llm_mode() -> str:
    """'live' calls OpenRouter; 'offline' uses the deterministic local analyser.

    Falls back to offline when no API key is present so the pipeline always runs.
    """
    mode = os.getenv("LLM_MODE", "live").lower()
    if mode == "live" and not os.getenv("OPENROUTER_API_KEY"):
        return "offline"
    return mode


def openrouter_api_key() -> str | None:
    return os.getenv("OPENROUTER_API_KEY")


def data_dir() -> Path:
    path = REPO_ROOT / "data"
    path.mkdir(exist_ok=True)
    return path
