"""正式评审入口：冻结输入、单文件 journal 与文件级结果恢复。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import cast

from ..config import Settings
from ..contracts import (
    FindingCandidate,
    InputMode,
    ReviewResult,
    ReviewUnit,
    RunManifest,
    RunStatus,
    UnitResult,
    stable_digest,
)
from ..inputs.snapshots import prepare_input
from ..location.pipeline import LOCATION_POLICY
from ..project_paths import ProjectPaths
from ..reflection.reviewer import REFLECTION_POLICY
from ..review.engine import ReviewEngine
from ..review.rules import RuleResolver
from ..review.scan_engine import SCAN_POLICY
from ..runtime.model_factory import create_model
from ..runtime.stages import ModelFactory, StageRunner
from ..sessions.journal import JournalError, SessionJournal
from ..sessions.recorder import ResultRecorder, file_fingerprints
from ..sessions.replay import load_session, object_field
from ..sessions.results import encode_file, input_metadata
from ..sessions.serialization import json_object


async def review(
    paths: ProjectPaths,
    settings: Settings,
    repo: Path,
    *,
    mode: str = "review",
    from_ref: str | None = None,
    to_ref: str | None = None,
    commit: str | None = None,
    scan_path: str | None = None,
    preview: bool = False,
    resume: str | None = None,
    range_strategy: str | None = None,
    model_factory: ModelFactory = create_model,
) -> ReviewResult:
    preparation_started = time.monotonic()
    parent = load_session(paths, resume) if resume else None
    historical = []
    cached: dict[str, UnitResult] = {}
    if parent:
        manifest = parent.manifest
        saved = object_field(parent.start, "input")
        saved_strategy = str(saved.get("range_strategy", "merge-base"))
        if range_strategy is not None and range_strategy != saved_strategy:
            raise ValueError("Revision range strategy changed")
        range_strategy = saved_strategy
        arguments = object_field(parent.start, "arguments")
        if manifest["mode"] == InputMode.WORKSPACE.value:
            raise ValueError("workspace cross-run recovery is unsupported")
        if str(repo.resolve()) != manifest["repo"]:
            raise ValueError("Repository identity changed")
        if arguments.get("baseline", False) is True:
            raise ValueError("Legacy baseline sessions cannot be resumed")
        scan_path = cast(str | None, arguments.get("scan_path"))
        if manifest["mode"] == InputMode.RANGE.value:
            current = prepare_input(
                repo,
                settings,
                from_ref=cast(str, saved["from_sha"]),
                to_ref=cast(str, saved["head_sha"]),
                range_strategy=range_strategy,
            )
        elif manifest["mode"] == InputMode.COMMIT.value:
            current = prepare_input(repo, settings, commit=cast(str, saved["head_sha"]))
        else:
            current = prepare_input(repo, settings, mode="scan", scan_path=scan_path)
        mode = "scan" if current.mode == InputMode.SCAN else "review"
        if input_metadata(current) != saved:
            raise ValueError("Frozen input or scan content changed")
    else:
        current = prepare_input(
            repo,
            settings,
            mode=mode,
            from_ref=from_ref,
            to_ref=to_ref,
            commit=commit,
            scan_path=scan_path,
            range_strategy=range_strategy or "merge-base",
        )
    metadata = input_metadata(current)
    rules = RuleResolver(paths, current, settings)
    review_input = rules.apply_filters(current, settings)
    config_identity = stable_digest(
        [settings.identity, False, LOCATION_POLICY, REFLECTION_POLICY, SCAN_POLICY]
    )
    if parent:
        if (
            parent.manifest["config_identity"] != config_identity
            or parent.manifest["resource_identity"] != rules.identity
        ):
            raise ValueError("Model/configuration/rule/resource identity changed")
        for output in parent.reusable.values():
            cached[output.unit.unit_id] = output
        historical = parent.historical_usage + parent.usage
    session_id = SessionJournal.new_id()
    manifest_value = RunManifest(
        session_id,
        stable_digest(metadata),
        config_identity,
        rules.identity,
        review_input.mode,
        review_input.repo,
        review_input.base_sha,
        review_input.head_sha,
        schema_version=2,
        parent_session=resume,
    )
    result = ReviewResult(
        RunStatus.PREVIEW if preview else RunStatus.PENDING,
        manifest_value,
        warnings=list(rules.warnings),
        historical_usage=historical,
    )
    result.coverage = {file.path: "pending" for file in review_input.targets}
    if parent and parent.damaged_tail:
        result.warnings.append("Recovered completed files; incomplete final journal line ignored")
    if preview:
        result.preparation_seconds = time.monotonic() - preparation_started
        result.warnings.extend(f"{item.path}: {item.reason}" for item in review_input.selection)
        return result
    if review_input.targets:
        settings.reviewer.require("reviewer")
        if review_input.mode != InputMode.SCAN:
            settings.reflection.require("reflection")
    journal = SessionJournal(
        paths,
        session_id,
        (settings.reviewer.api_key, settings.reflection.api_key, settings.judge.api_key),
    )
    recorder = ResultRecorder(journal, file_fingerprints(metadata))
    runner = StageRunner(
        settings, model_factory=model_factory, record_sink=journal.record_interaction
    )
    engine = ReviewEngine(review_input, settings, rules, runner)
    reused_paths = {path for output in cached.values() for path in output.unit.paths}
    saved_paths: set[str] = set(reused_paths)

    async def checkpoint_unit(output: UnitResult) -> None:
        runner.check_recording()
        if not set(output.unit.paths) <= reused_paths:
            recorder.save_unit(output)
            saved_paths.update(output.unit.paths)

    async def checkpoint_batch(batch: ReviewUnit, candidates: list[FindingCandidate]) -> None:
        runner.check_recording()
        recorder.save_batch(batch, candidates)

    try:
        journal.append(
            "session_start",
            {
                "manifest": json_object(asdict(manifest_value)),
                "input": metadata,
                "arguments": {"mode": mode, "scan_path": scan_path, "baseline": False},
                "configuration": settings.public_json(),
                "historical_usage": [json_object(asdict(item)) for item in historical],
                "targets": [file.path for file in review_input.targets],
            },
        )
        if parent:
            for path in reused_paths:
                recorder.save_payload(
                    path, object_field(parent.items[path], "payload"), "reused", resume
                )
        result.status = RunStatus.RUNNING
        result.preparation_seconds = time.monotonic() - preparation_started
        review_started = time.monotonic()
        try:
            await engine.run(
                result,
                completed=cached,
                checkpoint=checkpoint_unit,
                batch_checkpoint=checkpoint_batch,
            )
        except JournalError:
            raise
        except asyncio.CancelledError:
            result.status = (
                RunStatus.PARTIAL if saved_paths or result.candidates else RunStatus.FAILED
            )
            result.warnings.append("Review cancelled; only committed completed files are reusable")
        except Exception as error:
            result.status = (
                RunStatus.PARTIAL if result.candidates or saved_paths else RunStatus.FAILED
            )
            result.warnings.append(f"{type(error).__name__}: {error}")
        finally:
            result.review_seconds = time.monotonic() - review_started
        runner.check_recording()
        journal.check()
        # 已知但失败的文件也保存诊断；未派发文件保持 pending，不冒充完成。
        for path in result.coverage:
            if path not in saved_paths:
                diagnostic = UnitResult(
                    next(
                        (unit for unit in result.groups if path in unit.paths),
                        ReviewUnit(stable_digest([path]), (path,)),
                    ),
                    {path: result.coverage[path]},
                    candidates=[item for item in result.candidates if item.path == path],
                    warnings=["File processing did not settle"],
                    locations=list(result.locations),
                    reflections=list(result.reflections),
                )
                recorder.save_payload(path, encode_file(diagnostic, path), "failed")
        result.usage = list(runner.usage)
        result.tool_calls = [
            record for record in runner.records if record["name"] == "tool_execution"
        ]
        recorder.finish(result)
        return result
    finally:
        journal.close()
