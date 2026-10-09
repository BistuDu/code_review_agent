"""共同任务、编排恢复与独立评分测试。"""

import asyncio
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from test_evaluation import row
from test_inputs import git
from test_review_sessions import configured, scripted_factory
from test_scripts_benchmark import execute

from code_review_agent.application.benchmark_service import BenchmarkService
from code_review_agent.application.review_service import review
from code_review_agent.evaluation.config import ClaudeConfig
from code_review_agent.evaluation.repositories import RepositoryPool
from code_review_agent.evaluation.reviewers.claude import ClaudeReviewer
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.sessions.replay import load_session
from code_review_agent.sessions.serialization import objects


@pytest.fixture
def service(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    git(remote, "config", "user.name", "Test")
    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "commit.gpgsign", "false")
    (remote / "a.py").write_text("return x\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "source")
    source = git(remote, "rev-parse", "HEAD")
    (remote / "a.py").write_text("return 1 / x\n")
    git(remote, "commit", "-qam", "target")
    target = git(remote, "rev-parse", "HEAD")
    root = tmp_path / "project"
    root.mkdir()
    rows = [row("org/one", "Python"), row("org/two", "Go")]
    for record in rows:
        record.update(pr_source_commit=source, pr_target_commit=target)
    (root / "mirror.json").write_text(json.dumps(rows))
    monkeypatch.setattr(RepositoryPool, "_clone_url", lambda *_: str(remote))
    requests = []
    settings = configured()
    settings = replace(settings, judge=settings.reviewer)
    benchmark = BenchmarkService(
        ProjectPaths(root),
        settings,
        "comparison",
        scripted_factory(requests),
        repository_concurrency=2,
        claude_config=ClaudeConfig("test", "https://example.invalid/anthropic", "secret-key"),
    )
    benchmark.prepare(repo_count=2, mirror="mirror.json")
    return benchmark, requests


async def test_production_adapter_same_task_resume_and_score_without_git(service, monkeypatch):
    benchmark, requests = service
    observed = []
    progress = []
    benchmark.progress = progress.append

    async def claude(self, task, job_id, pool, pr):
        observed.append((task.identity, task.payload, git(task.path, "rev-parse", "HEAD")))
        return {
            "status": "completed",
            "comments": [
                {
                    "candidate_id": job_id,
                    "path": "a.py",
                    "side": "right",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "division by zero",
                }
            ],
            "usage": [
                {
                    "stage": "review.claude",
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cached_tokens": 0,
                    "duration_seconds": 0.1,
                }
            ],
            "review_seconds": 0.1,
        }

    monkeypatch.setattr(ClaudeReviewer, "run", claude)
    await benchmark.prepare_repositories(repo="org/one", limit=1)
    plan = await benchmark.run_reviews(repo="org/one", limit=1)
    selection = benchmark.dataset()["selection"]
    assert selection["selected_repos"] == ["org/one"]
    assert selection["final_prs"] == plan["selected_prs"]
    assert selection["selected_pr_count"] == 1
    assert any(message.startswith("[prepare 1/1]") for message in progress)
    assert any("[review] project" in message and "completed" in message for message in progress)
    assert any("[review] claude" in message and "completed" in message for message in progress)
    jobs = objects(plan["jobs"])
    assert [job["reviewer"] for job in jobs] == ["project", "claude"]
    assert all(job["status"] == "completed" for job in jobs)
    assert jobs[0]["task_identity"] == jobs[1]["task_identity"] == observed[0][0]
    assert "references" not in observed[0][1] and "label" not in observed[0][1]
    assert observed[0][2] == jobs[0]["repository"]["actual_head"]
    assert jobs[0]["repository"]["cleanup_status"] == "clean"
    assert any(stage.startswith("reflection") for stage, _ in requests)
    assert list((benchmark.paths.root / ".state/sessions").glob("*.jsonl"))
    assert not (benchmark.store.root / "events.jsonl").exists()
    before = len(requests)

    def forbidden(*args, **kwargs):
        raise AssertionError("completed results and score/report must not prepare repositories")

    monkeypatch.setattr(RepositoryPool, "review_checkout", forbidden)
    await benchmark.run_reviews()
    assert len(requests) == before and len(observed) == 1
    score = await benchmark.score_reviews(mock=True)
    assert [row["matched"] for row in objects(score["scores"])] == [1, 1]
    report = benchmark.report_reviews()
    assert any(message.startswith("[score]") for message in progress)
    assert any(message.startswith("[report] 完成：") for message in progress)
    assert report["systems"]["project"]["micro"]["f1"] == 1
    assert report["measurement_status"] == "incomplete" and report["flow_test_only"]
    second = await benchmark.score_reviews(mock=True, k=2)
    assert second["score_id"] != score["score_id"]
    assert (benchmark.store.root / f"scores/{score['score_id']}.json").exists()
    assert len(requests) == before
    changed = BenchmarkService(
        benchmark.paths,
        replace(benchmark.settings, background="changed"),
        "comparison",
        claude_config=benchmark.claude_config,
        repository_concurrency=2,
    )
    with pytest.raises(ValueError, match="changed"):
        await changed.run_reviews()


async def test_repetitions_repo_serial_parallel_and_failed_retry(service, monkeypatch):
    benchmark, _ = service
    active = Counter()
    peak = total = 0
    order = []
    calls = Counter()

    async def run(task, job_id, reviewer):
        nonlocal peak, total
        repo = task.path.name
        calls[job_id] += 1
        active[repo] += 1
        total += 1
        peak = max(peak, total)
        assert active[repo] == 1
        head = git(task.path, "rev-parse", "HEAD")
        await asyncio.sleep(0.03)
        assert head == git(task.path, "rev-parse", "HEAD") == task.target
        order.append((repo, reviewer))
        active[repo] -= 1
        total -= 1
        if reviewer == "claude" and calls[job_id] == 1:
            return {"status": "failed", "comments": [], "usage": [], "error": "simulated failure"}
        return {"status": "completed", "comments": [], "usage": [], "review_seconds": 0.03}

    async def project(paths, settings, task, job_id, factory, *, deadline=None):
        assert deadline is not None
        return await run(task, job_id, "project")

    async def claude(self, task, job_id, pool, pr):
        return await run(task, job_id, "claude")

    monkeypatch.setattr("code_review_agent.application.benchmark_service.run_project", project)
    monkeypatch.setattr(ClaudeReviewer, "run", claude)
    first = await benchmark.run_reviews(repetitions=3)
    jobs = objects(first["jobs"])
    assert len(jobs) == 12 and len(set(job["job_id"] for job in jobs)) == 12
    assert peak == 2
    assert sum(job["status"] == "failed" for job in jobs) == 6
    score = await benchmark.score_reviews(mock=True)
    report = benchmark.report_reviews()
    assert report["systems"]["claude"]["micro"]["generated"] is None
    assert report["systems"]["claude"]["micro"]["recall"] == 0
    assert report["systems"]["claude"]["micro"]["completion_rate"] == 0
    assert report["systems"]["claude"]["micro"]["f1"] is None
    assert report["systems"]["claude"]["efficiency"]["review_tokens"] is None
    final = await benchmark.run_reviews(repetitions=3)
    assert all(job["status"] == "completed" for job in objects(final["jobs"]))
    assert all(
        calls[str(job["job_id"])] == (1 if job["reviewer"] == "project" else 2) for job in jobs
    )
    assert all(
        len(job["attempts"]) == (1 if job["reviewer"] == "project" else 2)
        for job in objects(final["jobs"])
    )
    assert (
        benchmark.report_reviews(score_id=str(score["score_id"]))["systems"]["claude"]["micro"][
            "completion_rate"
        ]
        == 0
    )


async def test_direct_resume_preserves_strategy_and_rejects_change(service):
    benchmark, requests = service
    pr = benchmark.selected_prs("org/one")[0]
    async with RepositoryPool(benchmark.paths).review_checkout(pr) as prepared:
        result = await review(
            benchmark.paths,
            benchmark.settings,
            prepared.path,
            from_ref=pr.base_sha,
            to_ref=pr.head_sha,
            range_strategy="direct",
            model_factory=scripted_factory(requests),
        )
        view = load_session(benchmark.paths, result.manifest.session_id)
        assert view.start["input"]["range_strategy"] == "direct"
        before = len(requests)
        reused = await review(
            benchmark.paths,
            benchmark.settings,
            prepared.path,
            resume=result.manifest.session_id,
            model_factory=scripted_factory(requests),
        )
        assert reused.reused_units == 1 and len(requests) == before
        with pytest.raises(ValueError, match="strategy"):
            await review(
                benchmark.paths,
                benchmark.settings,
                prepared.path,
                resume=result.manifest.session_id,
                range_strategy="merge-base",
            )


def test_removed_cli_parameters_are_rejected_and_new_help():
    help_result = execute("benchmark.py", "--help")
    assert help_result.returncode == 0
    assert "project,claude,both" in help_result.stdout and "--repetitions" in help_result.stdout
    for values in [
        ("--stage", "freeze"),
        ("--stage", "review", "--reviewer", "baseline"),
        ("--stage", "review", "--experiment", "reflection"),
    ]:
        invalid = execute("benchmark.py", *values)
        assert invalid.returncode == 2 and not invalid.stdout
    assert execute("main.py", "--reviewer", "baseline").returncode == 2


async def test_same_run_rejects_concurrent_execution(service):
    benchmark, _ = service
    with benchmark.exclusive():
        with pytest.raises(ValueError, match="already active"):
            await benchmark.run_reviews()
        with pytest.raises(ValueError, match="already active"):
            await benchmark.score_reviews(mock=True)


async def test_normal_old_false_and_missing_baseline_resume_but_true_rejected(service):
    benchmark, requests = service
    pr = benchmark.selected_prs("org/one")[0]
    async with RepositoryPool(benchmark.paths).review_checkout(pr) as prepared:
        initial = await review(
            benchmark.paths,
            benchmark.settings,
            prepared.path,
            from_ref=pr.base_sha,
            to_ref=pr.head_sha,
            model_factory=scripted_factory(requests),
        )
        path = benchmark.paths.root / f".state/sessions/{initial.manifest.session_id}.jsonl"
        original = path.read_text()
        rows = [json.loads(line) for line in original.splitlines()]
        start = rows[0]
        # SDK 自定义事件的业务数据位于 value。
        data = start["value"]
        assert data["arguments"]["baseline"] is False
        del data["arguments"]["baseline"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        reused = await review(
            benchmark.paths,
            benchmark.settings,
            prepared.path,
            resume=initial.manifest.session_id,
            model_factory=scripted_factory(requests),
        )
        assert reused.reused_units == 1
        data["arguments"]["baseline"] = True
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with pytest.raises(ValueError, match="baseline"):
            await review(
                benchmark.paths,
                benchmark.settings,
                prepared.path,
                resume=initial.manifest.session_id,
            )


async def test_rule_content_change_rejects_reuse_before_git(service, monkeypatch):
    benchmark, _ = service
    rule = benchmark.paths.root / "custom.md"
    rule.write_text("FIRST_RULE")
    benchmark.settings = replace(benchmark.settings, rule_file="custom.md")
    benchmark._plan("org/one", 1, 1)
    rule.write_text("SECOND_RULE")

    def forbidden(*args, **kwargs):
        raise AssertionError("changed rules must fail before checkout")

    monkeypatch.setattr(RepositoryPool, "review_checkout", forbidden)
    with pytest.raises(ValueError, match="changed"):
        await benchmark.run_reviews()


async def test_common_symlink_filter(service):
    benchmark, requests = service
    from code_review_agent.evaluation.dataset import PullRequest
    from code_review_agent.evaluation.reviewers.project import run_project
    from code_review_agent.evaluation.task import build_task, comparison_settings

    pr = benchmark.selected_prs("org/one")[0]
    pool = RepositoryPool(benchmark.paths)
    remote = Path(pool._clone_url(pr))
    (remote / "alias[1].py").symlink_to("a.py")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "alias")
    target = git(remote, "rev-parse", "HEAD")
    changed = PullRequest(pr.pr_url, pr.repo, pr.language, pr.base_sha, target, pr.references)
    async with pool.review_checkout(changed) as prepared:
        task = build_task(
            benchmark.paths, benchmark.settings, prepared.path, changed.base_sha, target
        )
        assert task.payload["targets"] == ["a.py"]
        assert any(item["path"] == "alias[1].py" for item in task.payload["excluded"])
        effective = comparison_settings(benchmark.settings, prepared.path, changed.base_sha, target)
        assert effective.exclude != benchmark.settings.exclude
        result = await run_project(
            benchmark.paths, benchmark.settings, task, "alias-test", scripted_factory(requests)
        )
        assert list(result["coverage"]) == ["a.py"]


async def test_filtered_references_keep_full_recall_denominator(service):
    benchmark, requests = service
    benchmark.settings = replace(benchmark.settings, exclude=("a.py",))
    plan = await benchmark.run_reviews(repo="org/one", limit=1)
    assert all(job["status"] == "no_files" for job in objects(plan["jobs"]))
    assert not requests
    await benchmark.score_reviews(mock=True)
    report = benchmark.report_reviews()
    for name in ("project", "claude"):
        assert report["systems"][name]["micro"]["references"] == 1
        assert report["systems"][name]["micro"]["recall"] == 0
    assert all(len(row["filtered_positive_references"]) == 1 for row in report["per_pr"])


def test_report_keeps_negative_percentage_point_change_and_single_round(service):
    from code_review_agent.evaluation.reporting import build_report

    benchmark, _ = service
    plan = benchmark._plan("org/one", 1, 1)
    jobs = objects(plan["jobs"])
    reference_id = benchmark.selected_prs("org/one")[0].references[0].reference_id
    rows = []
    for job in jobs:
        job["status"] = "completed"
        hit = int(job["reviewer"] == "claude")
        rows.append(
            {
                "job_id": job["job_id"],
                "reviewer": job["reviewer"],
                "repo": job["repo"],
                "pr_url": job["pr_url"],
                "repetition": 0,
                "completed": True,
                "generated_known": True,
                "matched": hit,
                "matched_upper": hit,
                "generated": 1,
                "references": 1,
                "matching": {reference_id: "g"} if hit else {},
                "undecided": 0,
                "excluded_reference_rows": 0,
            }
        )
    report = build_report(
        benchmark.store,
        plan,
        {
            "score_id": "hand-calculated",
            "identity": {"jobs": jobs},
            "scores": rows,
            "judge_usage": [],
            "flow_test_only": False,
        },
    )
    assert report["project_minus_claude_f1_percentage_points"] == -100
    assert report["systems"]["project"]["repeat_std_f1"] is None
    assert report["systems"]["project"]["micro"]["f1"] == 0
    assert report["systems"]["claude"]["micro"]["f1"] == 1
