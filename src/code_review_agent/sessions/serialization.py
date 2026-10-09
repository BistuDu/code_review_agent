"""持久化格式的显式转换；不使用 pickle 或加载任意 Python 对象。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

from ..contracts import (
    DiffLine,
    FileSelection,
    FileSnapshot,
    FindingCandidate,
    Hunk,
    InputMode,
    JsonObject,
    JsonValue,
    Location,
    LocationAttempt,
    LocationDecision,
    ReflectionDecision,
    ReviewInput,
    ReviewUnit,
    Side,
    UnitResult,
    UsageRecord,
)


def json_object(value: object) -> JsonObject:
    return cast(JsonObject, json.loads(json.dumps(value, ensure_ascii=False)))


def encode_input(review_input: ReviewInput) -> JsonObject:
    return json_object(asdict(review_input))


def decode_input(value: JsonObject) -> ReviewInput:
    files: list[FileSnapshot] = []
    for item in objects(value["files"]):
        hunks = tuple(
            Hunk(
                int(str(hunk["old_start"])),
                int(str(hunk["new_start"])),
                tuple(
                    DiffLine(
                        string(line["text"]),
                        string(line["kind"]),
                        optional_integer(line["old_line"]),
                        optional_integer(line["new_line"]),
                    )
                    for line in objects(hunk["lines"])
                ),
            )
            for hunk in objects(item["hunks"])
        )
        files.append(
            FileSnapshot(
                string(item["path"]),
                string(item["content"]),
                string(item.get("old_content", "")),
                string(item.get("diff", "")),
                hunks,
                optional_string(item.get("old_path")),
                boolean(item.get("binary", False)),
                boolean(item.get("deleted", False)),
                integer(item.get("byte_size", 0)),
            )
        )
    return ReviewInput(
        str(value["repo"]),
        InputMode(str(value["mode"])),
        optional_string(value.get("base_sha")),
        optional_string(value.get("head_sha")),
        tuple(files),
        tuple(
            FileSelection(string(item["path"]), boolean(item["included"]), string(item["reason"]))
            for item in objects(value["selection"])
        ),
        str(value["snapshot_id"]),
        tuple(strings(value.get("workspace_state", []))),
        optional_string(value.get("from_sha")),
    )


def optional_string(value: JsonValue) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Expected string or null")
    return value


def string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected string")
    return value


def integer(value: JsonValue) -> int:
    if type(value) is not int:
        raise ValueError("Expected integer")
    return value


def optional_integer(value: JsonValue) -> int | None:
    return None if value is None else integer(value)


def boolean(value: JsonValue) -> bool:
    if type(value) is not bool:
        raise ValueError("Expected boolean")
    return value


def decode_usage(value: JsonObject) -> UsageRecord:
    duration = value.get("duration_seconds", 0.0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise ValueError("Invalid usage duration")
    return UsageRecord(
        string(value["stage"]),
        optional_integer(value["input_tokens"]),
        optional_integer(value["output_tokens"]),
        optional_integer(value.get("cached_tokens")),
        float(duration),
    )


def strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected string array")
    return cast(list[str], value)


def objects(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Expected object array")
    return cast(list[JsonObject], value)


def decode_candidate(value: JsonObject) -> FindingCandidate:
    return FindingCandidate(
        string(value["candidate_id"]),
        string(value["path"]),
        string(value["content"]),
        string(value["existing_code"]),
        string(value["category"]),
        string(value["severity"]),
        Side(string(value["side"])),
        string(value.get("suggestion_code", "")),
        optional_integer(value.get("proposed_start")),
        optional_integer(value.get("proposed_end")),
    )


def decode_unit(value: JsonObject) -> ReviewUnit:
    return ReviewUnit(str(value["unit_id"]), tuple(strings(value["paths"])), str(value["reason"]))


def decode_unit_result(value: JsonObject) -> UnitResult:
    unit = value["unit"]
    if not isinstance(unit, dict) or not isinstance(value["coverage"], dict):
        raise ValueError("Invalid unit checkpoint")
    return UnitResult(
        decode_unit(unit),
        {key: str(item) for key, item in value["coverage"].items()},
        [decode_candidate(item) for item in objects(value["candidates"])],
        strings(value["warnings"]),
        [decode_location_decision(item) for item in objects(value.get("locations", []))],
        [decode_reflection(item) for item in objects(value.get("reflections", []))],
    )


def decode_location(value: JsonObject) -> Location:
    return Location(
        string(value["path"]),
        Side(string(value["side"])),
        integer(value["start_line"]),
        integer(value["end_line"]),
        string(value["snippet"]),
    )


def decode_location_decision(value: JsonObject) -> LocationDecision:
    location = value["location"]
    if location is not None and not isinstance(location, dict):
        raise ValueError("Invalid saved location")
    attempts = tuple(
        LocationAttempt(
            string(attempt["stage"]),
            tuple(decode_location(item) for item in objects(attempt["matches"])),
            string(attempt["reason"]),
        )
        for attempt in objects(value["attempts"])
    )
    return LocationDecision(
        string(value["candidate_id"]),
        string(value["status"]),
        decode_location(location) if location else None,
        attempts,
    )


def decode_reflection(value: JsonObject) -> ReflectionDecision:
    return ReflectionDecision(
        string(value["candidate_id"]),
        string(value["decision"]),
        string(value["reason"]),
        tuple(strings(value["evidence"])),
    )
