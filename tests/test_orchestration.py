import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from test_inputs import git
from test_review_sessions import configured, scripted_factory

from code_review_agent.application.review_service import review
from code_review_agent.config import Settings
from code_review_agent.contracts import (
    InputMode,
    ReviewResult,
    ReviewUnit,
    RunManifest,
    RunStatus,
)
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.review.engine import ReviewEngine
from code_review_agent.review.rules import RuleResolver
from code_review_agent.runtime.agentscope_adapter import AgentRun
from code_review_agent.runtime.stages import StageRunner


@pytest.mark.asyncio
async def test_concurrency_secondary_file_coverage_and_planning_fallback(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (source / name).write_text("value = 1\n" * 60)
    settings = replace(Settings(), max_concurrency=2, effort="low")
    frozen = prepare_input(source, settings, mode="scan")
    frozen = replace(
        frozen,
        mode=InputMode.RANGE,
        files=tuple(replace(file, diff=file.content) for file in frozen.files),
    )
    rules = RuleResolver(ProjectPaths(tmp_path), frozen, settings)
    runner = StageRunner(settings)
    active = peak = 0
    calls = []

    async def fake_run(stage, system, user, **kwargs):
        nonlocal active, peak
        calls.append(stage)
        if stage == "plan":
            raise ValueError("planning server error")
        registry = kwargs["registry"]
        assert registry.collector is not None
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        # 多文件组明确完成第一个文件，不能冒充全部完成。
        paths = sorted(registry.collector.target_paths)
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": paths[:1]})
        active -= 1
        return AgentRun("", True, {}, ())

    runner.run = fake_run
    units = [
        ReviewUnit("ab", ("a.py", "b.py")),
        ReviewUnit("c", ("c.py",)),
        ReviewUnit("d", ("d.py",)),
    ]
    result = ReviewResult(
        RunStatus.PENDING,
        RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None),
    )
    await ReviewEngine(frozen, settings, rules, runner).run(result, planned=units)
    assert peak == 2 and "plan" in calls
    assert result.status == RunStatus.PARTIAL
    assert result.coverage["b.py"] == "pending"
    assert result.coverage["a.py"] == "reviewed"
    assert any("Planning fallback" in message for message in result.warnings)


@pytest.mark.asyncio
async def test_scan_has_one_outer_round_and_no_reflection_even_with_high_effort(tmp_path: Path):
    from code_review_agent.sessions.replay import load_session

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("return 1 / x\n")
    calls = []
    result = await review(
        ProjectPaths(tmp_path),
        replace(configured(), effort="high"),
        source,
        mode="scan",
        model_factory=scripted_factory(calls),
    )
    assert result.status == RunStatus.COMPLETED and len(result.candidates) == 1
    identifiers = {item.candidate_id for item in result.candidates}
    assert len(identifiers) == len(result.locations) == 1
    assert not result.reflections
    view = load_session(ProjectPaths(tmp_path), result.manifest.session_id)
    assert {item.candidate_id for item in view.result().candidates} == identifiers
    assert not any(
        stage in {"review.round2", "review.round3"} or stage.startswith("reflection")
        for stage, _ in calls
    )


