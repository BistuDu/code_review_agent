import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_inputs import git
from test_review_sessions import configured, scripted_factory

from code_review_agent.application.review_service import review
from code_review_agent.config import ModelSettings, Settings
from code_review_agent.contracts import (
    FindingCandidate,
    Location,
    LocationDecision,
    ReviewResult,
    ReviewUnit,
    RunManifest,
    RunStatus,
    Side,
)
from code_review_agent.inputs.scan import scan_units
from code_review_agent.inputs.scan_files import enumerate_scan_paths
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.project_paths import ProjectPaths
from code_review_agent.review.engine import ReviewEngine
from code_review_agent.review.prompts import format_scan_plan
from code_review_agent.review.rules import RuleResolver
from code_review_agent.runtime.agentscope_adapter import AgentRun
from code_review_agent.runtime.stages import StageRunner


def write(root, path, content="safe()\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def manifest(frozen):
    return RunManifest("test", "input", "config", "rules", frozen.mode, frozen.repo, None, None)


def test_git_scan_uses_tracked_and_untracked_files_with_nested_ignore_and_root_rules(tmp_path):
    git(tmp_path, "init", "-q")
    write(tmp_path, ".gitignore", "ignored.py\n!keep.py\n")
    write(tmp_path, "ignored.py")
    write(tmp_path, "keep.py")
    write(tmp_path, "nested/.gitignore", "local.py\n")
    write(tmp_path, "nested/local.py")
    write(tmp_path, "nested/visible.py")
    write(tmp_path, " tracked.py ")
    git(tmp_path, "add", "-f", "ignored.py", " tracked.py ")
    paths = enumerate_scan_paths(tmp_path)
    assert "ignored.py" not in paths and "nested/local.py" not in paths
    assert {"keep.py", "nested/visible.py", " tracked.py "} <= set(paths)
    assert enumerate_scan_paths(tmp_path / "nested") == (".gitignore", "visible.py")


def test_non_git_scan_applies_ordered_root_gitignore_and_builtin_directory_rules(tmp_path):
    write(
        tmp_path, ".gitignore", "*.py\n!keep.py\n!blocked.py\nblocked.py\n/root_only.py\ncache/\n"
    )
    for path in (
        "keep.py",
        "blocked.py",
        "root_only.py",
        "nested/keep.py",
        "nested/root_only.py",
        "cache/keep.py",
        "vendor/keep.py",
        "node_modules/keep.py",
    ):
        write(tmp_path, path)
    assert set(enumerate_scan_paths(tmp_path)) == {".gitignore", "keep.py", "nested/keep.py"}
    # Non-git fallback intentionally only loads the root ignore file, like Go.
    write(tmp_path, "nested/.gitignore", "keep.py\n")
    assert "nested/keep.py" in enumerate_scan_paths(tmp_path)


def test_directory_batches_use_first_level_directory_and_language_handles_extensionless():
    paths = ("src/deep/a.py", "src/other/b.py", "root.py", "lib/c.js")
    batches = scan_units(paths, "by-directory", 50)
    assert [batch.paths for batch in batches] == [
        ("root.py",),
        ("lib/c.js",),
        ("src/deep/a.py", "src/other/b.py"),
    ]
    assert scan_units(("Makefile", "a.py"), "by-language", 50)[-1].paths == ("Makefile",)


@pytest.mark.asyncio
async def test_scan_batches_are_sequential_locations_precede_dedup_and_checkpoints_follow_dedup(
    tmp_path,
):
    for path in ("a.js", "b.js", "c.py", "d.py"):
        write(tmp_path, path)
    settings = replace(Settings(), effort="high", max_concurrency=2, scan_plan=False)
    frozen = prepare_input(tmp_path, settings, mode="scan")
    runner = StageRunner(settings)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    result = ReviewResult(RunStatus.PENDING, manifest(frozen))
    events, checkpoints = [], {}
    active = peak = 0

    async def run(stage, system, user, **kwargs):
        nonlocal active, peak
        if stage == "scan.dedup":
            assert kwargs["single_call"]
            assert len(result.locations) == (4 if not checkpoints else 8)
            events.append("dedup")
            comments = json.loads(
                user.split("<batch_comments>\n", 1)[1].split("\n</batch_comments>", 1)[0]
            )
            assert [item["id"] for item in comments] == ["c-0", "c-1", "c-2", "c-3"]
            return AgentRun(
                json.dumps(
                    {
                        "groups": [
                            {"members": ["c-0", "c-1"], "merged_content": "merged finding"},
                            {"members": ["c-2", "c-3"]},
                        ]
                    }
                ),
                True,
                {},
                (),
            )
        if stage == "scan.summary":
            assert kwargs["single_call"] and len(checkpoints) == 4
            assert "Total comments: 4 across 4 files" in user
            assert "merged finding" in user
            events.append("summary")
            return AgentRun("summary", True, {}, ())
        assert stage == "review.round1"
        registry = kwargs["registry"]
        path = next(iter(registry.collector.target_paths))
        if path.endswith(".py"):
            assert "a.js" in checkpoints and "b.js" in checkpoints
        events.append("review:" + path)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": path,
                        "existing_code": "safe()",
                        "content": "same finding",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
                * 2
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": [path]})
        active -= 1
        return AgentRun("", True, {}, ())

    async def checkpoint(unit):
        assert events[-1] == "dedup"
        assert len(unit.candidates) == 1
        assert len(unit.raw_candidates) == len(unit.locations) == 2
        assert {item.candidate_id for item in unit.locations} == {
            item.candidate_id for item in unit.raw_candidates
        }
        checkpoints[unit.unit.paths[0]] = deepcopy(unit)

    runner.run = run
    await engine.run(result, checkpoint=checkpoint)
    assert result.status == RunStatus.COMPLETED and peak == 2
    assert len(result.candidates) == 4 and len(result.raw_candidates) == len(result.locations) == 8
    assert not result.reflections and not result.suppressed and not result.rewritten_content
    assert events.count("dedup") == 2 and events[-1] == "summary"
    assert [batch.paths for batch in result.scan_batches] == [("a.js", "b.js"), ("c.py", "d.py")]
    before = len(events)
    resumed = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest(frozen)),
        completed={item.unit.unit_id: item for item in checkpoints.values()},
    )
    assert resumed.reused_units == 4 and len(resumed.candidates) == 4
    assert events[before:] == ["summary"]


