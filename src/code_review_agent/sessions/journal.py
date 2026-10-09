"""唯一 writer 追加完整行；只读 reader 不修补或创建会话。"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import cast

from ..contracts import JsonObject
from ..project_paths import ProjectPaths
from .records import DURABLE, SCHEMA, make_record, validate_record
from .store import redact


class JournalError(RuntimeError):
    """持久化失败不能作为模型失败降级处理。"""


def journal_path(paths: ProjectPaths, session_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session_id):
        raise ValueError("Invalid session id")
    return paths.writable(Path(".state/sessions") / f"{session_id}.jsonl")


def read_records(paths: ProjectPaths, session_id: str) -> tuple[list[JsonObject], bool]:
    path = journal_path(paths, session_id)
    if not path.is_file():
        legacy = paths.writable(Path(".state/sessions") / session_id)
        if legacy.is_dir():
            raise ValueError("Unsupported legacy session format; old files are unchanged")
        raise ValueError(f"Session not found: {session_id}")
    records: list[JsonObject] = []
    ids: set[str] = set()
    damaged = False
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                damaged = True
                break
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Expected journal object")
                record = cast(JsonObject, value)
                validate_record(record, session_id, len(records) + 1)
                identifier = str(record["id"])
                if identifier in ids:
                    raise ValueError("Repeated journal event id")
                ids.add(identifier)
                records.append(record)
            except (ValueError, UnicodeError) as error:
                raise ValueError(
                    f"Corrupt committed journal line {len(records) + 1}: {error}"
                ) from error
    return records, damaged


class SessionJournal:
    def __init__(self, paths: ProjectPaths, session_id: str, secrets: tuple[str, ...] = ()):
        self.session_id, self.secrets = session_id, secrets
        self.path = journal_path(paths, session_id)
        paths.directory(self.path.parent)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(descriptor, "wb")
        self._lock = threading.Lock()
        self._seq = 0
        self._failure: OSError | None = None
        # 目录项与启动记录一起同步，避免只同步内容却丢失新文件名。
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        except OSError as error:
            self._stream.close()
            raise JournalError(f"Session journal creation failed: {error}") from error
        finally:
            os.close(directory)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def check(self) -> None:
        if self._failure:
            raise JournalError(
                f"Session journal persistence failed: {self._failure}"
            ) from self._failure

    def append(
        self,
        name: str,
        value: JsonObject,
        metadata: JsonObject | None = None,
        *,
        event: JsonObject | None = None,
    ) -> JsonObject:
        with self._lock:
            self.check()
            context = {
                **(metadata or {}),
                "schema": SCHEMA,
                "session_id": self.session_id,
                "seq": self._seq + 1,
            }
            record = (
                {**event, "name": name, "value": value, "metadata": context}
                if event is not None
                else make_record(name, value, context)
            )
            record = cast(JsonObject, redact(record, self.secrets))
            validate_record(record, self.session_id, self._seq + 1)
            raw = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            try:
                self._stream.write(raw)
                self._stream.flush()
                if name in DURABLE:
                    os.fsync(self._stream.fileno())
            except OSError as error:
                self._failure = error
                self.check()
            self._seq += 1
            return record

    def record_interaction(self, record: JsonObject) -> None:
        value, metadata = record["value"], record["metadata"]
        if not isinstance(value, dict) or not isinstance(metadata, dict):
            raise ValueError("Invalid interaction record")
        persisted = self.append(str(record["name"]), value, metadata, event=record)
        # 同一 SDK 聚合记录的身份在当前输出与磁盘查询中保持一致。
        record.clear()
        record.update(persisted)

    def close(self) -> None:
        self._stream.close()
