"""回放业务结果，不重放工具、不恢复 Agent 对话。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from ..contracts import (
    InputMode,
    JsonObject,
    ReviewResult,
    RunManifest,
    RunStatus,
    UnitResult,
    UsageRecord,
    stable_digest,
)
from ..project_paths import ProjectPaths
from .journal import read_records
from .results import decode_file
from .serialization import (
    decode_candidate,
    decode_location_decision,
    decode_reflection,
    decode_unit,
    decode_usage,
    objects,
    strings,
)


def object_field(value: JsonObject, key: str) -> JsonObject:
    item = value[key]
    if not isinstance(item, dict):
        raise ValueError(f"Expected object: {key}")
    return item


@dataclass
class SessionView:
    start: JsonObject
    records: list[JsonObject]
    damaged_tail: bool
    items: dict[str, JsonObject] = field(default_factory=dict)
    findings: dict[str, JsonObject] = field(default_factory=dict)
    batches: list[JsonObject] = field(default_factory=list)
    end: JsonObject | None = None

    @property
    def manifest(self) -> JsonObject:
        return object_field(self.start, "manifest")

    @property
    def usage(self) -> list[UsageRecord]:
        return [
            decode_usage(object_field(object_field(record, "value"), "usage"))
            for record in self.records
            if record["name"] == "model_call"
        ]

    @property
    def historical_usage(self) -> list[UsageRecord]:
        return [decode_usage(item) for item in objects(self.start["historical_usage"])]

    @property
    def reusable(self) -> dict[str, UnitResult]:
        return {
            path: decode_file(path, object_field(item, "payload"))
            for path, item in self.items.items()
            if item["status"] in {"completed", "reused"}
        }

    def result(self) -> ReviewResult:
        manifest = self.manifest
        result = ReviewResult(
            RunStatus(str(self.end["status"])) if self.end else RunStatus.PARTIAL,
            RunManifest(
                str(manifest["session_id"]),
                str(manifest["input_identity"]),
                str(manifest["config_identity"]),
                str(manifest["resource_identity"]),
                InputMode(str(manifest["mode"])),
                str(manifest["repo"]),
                cast(str | None, manifest.get("base_sha")),
                cast(str | None, manifest.get("head_sha")),
                schema_version=2,
                parent_session=cast(str | None, manifest.get("parent_session")),
            ),
            usage=self.usage,
            historical_usage=self.historical_usage,
        )
        result.coverage = dict.fromkeys(strings(self.start["targets"]), "pending")
        raw_refs: list[str] = []
        restore_refs: list[str] = []
        for path, item in self.items.items():
            payload = object_field(item, "payload")
            result.coverage[path] = str(payload["coverage"])
            result.warnings.extend(strings(payload["warnings"]))
            raw_refs.extend(strings(payload["raw_refs"]))
            restore_refs.extend(strings(payload["restore_refs"]))
        if self.end:
            display = strings(self.end["display_refs"])
        elif self.batches:
            display = [
                str(row["key"]) for batch in self.batches for row in objects(batch["display"])
            ]
            finalized = {
                path
                for batch in self.batches
                for path in decode_unit(object_field(batch, "batch")).paths
            }
            display.extend(
                key
                for path, item in self.items.items()
                if path not in finalized
                for key in strings(object_field(item, "payload")["restore_refs"])
            )
        else:
            display = restore_refs
        if not self.end and manifest["mode"] == "scan":
            result.warnings.append(
                "Interrupted Scan: non-finalized batch findings are recovery inputs, not final deduplicated comments"
            )
        result.raw_candidates = (
            [decode_candidate(object_field(self.findings[key], "candidate")) for key in raw_refs]
            if manifest["mode"] == "scan"
            else []
        )
        result.candidates = [
            decode_candidate(object_field(self.findings[key], "candidate")) for key in display
        ]
        seen: set[str] = set()
        for key in display + raw_refs:
            row = self.findings[key]
            candidate_id = str(object_field(row, "candidate")["candidate_id"])
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            location, reflection = row.get("location"), row.get("reflection")
            if isinstance(location, dict):
                result.locations.append(decode_location_decision(location))
            if isinstance(reflection, dict):
                result.reflections.append(decode_reflection(reflection))
        result.tool_calls = [
            record for record in self.records if record["name"] == "tool_execution"
        ]
        if self.end:
            result.coverage = {
                path: str(state) for path, state in object_field(self.end, "coverage").items()
            }
            result.warnings = strings(self.end.get("warnings", []))
            result.project_summary = str(self.end.get("project_summary", ""))
            result.groups = [decode_unit(item) for item in objects(self.end.get("groups", []))]
            result.scan_batches = [
                decode_unit(item) for item in objects(self.end.get("scan_batches", []))
            ]
            for key in ("preparation_seconds", "review_seconds", "reused_units", "rerun_units"):
                setattr(result, key, self.end.get(key, 0))
            result.suppressed = {
                key: str(value) for key, value in object_field(self.end, "suppressed").items()
            }
            result.rewritten_content = {
                key: str(value)
                for key, value in object_field(self.end, "rewritten_content").items()
            }
        if self.damaged_tail:
            result.warnings.append("Incomplete final journal line ignored")
        return result


def load_session(paths: ProjectPaths, session_id: str) -> SessionView:
    records, damaged = read_records(paths, session_id)
    if not records or records[0]["name"] != "session_start":
        raise ValueError("Session has no committed startup identity")
    view = SessionView(object_field(records[0], "value"), records, damaged)
    if view.manifest.get("session_id") != session_id:
        raise ValueError("Startup session identity mismatch")
    frozen_input = object_field(view.start, "input")
    if stable_digest(frozen_input) != view.manifest.get("input_identity"):
        raise ValueError("Startup input identity mismatch")
    fingerprints = {
        str(item["path"]): stable_digest(item) for item in objects(frozen_input["files"])
    }
    target_paths = set(strings(view.start["targets"]))
    if not target_paths <= set(fingerprints):
        raise ValueError("Selected targets missing from input manifest")
    for record in records[1:]:
        if view.end is not None:
            raise ValueError("Records after session_end")
        value = object_field(record, "value")
        if record["name"] == "session_start":
            raise ValueError("Repeated session_start")
        if record["name"] == "review_item":
            path = str(value["file_path"])
            if path not in target_paths:
                raise ValueError("Checkpoint file outside selected targets")
            if value["input_fingerprint"] != fingerprints[path]:
                raise ValueError("Checkpoint input fingerprint mismatch")
            payload = object_field(value, "payload")
            output = decode_file(path, payload)
            if value["status"] in {"completed", "reused"} and output.coverage[path] != "reviewed":
                raise ValueError("Reusable file has incomplete coverage")
            for row in objects(payload["findings"]):
                key = str(row["key"])
                if key in view.findings:
                    raise ValueError("Repeated finding key")
                candidate = decode_candidate(object_field(row, "candidate"))
                if candidate.path != path:
                    raise ValueError("Finding source does not match checkpoint file")
                view.findings[key] = row
            view.items[path] = value
        elif record["name"] == "scan_batch_finalized":
            batch = decode_unit(object_field(value, "batch"))
            if not set(batch.paths) <= set(view.items):
                raise ValueError("Scan batch references uncommitted file results")
            for row in objects(value["display"]):
                ref, key = str(row["ref"]), str(row["key"])
                if ref not in view.findings:
                    raise ValueError("Unknown Scan finding reference")
                if key != ref:
                    if key in view.findings:
                        raise ValueError("Repeated canonical finding key")
                    view.findings[key] = {**view.findings[ref], **row}
                    decode_candidate(object_field(view.findings[key], "candidate"))
            view.batches.append(value)
        elif record["name"] == "session_end":
            RunStatus(str(value["status"]))
            if any(key not in view.findings for key in strings(value["display_refs"])):
                raise ValueError("Unknown final finding reference")
            view.end = value
    return view
