"""将 settled 业务结果转为 journal 记录，最终摘要只保存引用。"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from ..contracts import (
    FindingCandidate,
    JsonObject,
    ReviewResult,
    ReviewUnit,
    UnitResult,
    stable_digest,
)
from .journal import SessionJournal
from .replay import object_field
from .results import encode_file
from .serialization import json_object, objects


class ResultRecorder:
    def __init__(self, journal: SessionJournal, fingerprints: dict[str, str]) -> None:
        self.journal, self.fingerprints = journal, fingerprints
        self.items: dict[str, JsonObject] = {}
        self.findings: dict[str, JsonObject] = {}

    def save_payload(
        self, path: str, payload: JsonObject, status: str, source_session: str | None = None
    ) -> None:
        self.journal.append(
            "review_item",
            {
                "file_path": path,
                "input_fingerprint": self.fingerprints[path],
                "status": status,
                "payload": payload,
                "source_session": source_session,
            },
            {"unit_id": object_field(payload, "unit")["unit_id"]},
        )
        self.items[path] = payload
        for row in objects(payload["findings"]):
            self.findings[str(row["key"])] = row

    def save_unit(self, output: UnitResult) -> None:
        for path in output.unit.paths:
            self.save_payload(
                path,
                encode_file(output, path),
                "completed" if output.coverage[path] == "reviewed" else "failed",
            )

    def _display(self, candidates: list[FindingCandidate]) -> list[JsonObject]:
        rows: list[JsonObject] = []
        for candidate in candidates:
            value = json_object(asdict(candidate))
            exact = next(
                (key for key, row in self.findings.items() if row["candidate"] == value), None
            )
            if exact:
                rows.append({"key": exact, "ref": exact})
                continue
            source = next(
                (
                    key
                    for key, row in self.findings.items()
                    if object_field(row, "candidate")["candidate_id"] == candidate.candidate_id
                ),
                None,
            )
            if source is None:
                raise ValueError("Final comment has no checkpoint source")
            key = uuid.uuid4().hex
            rows.append({"key": key, "ref": source, "candidate": value})
        return rows

    def save_batch(self, batch: ReviewUnit, candidates: list[FindingCandidate]) -> None:
        display = self._display(candidates)
        self.journal.append(
            "scan_batch_finalized",
            {
                "batch": json_object(asdict(batch)),
                "display": [row for row in display],
            },
        )
        for row in display:
            if row["key"] != row["ref"]:
                self.findings[str(row["key"])] = {**self.findings[str(row["ref"])], **row}

    def finish(self, result: ReviewResult) -> None:
        display = self._display(result.candidates)
        if any(row["key"] != row["ref"] for row in display):
            raise ValueError("Display rewrite must be checkpointed before session_end")
        self.journal.append(
            "session_end",
            {
                "status": result.status.value,
                "coverage": {path: state for path, state in result.coverage.items()},
                "display_refs": [row["key"] for row in display],
                "warnings": list(result.warnings),
                "project_summary": result.project_summary,
                "groups": [json_object(asdict(unit)) for unit in result.groups],
                "scan_batches": [json_object(asdict(unit)) for unit in result.scan_batches],
                "preparation_seconds": result.preparation_seconds,
                "review_seconds": result.review_seconds,
                "reused_units": result.reused_units,
                "rerun_units": result.rerun_units,
                "suppressed": dict(result.suppressed),
                "rewritten_content": dict(result.rewritten_content),
            },
        )


def file_fingerprints(metadata: JsonObject) -> dict[str, str]:
    return {str(item["path"]): stable_digest(item) for item in objects(metadata["files"])}