@pytest.mark.asyncio
async def test_frozen_range_resume_does_not_resolve_moved_branch(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    (source / "a.py").write_text("return x\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "initial")
    base = git(source, "rev-parse", "HEAD")
    (source / "a.py").write_text("return 1 / x\n")
    git(source, "commit", "-qam", "change")
    settings = configured()
    result = await review(
        ProjectPaths(tmp_path),
        settings,
        source,
        from_ref=base,
        to_ref="main",
        model_factory=scripted_factory([]),
    )
    head = result.manifest.head_sha
    (source / "a.py").write_text("return x + 2\n")
    git(source, "commit", "-qam", "moved")
    resumed = await review(
        ProjectPaths(tmp_path),
        settings,
        source,
        resume=result.manifest.session_id,
        model_factory=scripted_factory([]),
    )
    assert resumed.manifest.head_sha == head
    assert resumed.status == RunStatus.COMPLETED
    workspace = await review(
        ProjectPaths(tmp_path), settings, source, model_factory=scripted_factory([])
    )
    with pytest.raises(ValueError, match="workspace"):
        await review(ProjectPaths(tmp_path), settings, source, resume=workspace.manifest.session_id)


@pytest.mark.asyncio
async def test_cancel_retains_submitted_candidates_and_pending_coverage(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("return x\n")
    settings = replace(Settings(), effort="low", scan_plan=False)
    frozen = prepare_input(source, settings, mode="scan")
    runner = StageRunner(settings)
    submitted = asyncio.Event()
    stages = []

    async def fake_run(stage, system, user, **kwargs):
        stages.append(stage)
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "existing_code": "return x",
                        "content": "issue",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
            },
        )
        submitted.set()
        await asyncio.Future()

    runner.run = fake_run
    result = ReviewResult(
        RunStatus.PENDING,
        RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None),
    )
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    task = asyncio.create_task(engine.run(result))
    await submitted.wait()
    task.cancel()
    result = await task
    assert result.status == RunStatus.PARTIAL and len(result.candidates) == 1
    assert result.coverage == {"a.py": "partial"}
    assert stages == ["review.round1"]


@pytest.mark.asyncio
async def test_scan_dedup_is_atomic_and_keeps_original_ledger(tmp_path: Path):
    from code_review_agent.contracts import FindingCandidate

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("return x\n")
    settings = Settings()
    frozen = prepare_input(source, settings, mode="scan")
    runner = StageRunner(settings)
    candidates = [
        FindingCandidate(str(i), "a.py", f"issue {i}", "return x", "bug", "high") for i in range(4)
    ]
    result = ReviewResult(
        RunStatus.PENDING,
        RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None),
        candidates=candidates,
    )
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )

    async def malformed(*args, **kwargs):
        return AgentRun(
            '{"groups":[{"members":["c-0","c-1"]},{"members":["c-1","c-2","c-3"]}]}', True, {}, ()
        )

    runner.run = malformed
    await engine.deduplicate_batch(result, ReviewUnit("batch", ("a.py",)))
    assert not result.suppressed and result.warnings

    async def valid(*args, **kwargs):
        return AgentRun(
            '{"groups":[{"members":["c-0","c-1","c-2","c-3"],"merged_content":"combined finding"}]}',
            True,
            {},
            (),
        )

    runner.run = valid
    await engine.deduplicate_batch(result, ReviewUnit("batch", ("a.py",)))
    assert not result.suppressed and not result.rewritten_content
    assert len(result.candidates) == 1 and result.candidates[0].content == "combined finding"
    assert len(result.raw_candidates) == 4 and result.raw_candidates[0].content == "issue 0"


