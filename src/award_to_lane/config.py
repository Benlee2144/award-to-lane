"""Configuration: config.yaml for behaviour, .env for secrets.

Paths in config.yaml are relative to the directory that holds config.yaml (the project root).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class Settings:
    def __init__(self, data: dict[str, Any], root: Path, config_path: Path):
        self.data = data
        self.root = root
        self.config_path = config_path

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        path = Path(config_path) if config_path else Path.cwd() / "config.yaml"
        path = path.resolve()
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        root = path.parent
        load_dotenv(root / ".env")
        return cls(data, root, path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str) -> Path:
        value = Path(str(self.get(dotted, default)))
        return value if value.is_absolute() else (self.root / value)

    def env(self, name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name, default)
        return value if value else default
