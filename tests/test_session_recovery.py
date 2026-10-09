import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI
from test_inputs import git
from test_review_sessions import configured
from test_runtime import response, tool_call

from code_review_agent.application.benchmark_service import BenchmarkService
from code_review_agent.application.review_service import review
from code_review_agent.application.session_service import list_sessions, show_session
from code_review_agent.contracts import RunStatus, stable_digest
from code_review_agent.output.render import eligible_comments
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.runtime.model_factory import create_model
from code_review_agent.sessions.journal import JournalError, journal_path
from code_review_agent.sessions.replay import load_session


def repo_with_change(root: Path):
    repo = root / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    for path in ("a.py", "b.py"):
        (repo / path).write_text("return x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    base = git(repo, "rev-parse", "HEAD")
    for path in ("a.py", "b.py"):
        (repo / path).write_text("return 1 / x\n")
    git(repo, "commit", "-qam", "change")
    return repo, base


def multi_factory(requests, *, partial_group=False, findings=False, duplicates=1):
    ids = {stable_digest([path]): [path] for path in ("a.py", "b.py")}
    ids[stable_digest(["a.py", "b.py"])] = ["a.py", "b.py"]

    def factory(settings, stage, usage):
        model = create_model(settings, stage, usage)
        original, count = model.client, 0

        async def handler(request):
            nonlocal count
            await original.close()
            count += 1
            body = json.loads(request.content)
            requests.append((stage, model.unit_id, body))
            if stage == "grouping":
                return response(
                    {"role": "assistant", "content": json.dumps([{"files": ["a.py", "b.py"]}])}
                )
            if stage == "scan.dedup":
                return response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "groups": [
                                    {
                                        "members": ["c-0", "c-1"],
                                        "merged_content": "shared division by zero",
                                    },
                                ]
                            }
                        ),
                    }
                )
            if stage == "scan.summary":
                return response({"role": "assistant", "content": "One shared issue"})
            if stage.startswith("reflection"):
                return response(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call("approve_all_comments", {}, "approve")],
                    }
                )
            paths = ids[model.unit_id]
            if findings and count == 1:
                calls = [
                    tool_call(
                        "code_comment",
                        {
                            "comments": [
                                {
                                    "path": path,
                                    "content": "division by zero",
                                    "existing_code": "return 1 / x",
                                    "category": "bug",
                                    "severity": "high",
                                }
                                for path in paths
                                for _ in range(duplicates)
                            ]
                        },
                        "comments",
                    )
                ]
            else:
                covered = ["a.py"] if partial_group and len(paths) == 2 else paths
                calls = [
                    tool_call("task_done", {"state": "DONE", "reviewed_paths": covered}, "done")
                ]
            return response({"role": "assistant", "content": None, "tool_calls": calls})

        model.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return model

    return factory


def truncate_after_first_completed(paths, session_id):
    path = journal_path(paths, session_id)
    lines = path.read_bytes().splitlines(keepends=True)
    keep = next(
        index + 1
        for index, line in enumerate(lines)
        if json.loads(line)["name"] == "review_item"
        and json.loads(line)["value"]["status"] == "completed"
    )
    path.write_bytes(b"".join(lines[:keep]) + b'{"type":"CUSTOM",')


