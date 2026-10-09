import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from test_runtime import response, tool_call

from code_review_agent.application.review_service import review
from code_review_agent.config import ModelSettings, Settings
from code_review_agent.contracts import RunStatus
from code_review_agent.output.render import eligible_comments, output_json
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.review.grouping import validated_groups
from code_review_agent.runtime.model_factory import create_model
from code_review_agent.sessions.replay import load_session
from code_review_agent.sessions.store import SessionStore, redact


def configured():
    model = ModelSettings(
        base_url="https://example.invalid/v1", model="test", api_key="secret-key", retries=0
    )
    return replace(Settings(), reviewer=model, reflection=model, effort="low")


def scripted_factory(requests, *, reject=False, fail_second=False, snippet="return 1 / x"):
    def factory(settings, stage, usage):
        model = create_model(settings, stage, usage)
        original = model.client
        count = 0

        async def handler(request):
            nonlocal count
            await original.close()
            count += 1
            body = json.loads(request.content)
            requests.append((stage, body))
            if stage == "plan":
                return response(
                    {"role": "assistant", "content": '{"summary":"review file","checkpoints":[]}'}
                )
            if stage.startswith("reflection"):
                return response(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            tool_call(
                                "report_incorrect_comments" if reject else "approve_all_comments",
                                {"analysis": ["c-0: contradicted by diff"], "comment_ids": ["c-0"]}
                                if reject
                                else {},
                                "filter",
                            )
                        ],
                    }
                )
            if stage == "location.relocation":
                return response({"role": "assistant", "content": "```python\nreturn 1 / x\n```"})
            if stage == "scan.summary":
                return response({"role": "assistant", "content": "### Top Issues\na.py"})
            if fail_second and stage.endswith("round2"):
                return httpx.Response(500, json={"error": {"message": "failed"}})
            if count == 1:
                calls = [
                    tool_call(
                        "code_comment",
                        {
                            "comments": [
                                {
                                    "path": "a.py",
                                    "content": "division by zero",
                                    "existing_code": snippet,
                                    "category": "bug",
                                    "severity": "high",
                                }
                            ]
                        },
                        "comment",
                    )
                ]
            else:
                calls = [
                    tool_call("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]}, "done")
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


def test_group_partition_validates_and_completes_omissions():
    paths = ("a.py", "b.py", "c.py")
    units = validated_groups([{"label": "related", "files": ["b.py", "a.py"]}], paths, 1)
    assert sorted(path for unit in units for path in unit.paths) == sorted(paths)
    assert all(len(unit.paths) == 1 for unit in units)
    for value in (
        [{"files": ["a.py", "a.py"]}],
        [{"files": ["../bad.py"]}],
        [{"files": ["a.py"]}, {"files": ["a.py"]}],
    ):
        with pytest.raises(ValueError):
            validated_groups(value, paths, 8)


@pytest.mark.asyncio
async def test_full_scan_final_eligibility_and_resume(tmp_path: Path):
    paths = ProjectPaths(tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "a.py").write_text("return 1 / x\n")
    requests = []
    result = await review(
        paths, configured(), repo, mode="scan", model_factory=scripted_factory(requests)
    )
    assert result.status == RunStatus.COMPLETED and len(eligible_comments(result)) == 1
    assert result.coverage == {"a.py": "reviewed"}
    assert result.project_summary
    assert len(result.candidates) == len(result.locations) == 1 and not result.reflections
    view = load_session(paths, result.manifest.session_id)
    assert view.reusable and "histories" not in json.dumps(view.start)
    assert view.result().candidates == result.candidates
    assert view.result().tool_calls == result.tool_calls
    assert not (paths.root / ".state/sessions" / result.manifest.session_id).exists()
    rerun_requests = []
    resumed = await review(
        paths,
        configured(),
        repo,
        resume=result.manifest.session_id,
        model_factory=scripted_factory(rerun_requests),
    )
    assert resumed.status == RunStatus.COMPLETED
    assert all(not stage.startswith("review.") for stage, _ in rerun_requests)
    assert resumed.historical_usage == result.usage
    assert len(resumed.usage) < len(result.usage)
    with pytest.raises(ValueError, match="changed"):
        await review(
            paths, replace(configured(), language="en"), repo, resume=result.manifest.session_id
        )
    (repo / "a.py").write_text("changed\n")
    with pytest.raises(ValueError, match="changed"):
        await review(paths, configured(), repo, resume=result.manifest.session_id)


@pytest.mark.asyncio
async def test_filtered_candidates_stay_in_diagnostics_and_failure_keeps_earlier_result(
    tmp_path: Path,
):
    paths = ProjectPaths(tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "a.py").write_text("return 1 / x\n")
    from test_inputs import git

    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.py").write_text("return x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("return 1 / x\n")
    git(repo, "commit", "-qam", "change")
    rejected = await review(
        paths,
        configured(),
        repo,
        from_ref=base,
        to_ref="HEAD",
        model_factory=scripted_factory([], reject=True),
    )
    assert rejected.status == RunStatus.COMPLETED and rejected.candidates
    assert not eligible_comments(rejected)
    failed = await review(
        paths,
        replace(configured(), effort="medium"),
        repo,
        from_ref=base,
        to_ref="HEAD",
        model_factory=scripted_factory([], fail_second=True),
    )
    assert failed.status == RunStatus.PARTIAL and len(failed.candidates) == 1
    assert failed.coverage["a.py"] == "partial"
    value = output_json(failed, configured().public_json(), ("secret-key",))
    assert "secret-key" not in json.dumps(value)


def test_atomic_store_tail_and_secret_redaction(tmp_path: Path):
    store = SessionStore(ProjectPaths(tmp_path), "test", ("secret-key",))
    store.write(
        "checkpoint.json",
        {
            "authorization": "Bearer secret-key",
            "message": "https://user:password@example.com/?api_key=secret-key",
        },
    )
    raw = (store.root / "checkpoint.json").read_text()
    assert "secret-key" not in raw and "password" not in raw
    assert not list(store.root.glob("*.tmp"))
    store.append({"stage": "one"})
    with (store.root / "events.jsonl").open("a") as stream:
        stream.write('{"broken":')
    events, damaged = store.events()
    assert len(events) == 1 and damaged
    assert "secret" not in str(redact({"api_key": "secret"}))
