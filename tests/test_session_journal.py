import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from agentscope.event import CustomEvent

from code_review_agent.project_paths import ProjectPaths
from code_review_agent.sessions.journal import JournalError, SessionJournal, read_records
from code_review_agent.sessions.records import SCHEMA, make_record, validate_record


def start(journal):
    return journal.append(
        "session_start",
        {
            "manifest": {},
            "input": {},
            "arguments": {},
            "configuration": {},
            "historical_usage": [],
        },
    )


def test_sdk_envelope_and_validation():
    value = {
        "model": "test",
        "request": {},
        "response": None,
        "status": "success",
        "duration_seconds": 0.5,
        "attempt_count": 1,
        "usage": {},
    }
    metadata = {"schema": SCHEMA, "session_id": "s", "seq": 1, "operation_id": "m", "stage": "plan"}
    record = make_record("model_call", value, metadata)
    validate_record(record, "s", 1)
    assert CustomEvent.model_validate(record).model_dump(mode="json") == record
    for bad in (
        {**record, "type": "MODEL_CALL_END"},
        {**record, "name": "SDK_DELTA"},
        {**record, "metadata": {**metadata, "schema": "future"}},
        {**record, "value": {**value, "status": "maybe"}},
        {**record, "value": {**value, "attempt_count": True}},
    ):
        with pytest.raises(ValueError):
            validate_record(bad, "s", 1)


def test_concurrent_append_durable_boundaries_and_redaction(tmp_path: Path):
    paths = ProjectPaths(tmp_path)
    journal = SessionJournal(paths, "s", ("private-key",))
    with patch("code_review_agent.sessions.journal.os.fsync", wraps=os.fsync) as sync:
        start(journal)
        assert sync.call_count == 1
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda index: journal.append(
                        "tool_execution",
                        {
                            "name": "read",
                            "arguments": {"index": index},
                            "tool_call_id": str(index),
                            "output": "Bearer private-key",
                            "status": "success",
                            "duration_seconds": 0,
                        },
                        {"stage": "review", "operation_id": str(index)},
                    ),
                    range(80),
                )
            )
        journal.append(
            "review_item",
            {
                "file_path": "a.py",
                "status": "completed",
                "input_fingerprint": "hash",
                "payload": {},
            },
        )
        assert sync.call_count == 2
    journal.close()
    records, damaged = read_records(paths, "s")
    assert len(records) == 82 and not damaged
    assert len({record["id"] for record in records}) == 82
    assert "private-key" not in journal.path.read_text()
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert list(journal.path.parent.iterdir()) == [journal.path]
    with pytest.raises(FileExistsError):
        SessionJournal(paths, "s")


@pytest.mark.parametrize("tail", [b'{"broken":', b"{}", b"\xe4\xb8"])
def test_uncommitted_tail_is_ignored_without_modification(tmp_path, tail):
    paths = ProjectPaths(tmp_path)
    journal = SessionJournal(paths, "s")
    start(journal)
    journal.close()
    with journal.path.open("ab") as stream:
        stream.write(tail)
    before = journal.path.read_bytes()
    records, damaged = read_records(paths, "s")
    assert len(records) == 1 and damaged
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("bad", [b"{}\n", b"garbage\n", b"\xe4\xb8\n"])
def test_committed_corruption_is_rejected(tmp_path, bad):
    paths = ProjectPaths(tmp_path)
    journal = SessionJournal(paths, "s")
    start(journal)
    journal.close()
    with journal.path.open("ab") as stream:
        stream.write(bad)
    with pytest.raises(ValueError, match="Corrupt committed"):
        read_records(paths, "s")


def test_read_does_not_create_or_touch_legacy(tmp_path):
    paths = ProjectPaths(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        read_records(paths, "missing")
    assert not (tmp_path / ".state").exists()
    legacy = tmp_path / ".state/sessions/old"
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_text(json.dumps({"old": True}))
    with pytest.raises(ValueError, match="legacy"):
        read_records(paths, "old")
    with pytest.raises(ValueError, match="Invalid session"):
        read_records(paths, "../escape")
    assert not legacy.with_suffix(".jsonl").exists()


def test_sync_failure_cannot_acknowledge_checkpoint(tmp_path):
    journal = SessionJournal(ProjectPaths(tmp_path), "s")
    with patch("code_review_agent.sessions.journal.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(JournalError, match="disk full"):
            start(journal)
    with pytest.raises(JournalError):
        journal.check()
    journal.close()