@pytest.mark.asyncio
async def test_cross_file_dedup_replaces_collection_but_checkpoints_keep_each_files_findings(
    tmp_path,
):
    write(tmp_path, "a.py")
    write(tmp_path, "b.py")
    settings = replace(Settings(), scan_plan=False)
    frozen = prepare_input(tmp_path, settings, mode="scan")
    runner = StageRunner(settings)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    candidates = [
        FindingCandidate(str(index), path, "issue", "safe()", "bug", "high")
        for index, path in enumerate(("a.py", "a.py", "b.py", "b.py"))
    ]
    result = ReviewResult(RunStatus.PENDING, manifest(frozen), candidates=candidates)

    async def run(*args, **kwargs):
        return AgentRun(
            '{"groups":[{"members":["c-0","c-1","c-2","c-3"],"merged_content":"merged"}]}',
            True,
            {},
            (),
        )

    runner.run = run
    checkpoints = await engine.deduplicate_batch(result, ReviewUnit("batch", ("a.py", "b.py")))
    assert len(result.candidates) == 1 and result.candidates[0].content == "merged"
    assert [item.candidate_id for item in checkpoints["a.py"]] == ["0", "1"]
    assert [item.candidate_id for item in checkpoints["b.py"]] == ["2", "3"]
    assert len(result.raw_candidates) == 4 and not result.suppressed


@pytest.mark.asyncio
async def test_scan_does_not_require_reflection_configuration(tmp_path):
    write(tmp_path, "a.py", "return 1 / x\n")
    calls = []
    result = await review(
        ProjectPaths(tmp_path),
        replace(configured(), reflection=ModelSettings()),
        tmp_path,
        mode="scan",
        model_factory=scripted_factory(calls),
    )
    assert result.status == RunStatus.COMPLETED and not result.reflections
    assert not any(stage.startswith("reflection") for stage, _ in calls)


