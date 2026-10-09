import json
from dataclasses import asdict
from pathlib import Path

import pytest

from code_review_agent.config import load_settings
from code_review_agent.contracts import (
    FindingCandidate,
    ReflectionDecision,
    UsageRecord,
    stable_digest,
)
from code_review_agent.project_paths import ProjectPaths


def test_paths_reject_escape_before_creating_anything(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = ProjectPaths(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "link").symlink_to(outside, target_is_directory=True)
    for destination in ("../outside/report.json", outside / "report.json", "link/report.json"):
        with pytest.raises(ValueError, match="outside project"):
            paths.writable(destination)
    assert not list(outside.iterdir())


def test_candidate_roundtrip_and_stable_identity() -> None:
    value = {
        "path": "src/a.py",
        "content": "division by zero",
        "existing_code": "return 1 / x",
        "category": "bug",
        "severity": "high",
        "side": "right",
    }
    first = FindingCandidate.from_json(value, "revision-1")
    second = FindingCandidate.from_json(json.loads(json.dumps(value)), "revision-1")
    assert first == second
    assert json.loads(json.dumps(asdict(first)))["side"] == "right"
    assert FindingCandidate.from_json(value, "revision-2").candidate_id != first.candidate_id
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})
    with pytest.raises(ValueError, match="start_line"):
        FindingCandidate.from_json({**value, "start_line": True}, "revision")
    rejected = ReflectionDecision(
        first.candidate_id, "reject", "Reported incorrect by group filter"
    )
    assert rejected.decision == "reject" and rejected.evidence == ()
    unknown = UsageRecord("review", None, None)
    assert json.loads(json.dumps(asdict(unknown)))["input_tokens"] is None


def test_config_precedence_and_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"reviewer": {"model": "json-model", "api_key": "test-secret"}}))
    monkeypatch.setenv("CODE_REVIEW_REVIEWER_MODEL", "env-model")
    settings = load_settings(
        ProjectPaths(tmp_path), "config.json", {"reviewer": {"model": "arg-model"}}
    )
    assert settings.reviewer.model == "arg-model"
    assert settings.reflection.model == "arg-model"
    assert "test-secret" not in json.dumps(settings.public_json())
    assert settings.language == "zh"
    assert load_settings(ProjectPaths(tmp_path), "config.json").reviewer.model == "env-model"
    with pytest.raises(ValueError, match="Unknown configuration"):
        load_settings(ProjectPaths(tmp_path), overrides={"token_budget": 100})
    with pytest.raises(ValueError, match="integer"):
        load_settings(ProjectPaths(tmp_path), overrides={"max_concurrency": True})
    with pytest.raises(ValueError, match="chat_completions"):
        load_settings(ProjectPaths(tmp_path), overrides={"protocol": "responses"})
