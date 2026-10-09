"""跨阶段契约。此模块不依赖流程服务或 AgentScope 的内部消息类型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def stable_digest(value: JsonValue) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class InputMode(StrEnum):
    WORKSPACE = "workspace"
    RANGE = "range"
    COMMIT = "commit"
    SCAN = "scan"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    NO_FILES = "no_files"
    PREVIEW = "preview"


class Side(StrEnum):
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class DiffLine:
    text: str
    kind: str
    old_line: int | None
    new_line: int | None


@dataclass(frozen=True)
class Hunk:
    old_start: int
    new_start: int
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    content: str
    old_content: str = ""
    diff: str = ""
    hunks: tuple[Hunk, ...] = ()
    old_path: str | None = None
    binary: bool = False
    deleted: bool = False
    byte_size: int = 0

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class FileSelection:
    path: str
    included: bool
    reason: str


@dataclass(frozen=True)
class ReviewInput:
    repo: str
    mode: InputMode
    base_sha: str | None
    head_sha: str | None
    files: tuple[FileSnapshot, ...]
    selection: tuple[FileSelection, ...]
    snapshot_id: str
    workspace_state: tuple[str, ...] = ()
    from_sha: str | None = None
    range_strategy: str = "merge-base"

    @property
    def targets(self) -> tuple[FileSnapshot, ...]:
        included = {item.path for item in self.selection if item.included}
        return tuple(file for file in self.files if file.path in included)


@dataclass(frozen=True)
class ReviewUnit:
    unit_id: str
    paths: tuple[str, ...]
    reason: str = "single file"


@dataclass
class UnitResult:
    unit: ReviewUnit
    coverage: dict[str, str]
    candidates: list[FindingCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    locations: list[LocationDecision] = field(default_factory=list)
    reflections: list[ReflectionDecision] = field(default_factory=list)
    raw_candidates: list[FindingCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class FindingCandidate:
    candidate_id: str
    path: str
    content: str
    existing_code: str
    category: str
    severity: str
    side: Side = Side.RIGHT
    suggestion_code: str = ""
    proposed_start: int | None = None
    proposed_end: int | None = None

    @classmethod
    def from_json(cls, value: JsonObject, identity: str) -> FindingCandidate:
        """在工具边界校验外部结构；不相信模型提供的行号。"""
        required = ("path", "content", "existing_code", "category", "severity")
        for key in required:
            if not isinstance(value.get(key), str) or not value[key]:
                raise ValueError(f"Non-empty string required: {key}")
        if value["category"] not in {
            "bug",
            "security",
            "performance",
            "maintainability",
            "test",
            "style",
            "documentation",
            "other",
        }:
            raise ValueError("Invalid comment category")
        if value["severity"] not in {"critical", "high", "medium", "low"}:
            raise ValueError("Invalid comment severity")
        path = cast(str, value["path"])
        content = cast(str, value["content"])
        snippet = cast(str, value["existing_code"])
        side = Side(cast(str, value.get("side", "right")))
        suggestion = value.get("suggestion_code", "")
        if not isinstance(suggestion, str):
            raise ValueError("suggestion_code must be a string")
        proposed: list[int | None] = []
        for key in ("start_line", "end_line"):
            line = value.get(key)
            if line is not None and (type(line) is not int or line < 1):
                raise ValueError(f"Invalid {key}")
            proposed.append(line)
        identifier = stable_digest([identity, path, side.value, content, snippet])
        return cls(
            identifier,
            path,
            content,
            snippet,
            cast(str, value["category"]),
            cast(str, value["severity"]),
            side,
            suggestion,
            *proposed,
        )


@dataclass(frozen=True)
class Location:
    path: str
    side: Side
    start_line: int
    end_line: int
    snippet: str

    def __post_init__(self) -> None:
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("Invalid location interval")


@dataclass(frozen=True)
class LocationAttempt:
    stage: str
    matches: tuple[Location, ...]
    reason: str = ""


@dataclass(frozen=True)
class LocationDecision:
    candidate_id: str
    status: str
    location: Location | None
    attempts: tuple[LocationAttempt, ...]


@dataclass(frozen=True)
class ReflectionDecision:
    candidate_id: str
    decision: str
    reason: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"keep", "reject", "undecided"}:
            raise ValueError("Invalid reflection decision")


@dataclass(frozen=True)
class UsageRecord:
    stage: str
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class RunManifest:
    session_id: str
    input_identity: str
    config_identity: str
    resource_identity: str
    mode: InputMode
    repo: str
    base_sha: str | None
    head_sha: str | None
    schema_version: int = 1
    parent_session: str | None = None


@dataclass
class ReviewResult:
    status: RunStatus
    manifest: RunManifest
    candidates: list[FindingCandidate] = field(default_factory=list)
    raw_candidates: list[FindingCandidate] = field(default_factory=list)
    locations: list[LocationDecision] = field(default_factory=list)
    reflections: list[ReflectionDecision] = field(default_factory=list)
    groups: list[ReviewUnit] = field(default_factory=list)
    scan_batches: list[ReviewUnit] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    usage: list[UsageRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suppressed: dict[str, str] = field(default_factory=dict)
    rewritten_content: dict[str, str] = field(default_factory=dict)
    project_summary: str = ""
    historical_usage: list[UsageRecord] = field(default_factory=list)
    preparation_seconds: float = 0.0
    review_seconds: float = 0.0
    reused_units: int = 0
    rerun_units: int = 0
    tool_calls: list[JsonObject] = field(default_factory=list)

    def to_json(self) -> JsonObject:
        # str 枚举与 JSON 兼容；统一在序列化边界转为普通容器。
        return cast(JsonObject, json.loads(json.dumps(asdict(self), ensure_ascii=False)))