def test_scan_plan_formats_focus_areas_and_retains_fallback_text():
    raw = '```json\n{"summary":"boundary checks","checkpoints":[{"focus":"parse","lines":"2-8","why":"untrusted input"}]}\n```'
    assert format_scan_plan(raw) == (
        "**Summary**: boundary checks\n\n"
        "**Focus areas (give these extra attention; not exhaustive):**\n"
        "1. `parse` (lines 2-8) — untrusted input"
    )
    assert format_scan_plan(" ") == "(no pre-scan plan; review the entire file as usual)"
    assert format_scan_plan("Check the parser") == "Check the parser"
    assert format_scan_plan('{"summary":1}') == '{"summary":1}'


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "invalid", "request"])
async def test_scan_dedup_failure_preserves_comments_and_batch_checkpoints(tmp_path, failure):
    for path in ("a.js", "b.js", "c.py"):
        write(tmp_path, path)
    settings = replace(Settings(), scan_plan=False, scan_summary=False)
    frozen = prepare_input(tmp_path, settings, mode="scan")
    runner = StageRunner(settings)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    checkpoints, reviewed = {}, []

    async def run(stage, system, user, **kwargs):
        if stage == "scan.dedup":
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "request":
                return AgentRun("", False, {}, (), "server error")
            # 遗漏三条评论，不能把不完整分区发布为成功去重。
            return AgentRun('{"groups":[{"members":["c-0"]}]}', True, {}, ())
        assert stage == "review.round1"
        registry = kwargs["registry"]
        path = next(iter(registry.collector.target_paths))
        reviewed.append(path)
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": path,
                        "content": "issue",
                        "existing_code": "safe()",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
                * 2
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": [path]})
        return AgentRun("", True, {}, ())

    async def checkpoint(unit):
        assert len(unit.candidates) == len(unit.locations) == 2
        checkpoints[unit.unit.paths[0]] = unit

    runner.run = run
    result = await engine.run(
        ReviewResult(RunStatus.PENDING, manifest(frozen)), checkpoint=checkpoint
    )
    assert len(result.candidates) == len(result.raw_candidates) == len(result.locations)
    assert not result.reflections and not result.suppressed
    assert "a.js" in checkpoints and "b.js" in checkpoints
    if failure == "cancel":
        assert result.status == RunStatus.PARTIAL and len(result.candidates) == 4
        assert "c.py" not in reviewed and result.coverage["c.py"] == "pending"
    else:
        assert result.status == RunStatus.COMPLETED and len(result.candidates) == 6
        assert "c.py" in checkpoints
    assert result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_scan_location_workers_do_not_hold_file_review_slots(tmp_path, cancel):
    for path in ("a.py", "b.py"):
        write(tmp_path, path)
    settings = replace(
        Settings(), max_concurrency=1, scan_plan=False, scan_summary=False, dedup_min_comments=2
    )
    frozen = prepare_input(tmp_path, settings, mode="scan")
    runner = StageRunner(settings)
    engine = ReviewEngine(
        frozen, settings, RuleResolver(ProjectPaths(tmp_path), frozen, settings), runner
    )
    both_reviewed, release = asyncio.Event(), asyncio.Event()
    reviewed = []
    locations = []

    async def locate(item):
        await release.wait()
        locations.append(item.candidate_id)
        return LocationDecision(
            item.candidate_id, "verified", Location(item.path, Side.RIGHT, 1, 1, "safe()"), ()
        )

    async def run(stage, system, user, **kwargs):
        if stage == "scan.dedup":
            assert len(locations) == 2
            return AgentRun('{"groups":[{"members":["c-0","c-1"]}]}', True, {}, ())
        registry = kwargs["registry"]
        path = next(iter(registry.collector.target_paths))
        reviewed.append(path)
        registry.invoke(
            "code_comment",
            {
                "comments": [
                    {
                        "path": path,
                        "content": "issue",
                        "existing_code": "safe()",
                        "category": "bug",
                        "severity": "high",
                    }
                ]
            },
        )
        registry.invoke("task_done", {"state": "DONE", "reviewed_paths": [path]})
        if len(reviewed) == 2:
            both_reviewed.set()
        return AgentRun("", True, {}, ())

    runner.run = run
    engine.locate = locate
    task = asyncio.create_task(engine.run(ReviewResult(RunStatus.PENDING, manifest(frozen))))
    try:
        await asyncio.wait_for(both_reviewed.wait(), 1)
        assert not task.done() and not locations
        if cancel:
            task.cancel()
            result = await asyncio.wait_for(task, 1)
    finally:
        release.set()
    result = await task
    if cancel:
        assert result.status == RunStatus.PARTIAL and len(result.candidates) == 2
        assert not result.locations
    else:
        assert result.status == RunStatus.COMPLETED and len(result.candidates) == 1
        assert len(result.raw_candidates) == len(result.locations) == 2
    assert not engine._pending_scan_locations
