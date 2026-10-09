"""项目内原子检查点、可容忍损坏尾行的 JSONL 和秘密脱敏。"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import cast

from ..contracts import JsonObject, JsonValue
from ..project_paths import ProjectPaths

SECRET_KEYS = re.compile(r"api[_-]?key|authorization|password|access[_-]?token|secret", re.I)


def redact(value: JsonValue, secrets: tuple[str, ...] = ()) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SECRET_KEYS.search(key) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
        text = re.sub(
            r"(?i)([?&](?:api[_-]?key|token|access[_-]?token|key|password|secret)=)[^&\s]+",
            r"\1[REDACTED]",
            text,
        )
        text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
        return text
    return value


class SessionStore:
    def __init__(
        self,
        paths: ProjectPaths,
        session_id: str,
        secrets: tuple[str, ...] = (),
        *,
        namespace: str = "benchmarks",
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session_id):
            raise ValueError("Invalid session id")
        self.paths, self.session_id, self.secrets = paths, session_id, secrets
        if namespace != "benchmarks":
            raise ValueError("Invalid state namespace")
        self.root = paths.directory(Path(".state") / namespace / session_id)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def write(self, name: str, value: JsonValue, *, private: bool = False) -> None:
        destination = self.paths.writable(self.root / name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.writable(
            destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        )
        # 冻结源码可能本身包含凭证，恢复用快照保持字节身份且以 0600 存储；日志和输出始终脱敏。
        data = value if private else redact(value, self.secrets)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, name: str) -> JsonObject:
        value = json.loads(self.paths.writable(self.root / name).read_text())
        if not isinstance(value, dict):
            raise ValueError("Session artifact must be a JSON object")
        return cast(JsonObject, value)

    def append(self, event: JsonObject) -> None:
        destination = self.paths.writable(self.root / "events.jsonl")
        line = json.dumps(
            {"schema_version": 1, **cast(JsonObject, redact(event, self.secrets))},
            ensure_ascii=False,
        )
        descriptor = os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(line + "\n")
            stream.flush()

    def events(self) -> tuple[list[JsonObject], bool]:
        path = self.paths.writable(self.root / "events.jsonl")
        lines = path.read_bytes().decode(errors="replace").splitlines() if path.exists() else []
        result: list[JsonObject] = []
        for index, line in enumerate(lines):
            try:
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("schema_version") != 1:
                    raise ValueError("Unsupported event schema")
                result.append(cast(JsonObject, value))
            except (ValueError, json.JSONDecodeError):
                if index == len(lines) - 1:
                    return result, True
                raise ValueError("Corrupt event before the last line") from None
        return result, False