@pytest.mark.asyncio
async def test_diff_rounds_filter_before_next_prompt_and_reuse_checkpoint(tmp_path, monkeypatch):
    """拒绝项不反馈，保留项累计反馈；计划只在第一轮，恢复不重复复核。"""
    import json
    from dataclasses import asdict

    from code_review_agent.contracts import ReflectionDecision
    from code_review_agent.output.render import eligible_comments
    from code_review_agent.sessions.serialization import decode_unit_result

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    (source / "a.py").write_text("value = 1\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "initial")
    base = git(source, "rev-parse", "HEAD")
    (source / "a.py").write_text("value = 2\n")
    git(source, "commit", "-qam", "change")
    settings = replace(Settings(), effort="high", plan_line_threshold=0)
    frozen = prepare_input(source, settings, from_ref=base, to_ref="HEAD")
    runner = StageRunner(settings)
    events = []

    batches = []

    async def reflect(candidates, *args, **kwargs):
        batches.append(len(candidates))
        events.extend("filter:" + candidate.content for candidate in candidates)
        return [
            ReflectionDecision(
                candidate.candidate_id,
                "reject" if candidate.content == "rejected issue" else "keep",
                "fixture evidence",
                ("a.py:1: fixture",),
            )
            for candidate in candidates
        ]

    async def run(stage, system, user, **kwargs):
        events.append(stage)
        if stage == "plan":
            return AgentRun("INITIAL_PLAN", True, {}, ())
        registry = kwargs["registry"]
        if stage == "review.round1":
            assert "INITIAL_PLAN" in user
            contents = ["rejected issue", "retained issue", "retained issue"]
        else:
            assert "INITIAL_PLAN" not in user
            assert "Do not repeat them" in user
            assert "retained issue" in user and "rejected issue" not in user
            assert user.count("issue: retained issue") == (2 if stage == "review.round2" else 3)
            assert "rejected issue" not in kwargs["pinned_context"]
            assert events.index("filter:retained issue") < events.index(stage)
            contents = ["retained issue", "new issue"] if stage == "review.round2" else []
            if stage == "review.round3":
                assert "new issue" in user
        for content in contents:
            registry.invoke(
                "code_comment",
                {
                    "comments": [
                        {
                            "path": "a.py",
                            "content": content,
                            "existing_code": "value = 2",
                            "category": "bug",
                            "severity": "high",
                        }
                    ]
                },
            )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        return AgentRun("", True, {}, ())

    monkeypatch.setattr("code_review_agent.review.engine.reflect_group", reflect)
    runner.run = run
    unit = ReviewUnit("unit", ("a.py",))
    manifest = RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    result = await engine.run(ReviewResult(RunStatus.PENDING, manifest), planned=[unit])
    assert len(result.candidates) == len(result.locations) == len(result.reflections) == 5
    identifiers = {item.candidate_id for item in result.candidates}
    assert len(identifiers) == 5
    assert len(eligible_comments(result)) == 4
    assert events.count("filter:retained issue") == 3
    assert batches == [3, 2]
    checkpoint = decode_unit_result(json.loads(json.dumps(asdict(engine.units[0]))))
    assert len(checkpoint.locations) == len(checkpoint.reflections) == 5
    before = list(events)
    restored = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest),
        planned=[unit],
        completed={unit.unit_id: checkpoint},
    )
    assert events == before and restored.reused_units == 1
    assert len(restored.reflections) == 5
    assert {item.candidate_id for item in restored.candidates} == identifiers
    assert len(eligible_comments(restored)) == 4


@pytest.mark.asyncio
async def test_diff_all_rejected_stops_without_discarding_raw_candidates(tmp_path, monkeypatch):
    from code_review_agent.contracts import InputMode, ReflectionDecision

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("value = 2\n")
    settings = replace(Settings(), effort="high")
    frozen = replace(prepare_input(source, settings, mode="scan"), mode=InputMode.RANGE)
    runner = StageRunner(settings)
    calls = []

    async def reflect(candidates, *args, **kwargs):
        return [
            ReflectionDecision(candidate.candidate_id, "reject", "contradicted", ("a.py:1: fact",))
            for candidate in candidates
        ]

    async def run(stage, system, user, **kwargs):
        calls.append(stage)
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "content": "wrong claim",
                        "existing_code": "value = 2",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        return AgentRun("", True, {}, ())

    monkeypatch.setattr("code_review_agent.review.engine.reflect_group", reflect)
    runner.run = run
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    result = await engine.review_unit(ReviewUnit("unit", ("a.py",)))
    assert calls == ["review.round1"]
    assert len(result.candidates) == len(result.reflections) == 1
    assert result.reflections[0].decision == "reject"


def test_confirmed_summary_matches_original_limits():
    from code_review_agent.contracts import FindingCandidate
    from code_review_agent.review.prompts import confirmed_findings_block

    item = FindingCandidate("id", "a.py", "文" * 301, "码" * 201, "bug", "high")
    text = confirmed_findings_block((item,))
    assert "Do not repeat them" in text
    assert "code: " + "码" * 200 + "..." in text
    assert "issue: " + "文" * 300 + "..." in text
    assert "码" * 201 not in text and "文" * 301 not in text
    assert confirmed_findings_block(()) == ""


