"""无源码输入身份与文件 checkpoint；恢复内容和展示评论分别引用。"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import cast

from ..contracts import (
    FindingCandidate,
    JsonObject,
    JsonValue,
    ReviewInput,
    ReviewUnit,
    UnitResult,
    stable_digest,
)
from .serialization import (
    decode_candidate,
    decode_location_decision,
    decode_reflection,
    decode_unit,
    json_object,
    objects,
    strings,
)


def input_metadata(frozen: ReviewInput) -> JsonObject:
    """覆盖所有工具可读文件，独立源码副本不会进入 journal。"""
    return {
        "repo": frozen.repo,
        "mode": frozen.mode.value,
        "from_sha": frozen.from_sha,
        "base_sha": frozen.base_sha,
        "head_sha": frozen.head_sha,
        "snapshot_id": frozen.snapshot_id,
        **({"range_strategy": "direct"} if frozen.range_strategy == "direct" else {}),
        "workspace_state": list(frozen.workspace_state),
        "selection": [json_object(asdict(item)) for item in frozen.selection],
        "files": [
            {
                "path": file.path,
                "content_hash": file.content_hash,
                "old_content_hash": stable_digest(file.old_content),
                "diff_hash": stable_digest(file.diff),
                "hunks_hash": stable_digest(cast(JsonValue, [asdict(hunk) for hunk in file.hunks])),
                "old_path": file.old_path,
                "binary": file.binary,
                "deleted": file.deleted,
                "byte_size": file.byte_size,
            }
            for file in frozen.files
        ],
    }


def encode_file(output: UnitResult, path: str) -> JsonObject:
    """同一份 candidate 只存一次；不同文本版本和重复提交各自保留。"""
    locations = {item.candidate_id: item for item in output.locations}
    reflections = {item.candidate_id: item for item in output.reflections}
    rows: list[JsonValue] = []
    known: dict[str, str] = {}

    def references(candidates: list[FindingCandidate]) -> list[str]:
        refs: list[str] = []
        for candidate in candidates:
            if candidate.path != path:
                continue
            value = json_object(asdict(candidate))
            # candidate_id 已标识一次提交；正文变体仍需区分 canonical 和 raw。
            identity = stable_digest(value)
            key = known.get(identity)
            if key is None:
                key = uuid.uuid4().hex
                known[identity] = key
                location = locations.get(candidate.candidate_id)
                reflection = reflections.get(candidate.candidate_id)
                rows.append(
                    {
                        "key": key,
                        "candidate": value,
                        "location": json_object(asdict(location)) if location else None,
                        "reflection": json_object(asdict(reflection)) if reflection else None,
                    }
                )
            refs.append(key)
        return refs

    restore_refs = references(output.candidates)
    raw_refs = references(output.raw_candidates or output.candidates)
    return {
        "unit": json_object(asdict(output.unit)),
        "coverage": output.coverage[path],
        "findings": rows,
        "restore_refs": [key for key in restore_refs],
        "raw_refs": [key for key in raw_refs],
        "warnings": list(output.warnings),
    }


def decode_file(path: str, payload: JsonObject) -> UnitResult:
    findings = {str(item["key"]): item for item in objects(payload["findings"])}
    restore_refs, raw_refs = strings(payload["restore_refs"]), strings(payload["raw_refs"])
    if any(key not in findings for key in restore_refs + raw_refs):
        raise ValueError("Unknown file finding reference")
    unit = ReviewUnit(stable_digest([path]), (path,), "reused file")
    output = UnitResult(
        unit, {path: str(payload["coverage"])}, warnings=strings(payload["warnings"])
    )
    for key in restore_refs:
        row = findings[key]
        candidate = row["candidate"]
        if not isinstance(candidate, dict):
            raise ValueError("Invalid checkpoint candidate")
        output.candidates.append(decode_candidate(candidate))
        location, reflection = row.get("location"), row.get("reflection")
        if isinstance(location, dict):
            output.locations.append(decode_location_decision(location))
        if isinstance(reflection, dict):
            output.reflections.append(decode_reflection(reflection))
    location_ids = {item.candidate_id for item in output.locations}
    reflection_ids = {item.candidate_id for item in output.reflections}
    for key in raw_refs:
        row = findings[key]
        candidate = row["candidate"]
        if not isinstance(candidate, dict):
            raise ValueError("Invalid raw candidate")
        output.raw_candidates.append(decode_candidate(candidate))
        location, reflection = row.get("location"), row.get("reflection")
        if isinstance(location, dict):
            decision = decode_location_decision(location)
            if decision.candidate_id not in location_ids:
                output.locations.append(decision)
                location_ids.add(decision.candidate_id)
        if isinstance(reflection, dict):
            reflected = decode_reflection(reflection)
            if reflected.candidate_id not in reflection_ids:
                output.reflections.append(reflected)
                reflection_ids.add(reflected.candidate_id)
    origin = payload["unit"]
    if not isinstance(origin, dict) or path not in decode_unit(origin).paths:
        raise ValueError("Checkpoint source unit does not contain file")
    return output