@pytest.mark.asyncio
async def test_diff_partial_group_reuses_one_file_and_regroups_only_pending(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, base = repo_with_change(tmp_path)
    settings = replace(configured(), group_min_files=2)
    initial = await review(
        paths,
        settings,
        repo,
        from_ref=base,
        to_ref="HEAD",
        model_factory=multi_factory([], partial_group=True),
    )
    assert initial.status == RunStatus.PARTIAL
    view = load_session(paths, initial.manifest.session_id)
    assert sorted(view.reusable) == ["a.py"] and not view.reusable["a.py"].candidates
    requests = []
    resumed = await review(
        paths,
        settings,
        repo,
        resume=initial.manifest.session_id,
        model_factory=multi_factory(requests),
    )
    assert resumed.status == RunStatus.COMPLETED
    assert [unit for stage, unit, _ in requests if stage.startswith("review")] == [
        stable_digest(["b.py"])
    ]
    assert not any(stage == "grouping" for stage, _, _ in requests)
    # 新 Agent 的首次请求只有 system/user，没有恢复旧的 Assistant/Tool 历史。
    assert all(
        [msg["role"] for msg in body["messages"]] == ["system", "user"] for _, _, body in requests
    )
    child = load_session(paths, resumed.manifest.session_id)
    assert child.items["a.py"]["status"] == "reused"
    assert child.result().coverage == resumed.coverage
    assert not (paths.root / ".state/sessions" / resumed.manifest.session_id).exists()


@pytest.mark.asyncio
async def test_hard_interruption_without_end_and_self_contained_resume_chain(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, base = repo_with_change(tmp_path)
    initial = await review(
        paths, configured(), repo, from_ref=base, to_ref="HEAD", model_factory=multi_factory([])
    )
    truncate_after_first_completed(paths, initial.manifest.session_id)
    before = journal_path(paths, initial.manifest.session_id).read_bytes()
    parent = load_session(paths, initial.manifest.session_id)
    assert parent.end is None and parent.damaged_tail and len(parent.reusable) == 1
    requests = []
    resumed = await review(
        paths,
        configured(),
        repo,
        resume=initial.manifest.session_id,
        model_factory=multi_factory(requests),
    )
    assert resumed.status == RunStatus.COMPLETED and len(requests) == 1
    assert journal_path(paths, initial.manifest.session_id).read_bytes() == before
    assert resumed.historical_usage == parent.usage
    journal_path(paths, initial.manifest.session_id).unlink()
    final_requests = []
    final = await review(
        paths,
        configured(),
        repo,
        resume=resumed.manifest.session_id,
        model_factory=multi_factory(final_requests),
    )
    assert final.status == RunStatus.COMPLETED and not final_requests
    assert final.historical_usage == resumed.historical_usage + resumed.usage
    assert sorted(show_session(paths, final.manifest.session_id)["completed_files"]) == [
        "a.py",
        "b.py",
    ]


@pytest.mark.asyncio
async def test_scan_cross_file_merge_is_recoverable_mid_checkpoint(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, _ = repo_with_change(tmp_path)
    settings = replace(configured(), batch_size=2, dedup_min_comments=2)
    initial = await review(
        paths, settings, repo, mode="scan", model_factory=multi_factory([], findings=True)
    )
    assert initial.status == RunStatus.COMPLETED and len(initial.candidates) == 1
    view = load_session(paths, initial.manifest.session_id)
    assert len(view.reusable) == 2 and all(
        len(unit.candidates) == 1 for unit in view.reusable.values()
    )
    assert view.result().candidates == initial.candidates
    assert eligible_comments(view.result()) == eligible_comments(initial)
    assert view.result().raw_candidates == initial.raw_candidates
    truncate_after_first_completed(paths, initial.manifest.session_id)
    requests = []
    resumed = await review(
        paths,
        settings,
        repo,
        resume=initial.manifest.session_id,
        model_factory=multi_factory(requests, findings=True),
    )
    assert resumed.status == RunStatus.COMPLETED and len(resumed.candidates) == 1
    assert len([unit for stage, unit, _ in requests if stage.startswith("review")]) == 2
    assert any(stage == "scan.dedup" for stage, _, _ in requests)
    assert (
        load_session(paths, resumed.manifest.session_id).result().candidates == resumed.candidates
    )


@pytest.mark.asyncio
async def test_identity_mismatch_and_missing_git_object_precede_models(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, base = repo_with_change(tmp_path)
    initial = await review(
        paths, configured(), repo, from_ref=base, to_ref="HEAD", model_factory=multi_factory([])
    )
    requests = []
    for settings in (
        replace(configured(), language="en"),
        replace(configured(), reviewer=replace(configured().reviewer, model="other")),
    ):
        with pytest.raises(ValueError, match="identity changed"):
            await review(
                paths,
                settings,
                repo,
                resume=initial.manifest.session_id,
                model_factory=multi_factory(requests),
            )
    git(repo, "update-ref", "refs/heads/main", base)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "prune", "--expire=now")
    with pytest.raises(ValueError):
        await review(
            paths,
            configured(),
            repo,
            resume=initial.manifest.session_id,
            model_factory=multi_factory(requests),
        )
    assert not requests


@pytest.mark.asyncio
async def test_storage_failure_is_not_successful_review(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, base = repo_with_change(tmp_path)
    with patch("code_review_agent.sessions.journal.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(JournalError):
            await review(
                paths,
                configured(),
                repo,
                from_ref=base,
                to_ref="HEAD",
                model_factory=multi_factory([]),
            )


def test_legacy_listing_is_read_only_and_real_history_stops_early(tmp_path):
    paths = ProjectPaths(tmp_path)
    assert list_sessions(paths)["sessions"] == [] and not (tmp_path / ".state").exists()
    old = tmp_path / ".state/sessions/old"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{}")
    assert list_sessions(paths)["skipped"][0]["session_id"] == "old"
    with pytest.raises(ValueError, match="legacy"):
        show_session(paths, "old")


@pytest.mark.asyncio
async def test_obsolete_history_pool_does_not_read_dataset_or_call_judge(tmp_path):
    requests = []
    service = BenchmarkService(
        ProjectPaths(tmp_path), configured(), "test", model_factory=multi_factory(requests)
    )
    assert not hasattr(service, "prepare_pool")
    assert not hasattr(service, "run_module")
    assert not requests


@pytest.mark.asyncio
async def test_root_commit_identity_and_no_source_snapshot(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, root_sha = repo_with_change(tmp_path)
    initial = await review(
        paths, configured(), repo, commit=root_sha, model_factory=multi_factory([])
    )
    view = load_session(paths, initial.manifest.session_id)
    assert view.start["input"]["base_sha"] is None
    assert view.start["input"]["head_sha"] == root_sha
    assert all(
        not ({"content", "old_content", "diff", "hunks"} & set(item))
        for item in view.start["input"]["files"]
    )
    assert {record["name"] for record in view.records} <= {
        "session_start",
        "model_call",
        "tool_execution",
        "review_item",
        "session_end",
    }
    requests = []
    resumed = await review(
        paths,
        configured(),
        repo,
        resume=initial.manifest.session_id,
        model_factory=multi_factory(requests),
    )
    assert resumed.status == RunStatus.COMPLETED and not requests


@pytest.mark.asyncio
async def test_scan_context_change_outside_selected_target_rejects_reuse(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, _ = repo_with_change(tmp_path)
    settings = replace(configured(), include=("a.py",))
    result = await review(paths, settings, repo, mode="scan", model_factory=multi_factory([]))
    assert list(result.coverage) == ["a.py"]
    (repo / "b.py").write_text("changed context\n")
    requests = []
    with pytest.raises(ValueError, match="content changed"):
        await review(
            paths,
            settings,
            repo,
            resume=result.manifest.session_id,
            model_factory=multi_factory(requests),
        )
    assert not requests


@pytest.mark.asyncio
async def test_corrupt_checkpoint_reference_is_rejected(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo, base = repo_with_change(tmp_path)
    initial = await review(
        paths, configured(), repo, from_ref=base, to_ref="HEAD", model_factory=multi_factory([])
    )
    path = journal_path(paths, initial.manifest.session_id)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = next(row for row in rows if row["name"] == "review_item")
    row["value"]["payload"]["restore_refs"] = ["missing"]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="Unknown file finding"):
        load_session(paths, initial.manifest.session_id)


@pytest.mark.asyncio
async def test_source_credential_redaction_does_not_change_recovery_identity(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "a.py").write_text(
        'token = "secret-key"\nurl = "https://user:password@example.invalid/?api_key=secret-key"\n'
    )
    initial = await review(paths, configured(), repo, mode="scan", model_factory=multi_factory([]))
    raw = journal_path(paths, initial.manifest.session_id).read_text()
    assert "secret-key" not in raw and "user:password" not in raw
    assert "[REDACTED]" in raw
    requests = []
    resumed = await review(
        paths,
        configured(),
        repo,
        resume=initial.manifest.session_id,
        model_factory=multi_factory(requests),
    )
    assert resumed.status == RunStatus.COMPLETED and not requests


@pytest.mark.asyncio
async def test_scan_same_file_dedup_preserves_raw_location_diagnostics_on_replay(tmp_path):
    paths = ProjectPaths(tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "a.py").write_text("return 1 / x\n")
    settings = replace(configured(), dedup_min_comments=2)
    initial = await review(
        paths,
        settings,
        repo,
        mode="scan",
        model_factory=multi_factory([], findings=True, duplicates=2),
    )
    assert (
        len(initial.candidates) == 1 and len(initial.raw_candidates) == len(initial.locations) == 2
    )
    view = load_session(paths, initial.manifest.session_id)
    assert view.result().raw_candidates == initial.raw_candidates
    assert view.result().locations == initial.locations
    assert len(view.reusable["a.py"].candidates) == 1
    resumed = await review(
        paths, settings, repo, resume=initial.manifest.session_id, model_factory=multi_factory([])
    )
    assert len(resumed.candidates) == 1 and len(resumed.locations) == 2