@pytest.mark.asyncio
async def test_diff_reflection_cancellation_keeps_submitted_candidates(tmp_path, monkeypatch):
    from test_location import input_with_files

    from code_review_agent.contracts import FileSnapshot

    frozen = input_with_files(FileSnapshot("a.py", "safe()"))
    settings = replace(Settings(), effort="high")
    runner = StageRunner(settings)

    async def run(stage, system, user, **kwargs):
        assert stage == "review.round1"
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "content": "issue",
                        "existing_code": "safe()",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        return AgentRun("", True, {}, ())

    async def reflect(candidates, *args, **kwargs):
        raise asyncio.CancelledError

    runner.run = run
    monkeypatch.setattr("code_review_agent.review.engine.reflect_group", reflect)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    manifest = RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)
    result = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest), planned=[ReviewUnit("unit", ("a.py",))]
    )
    assert result.status == RunStatus.PARTIAL
    assert len(result.candidates) == len(result.locations) == 1
    assert not result.reflections and result.coverage["a.py"] == "partial"


@pytest.mark.asyncio
async def test_comment_starts_location_worker_before_main_ends_and_waits_before_reflection(
    tmp_path, monkeypatch
):
    from test_location import input_with_files

    from code_review_agent.contracts import (
        FileSnapshot,
        Location,
        LocationDecision,
        ReflectionDecision,
        Side,
    )
    from code_review_agent.output.render import eligible_comments

    frozen = input_with_files(FileSnapshot("a.py", "new()", "old()"))
    settings = replace(Settings(), effort="low", max_concurrency=1)
    runner = StageRunner(settings)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    started, main_ended, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = peak = count = 0
    reflected = []

    async def locate(item, *args, **kwargs):
        nonlocal active, peak, count
        count += 1
        active += 1
        peak = max(peak, active)
        started.set()
        try:
            await release.wait()
            return LocationDecision(
                item.candidate_id, "verified", Location("a.py", Side.LEFT, 1, 1, "old()"), ()
            )
        finally:
            active -= 1

    async def reflect(candidates, *args, **kwargs):
        assert main_ended.is_set() and active == 0
        for item in candidates:
            assert item.side == Side.LEFT and item.proposed_start == item.proposed_end == 1
            assert item.existing_code == "old()"
            reflected.append(item.candidate_id)
        return [ReflectionDecision(item.candidate_id, "keep", "checked") for item in candidates]

    async def run(stage, system, user, **kwargs):
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "existing_code": "old()",
                        "content": "issue",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
                * 2
            },
        )
        await asyncio.wait_for(started.wait(), 1)
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        main_ended.set()
        return AgentRun("", True, {}, ())

    monkeypatch.setattr("code_review_agent.review.engine.locate_candidate", locate)
    monkeypatch.setattr("code_review_agent.review.engine.reflect_group", reflect)
    runner.run = run
    unit = ReviewUnit("unit", ("a.py",))
    manifest = RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)
    task = asyncio.create_task(
        engine.run(ReviewResult(RunStatus.PENDING, manifest), planned=[unit])
    )
    await asyncio.wait_for(main_ended.wait(), 1)
    assert not task.done() and not reflected
    release.set()
    result = await task
    assert peak == 1 and count == len(result.locations) == len(reflected) == 2
    assert all(item["side"] == "left" for item in eligible_comments(result))
    restored = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest),
        planned=[unit],
        completed={unit.unit_id: engine.units[0]},
    )
    assert restored.reused_units == 1 and count == len(reflected) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coordinates", [{}, {"start_line": 8}, {"end_line": 8}, {"start_line": 8, "end_line": 2}]
)
async def test_failed_or_supplied_location_is_published_and_checkpointed(
    tmp_path, monkeypatch, coordinates
):
    from dataclasses import asdict

    from test_location import input_with_files

    from code_review_agent.contracts import FileSnapshot, ReflectionDecision
    from code_review_agent.output.render import eligible_comments
    from code_review_agent.sessions.serialization import decode_unit_result, json_object

    frozen = input_with_files(FileSnapshot("a.py", "safe()"))
    settings = replace(Settings(), effort="low")
    runner = StageRunner(settings)
    calls = []

    async def run(stage, system, user, **kwargs):
        calls.append(stage)
        if stage == "location.relocation":
            return AgentRun("Explanation\n```python\ninvented()\n```", True, {}, ())
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "existing_code": "missing()",
                        "content": "issue",
                        "category": "bug",
                        "severity": "high",
                        **coordinates,
                    }
                ]
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        return AgentRun("", True, {}, ())

    async def reflect(candidates, *args, **kwargs):
        assert all(item.existing_code == "missing()" for item in candidates)
        return [ReflectionDecision(item.candidate_id, "keep", "checked") for item in candidates]

    monkeypatch.setattr("code_review_agent.review.engine.reflect_group", reflect)
    runner.run = run
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    unit = ReviewUnit("unit", ("a.py",))
    manifest = RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)
    result = await engine.run(ReviewResult(RunStatus.PENDING, manifest), planned=[unit])
    comments = eligible_comments(result)
    assert result.status == RunStatus.COMPLETED and len(comments) == 1
    assert comments[0]["start_line"] == coordinates.get("start_line", 0)
    assert comments[0]["end_line"] == coordinates.get("end_line", 0)
    assert comments[0]["existing_code"] == "missing()"
    assert result.locations[0].status == ("provided" if coordinates else "unlocated")
    assert calls.count("location.relocation") == (0 if coordinates else 1)
    checkpoint = decode_unit_result(json_object(asdict(engine.units[0])))
    before = list(calls)
    restored = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest),
        planned=[unit],
        completed={unit.unit_id: checkpoint},
    )
    assert calls == before and eligible_comments(restored) == comments
    restored.reflections = [
        ReflectionDecision(
            result.candidates[0].candidate_id, "reject", "checked", ("a.py:1: safe",)
        )
    ]
    assert not eligible_comments(restored)


