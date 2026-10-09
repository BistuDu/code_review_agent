"""显式 JSON、环境变量和启动参数覆盖；配置读取没有导入时副作用。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, fields
from typing import Literal, cast
from urllib.parse import urlsplit

from .contracts import JsonObject, JsonValue, stable_digest
from .project_paths import ProjectPaths


@dataclass(frozen=True)
class ModelSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    max_output_tokens: int = 16384
    context_tokens: int = 300000
    temperature: float = 0.0
    timeout_seconds: float = 300.0
    retries: int = 2

    def validate(self) -> None:
        if not all(math.isfinite(value) for value in (self.temperature, self.timeout_seconds)):
            raise ValueError("Model numeric settings must be finite")
        if self.base_url:
            url = urlsplit(self.base_url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("base_url must be an HTTP(S) endpoint")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url cannot contain credentials, query or fragment")
        if self.max_output_tokens < 1 or self.context_tokens <= self.max_output_tokens:
            raise ValueError("context_tokens must exceed max_output_tokens")
        if self.retries < 0 or self.timeout_seconds <= 0 or self.temperature < 0:
            raise ValueError("Invalid request limits")

    def require(self, stage: str) -> None:
        self.validate()
        if not self.base_url or not self.model or not self.api_key:
            raise ValueError(f"Configure {stage} base_url, model and api_key before running")


@dataclass(frozen=True)
class Settings:
    reviewer: ModelSettings = ModelSettings()
    reflection: ModelSettings = ModelSettings()
    judge: ModelSettings = ModelSettings()
    protocol: str = "chat_completions"
    language: Literal["zh", "en"] = "zh"
    effort: Literal["low", "medium", "high"] = "medium"
    max_concurrency: int = 8
    max_tool_iterations: int = 100
    group_timeout_minutes: float = 15.0
    review_timeout_seconds: float = 0.0
    max_file_bytes: int = 2097152
    tool_result_chars: int = 50000
    group_min_files: int = 4
    group_max_files: int = 10
    plan_line_threshold: int = 50
    batch_size: int = 50
    batch_strategy: Literal["none", "by-language", "by-directory"] = "by-language"
    scan_plan: bool = True
    scan_dedup: bool = True
    scan_summary: bool = True
    dedup_min_comments: int = 4
    background: str = ""
    rule_file: str = ""
    global_rule_file: str = ""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def validate(self) -> None:
        for name in ("group_timeout_minutes", "review_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.protocol != "chat_completions":
            raise ValueError("Only chat_completions is supported")
        if self.language not in {"zh", "en"} or self.effort not in {"low", "medium", "high"}:
            raise ValueError("Invalid language or effort")
        if self.batch_strategy not in {"none", "by-language", "by-directory"}:
            raise ValueError("Invalid batch strategy")
        if any(
            type(value) is not bool
            for value in (self.scan_plan, self.scan_dedup, self.scan_summary)
        ):
            raise ValueError("Scan stage switches must be boolean")
        for name in (
            "max_concurrency",
            "max_tool_iterations",
            "max_file_bytes",
            "tool_result_chars",
            "group_min_files",
            "group_max_files",
            "plan_line_threshold",
            "batch_size",
            "dedup_min_comments",
        ):
            if cast(int, asdict(self)[name]) < 1:
                raise ValueError(f"{name} must be positive")
        for model in (self.reviewer, self.reflection, self.judge):
            model.validate()

    def public_json(self) -> JsonObject:
        value = cast(JsonObject, json.loads(json.dumps(asdict(self))))
        for role in ("reviewer", "reflection", "judge"):
            model = cast(JsonObject, value[role])
            model.pop("api_key")
        return value

    @property
    def review_rounds(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.effort]

    @property
    def identity(self) -> str:
        return stable_digest(self.public_json())


def _overlay(target: JsonObject, source: JsonObject) -> None:
    for key, value in source.items():
        if key not in target:
            raise ValueError(f"Unknown configuration: {key}")
        previous = target[key]
        if isinstance(previous, dict) and isinstance(value, dict):
            _overlay(previous, value)
        else:
            target[key] = value


def _environment_value(raw: str, default: JsonValue) -> JsonValue:
    if isinstance(default, str):
        return raw
    value = cast(JsonValue, json.loads(raw))
    return value


def load_settings(
    paths: ProjectPaths,
    config_file: str | None = None,
    overrides: JsonObject | None = None,
) -> Settings:
    """参数 > CODE_REVIEW_* 环境变量 > 项目内 JSON > 默认值。"""
    defaults = cast(JsonObject, json.loads(json.dumps(asdict(Settings()))))
    value = cast(JsonObject, json.loads(json.dumps(defaults)))
    if config_file:
        file = paths.writable(config_file)
        loaded = json.loads(file.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("Configuration must be a JSON object")
        _overlay(value, cast(JsonObject, loaded))
    for name, default in defaults.items():
        if isinstance(default, dict):
            nested = cast(JsonObject, value[name])
            for key, item in default.items():
                raw = os.environ.get(f"CODE_REVIEW_{name}_{key}".upper())
                if raw is not None:
                    nested[key] = _environment_value(raw, item)
        else:
            raw = os.environ.get(f"CODE_REVIEW_{name}".upper())
            if raw is not None:
                value[name] = _environment_value(raw, default)
    if overrides:
        _overlay(value, overrides)
    models: dict[str, ModelSettings] = {}
    for name in ("reviewer", "reflection", "judge"):
        model = cast(JsonObject, value.pop(name))
        # Reflection 默认继承服务设置，但创建 Agent 时仍使用全新状态。
        if name == "reflection":
            reviewer = asdict(models["reviewer"])
            for key in ("base_url", "model", "api_key"):
                if not model[key]:
                    model[key] = cast(JsonValue, reviewer[key])
        for field in fields(ModelSettings):
            default_model = asdict(ModelSettings())[field.name]
            item = model[field.name]
            if isinstance(default_model, str) and not isinstance(item, str):
                raise ValueError(f"{name}.{field.name} must be a string")
            if type(default_model) is int and type(item) is not int:
                raise ValueError(f"{name}.{field.name} must be an integer")
            if type(default_model) is float and (
                isinstance(item, bool) or not isinstance(item, (int, float))
            ):
                raise ValueError(f"{name}.{field.name} must be a number")
        models[name] = ModelSettings(**cast(dict[str, object], model))  # type: ignore[arg-type]
    for key, default in defaults.items():
        if key in models:
            continue
        item = value[key]
        if type(default) is int and type(item) is not int:
            raise ValueError(f"{key} must be an integer")
        if type(default) is float and (
            isinstance(item, bool) or not isinstance(item, (int, float))
        ):
            raise ValueError(f"{key} must be a number")
        if isinstance(default, str) and not isinstance(item, str):
            raise ValueError(f"{key} must be a string")
    for key in ("include", "exclude"):
        patterns = value[key]
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise ValueError(f"{key} must be a list of strings")
        value[key] = cast(JsonValue, patterns)
    # kwargs 只在外部配置转换边界使用，字段已在上方逐项校验。
    plain = cast(dict[str, object], value)
    plain["include"] = tuple(cast(list[str], value["include"]))
    plain["exclude"] = tuple(cast(list[str], value["exclude"]))
    result = Settings(**models, **plain)  # type: ignore[arg-type]
    result.validate()
    return result
