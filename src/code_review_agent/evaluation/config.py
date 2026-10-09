"""评测专用配置；不会进入普通 review 的 Settings 或会话身份。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..config import ModelSettings, Settings
from ..contracts import JsonObject, JsonValue
from ..project_paths import ProjectPaths


@dataclass(frozen=True)
class ClaudeConfig:
    model: str
    base_url: str
    api_key: str
    command: str = "claude"

    def public_json(self) -> JsonObject:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "command": self.command,
            "protocol": "anthropic",
            "isolation": "explicit-settings-hooks-v1",
        }

    def require(self) -> None:
        ModelSettings(base_url=self.base_url, model=self.model, api_key=self.api_key).require(
            "claude"
        )
        if not self.command:
            raise ValueError("Claude command is required")


def load_claude_config(
    paths: ProjectPaths, settings: Settings, filename: str | None = None
) -> ClaudeConfig:
    value = json.loads(paths.writable(filename).read_text()) if filename else {}
    if not isinstance(value, dict) or set(value) - {"model", "base_url", "api_key", "command"}:
        raise ValueError("Invalid Claude benchmark configuration")
    defaults = {
        "model": settings.reviewer.model,
        "base_url": "",
        "api_key": settings.reviewer.api_key,
        "command": "claude",
    }
    for name in defaults:
        candidate = os.environ.get(
            "CODE_REVIEW_CLAUDE_" + name.upper(), value.get(name, defaults[name])
        )
        if not isinstance(candidate, str):
            raise ValueError("Claude configuration values must be strings")
        defaults[name] = candidate
    if not defaults["base_url"] and settings.reviewer.base_url.rstrip("/") in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    }:
        defaults["base_url"] = "https://api.deepseek.com/anthropic"
    config = ClaudeConfig(**defaults)
    # 空 endpoint 可在仅准备/本项目评审时保留，Claude 实际运行前必须显式确定。
    ModelSettings(base_url=config.base_url, model=config.model, api_key=config.api_key).validate()
    return config


def external_rules_identity(paths: ProjectPaths, settings: Settings) -> str:
    """恢复前只读外置规则及引用内容，不访问仓库或调用模型。"""
    import hashlib
    from pathlib import Path

    from ..contracts import stable_digest

    fingerprints: list[JsonValue] = []
    for filename in (settings.rule_file, settings.global_rule_file):
        if not filename:
            continue
        file = paths.writable(filename)
        text = file.read_text()
        fingerprints.append([filename, hashlib.sha256(file.read_bytes()).hexdigest()])
        if file.suffix.lower() != ".json":
            continue
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get("rules", []), list):
            raise ValueError("Invalid external rule configuration")
        for entry in value.get("rules", []):
            if not isinstance(entry, dict):
                raise ValueError("Invalid external rule entry")
            content = entry.get("rule", "")
            if (
                isinstance(content, str)
                and "\n" not in content
                and " " not in content
                and Path(content).suffix.lower() in {".md", ".txt", ".markdown"}
            ):
                try:
                    reference = paths.writable(content)
                    if reference.stat().st_size > 512 * 1024:
                        digest = "oversized"
                    else:
                        digest = hashlib.sha256(reference.read_bytes()).hexdigest()
                except (ValueError, OSError):
                    digest = "unreadable"
                fingerprints.append([content, digest])
    return stable_digest(fingerprints)