@pytest.mark.asyncio
async def test_cancel_during_worker_drain_cleans_up_and_keeps_candidate(tmp_path, monkeypatch):
    from test_location import input_with_files

    from code_review_agent.contracts import FileSnapshot

    frozen = input_with_files(FileSnapshot("a.py", "safe()"))
    settings = replace(Settings(), effort="low")
    runner = StageRunner(settings)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def locate(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def run(stage, system, user, **kwargs):
        registry = kwargs["registry"]
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": "a.py",
                        "existing_code": "missing()",
                        "content": "issue",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": ["a.py"]})
        return AgentRun("", True, {}, ())

    monkeypatch.setattr("code_review_agent.review.engine.locate_candidate", locate)
    runner.run = run
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    manifest = RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)
    task = asyncio.create_task(
        engine.run(
            ReviewResult(RunStatus.PENDING, manifest), planned=[ReviewUnit("unit", ("a.py",))]
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    result = await task
    assert cancelled.is_set() and result.status == RunStatus.PARTIAL
    assert len(result.candidates) == 1 and not result.reflections


@pytest.mark.asyncio
async def test_location_worker_exception_records_failure_without_losing_comment():
    from test_location import input_with_files

    from code_review_agent.contracts import FileSnapshot
    from code_review_agent.tools.findings import FindingCollector

    async def broken_locator(candidate):
        raise RuntimeError("worker failure")

    collector = FindingCollector(
        input_with_files(FileSnapshot("a.py", "safe()")), ("a.py",), "input", locator=broken_locator
    )
    collector.code_comment(
        [
            {
                "path": "a.py",
                "existing_code": "missing()",
                "content": "issue",
                "category": "bug",
                "severity": "high",
            }
        ]
    )
    await collector.wait_locations()
    assert len(collector.candidates) == len(collector.locations) == 1
    decision = collector.locations[collector.candidates[0].candidate_id]
    assert decision.status == "unlocated" and decision.attempts[0].stage == "worker"
    assert "RuntimeError" in decision.attempts[0].reason
