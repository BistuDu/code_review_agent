"""独立 Scan 编排：批次串行、文件并发、定位后替换去重、批后检查点。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from typing import Protocol

from ..config import Settings
from ..contracts import (
    FindingCandidate,
    LocationDecision,
    ReviewInput,
    ReviewResult,
    ReviewUnit,
    RunStatus,
    UnitResult,
    stable_digest,
)
from ..inputs.scan import scan_units
from ..runtime.recording import unit_scope
from ..runtime.stages import StageRunner, parse_json_response
from .prompts import scan_prompt

SCAN_POLICY = "scan-batches-v1"
Checkpoint = Callable[[UnitResult], Awaitable[None]]


class ScanReviewer(Protocol):
    """Scan 只依赖单文件评审能力，不反向导入 Diff 编排。"""

    input: ReviewInput
    settings: Settings
    runner: StageRunner
    units: list[UnitResult]
    scan_batches: tuple[ReviewUnit, ...]
    summary: str
    cancelled: bool

    async def review_unit(
        self, unit: ReviewUnit, *, defer_locations: bool = False
    ) -> UnitResult: ...

    async def complete_scan_locations(
        self, output: UnitResult, *, cancel: bool = False
    ) -> None: ...

    async def locate(self, candidate: FindingCandidate) -> LocationDecision: ...

    def effective_candidate(
        self, candidate: FindingCandidate, location: LocationDecision | None
    ) -> FindingCandidate: ...


async def deduplicate_batch(
    engine: ScanReviewer, result: ReviewResult, batch: ReviewUnit
) -> dict[str, list[FindingCandidate]] | None:
    candidates = [item for item in result.candidates if item.path in batch.paths]
    if not engine.settings.scan_dedup or len(candidates) < engine.settings.dedup_min_comments:
        return None
    values = {
        "batch_comments": json.dumps(
            [
                {
                    "id": f"c-{index}",
                    "path": item.path,
                    "content": item.content,
                    **({"existing_code": item.existing_code} if item.existing_code else {}),
                }
                for index, item in enumerate(candidates)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
    try:
        system, user = scan_prompt("DEDUP_TASK", values)
        response = await engine.runner.run("scan.dedup", system, user, single_call=True)
        if not response.completed:
            raise ValueError("Dedup request failed")
        value = parse_json_response(response.text)
        if not isinstance(value, dict):
            raise ValueError("Invalid dedup response")
        groups = value.get("groups")
        if not isinstance(groups, list):
            raise ValueError("Invalid dedup response")
        by_id = {f"c-{index}": item for index, item in enumerate(candidates)}
        seen: set[str] = set()
        final: list[FindingCandidate] = []
        safe_checkpoints: dict[str, list[FindingCandidate]] = {}
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Invalid dedup group")
            members = group.get("members")
            if not isinstance(members, list):
                raise ValueError("Invalid dedup group")
            if not members or any(
                not isinstance(item, str) or item not in by_id or item in seen for item in members
            ):
                raise ValueError("Unknown or repeated dedup id")
            identifiers = [str(item) for item in members]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Duplicate assignment")
            seen.update(identifiers)
            originals = [by_id[item] for item in identifiers]
            canonical = originals[0]
            merged = group.get("merged_content", "")
            if merged is not None and not isinstance(merged, str):
                raise ValueError("Invalid merged content")
            if len(originals) > 1 and merged:
                canonical = replace(canonical, content=merged)
            final.append(canonical)
            if len({item.path for item in originals}) == 1:
                safe_checkpoints.setdefault(canonical.path, []).append(canonical)
            else:
                # 跨文件合并保留各文件原评论，避免单文件重跑时丢失其他文件的发现。
                for item in originals:
                    safe_checkpoints.setdefault(item.path, []).append(item)
        if seen != set(by_id):
            raise ValueError("Dedup omitted candidates")
        if len(final) == len(candidates):
            return None
        if not result.raw_candidates:
            result.raw_candidates.extend(result.candidates)
        candidate_ids = {item.candidate_id for item in candidates}
        first = next(
            index
            for index, item in enumerate(result.candidates)
            if item.candidate_id in candidate_ids
        )
        remaining = [item for item in result.candidates if item.candidate_id not in candidate_ids]
        result.candidates[:] = remaining[:first] + final + remaining[first:]
        return safe_checkpoints
    except Exception as error:
        result.warnings.append(f"Dedup fallback: {type(error).__name__}")
        return None


async def run_scan(
    engine: ScanReviewer,
    result: ReviewResult,
    *,
    completed: dict[str, UnitResult] | None = None,
    checkpoint: Checkpoint | None = None,
    planned: list[ReviewUnit] | None = None,
    completed_locations: dict[str, LocationDecision] | None = None,
    batch_checkpoint: Callable[[ReviewUnit, list[FindingCandidate]], Awaitable[None]] | None = None,
) -> ReviewResult:
    paths = tuple(file.path for file in engine.input.targets)
    if not paths:
        result.status = RunStatus.NO_FILES
        return result
    groups = planned or [
        ReviewUnit(stable_digest([path]), (path,), "scan per-file") for path in paths
    ]
    if any(len(unit.paths) != 1 for unit in groups) or sorted(
        path for unit in groups for path in unit.paths
    ) != sorted(paths):
        raise ValueError("Scan requires a complete partition of single-file units")
    result.groups = groups
    result.coverage = dict.fromkeys(paths, "pending")
    engine.scan_batches = scan_units(
        paths, engine.settings.batch_strategy, engine.settings.batch_size
    )
    result.scan_batches = list(engine.scan_batches)
    by_path = {unit.paths[0]: unit for unit in groups}
    semaphore = asyncio.Semaphore(engine.settings.max_concurrency)

    async def execute(unit: ReviewUnit) -> UnitResult:
        cached = (completed or {}).get(unit.unit_id)
        if cached is not None:
            result.reused_units += 1
            output = deepcopy(cached)
        else:
            result.rerun_units += 1
            try:
                async with semaphore:
                    output = await engine.review_unit(unit, defer_locations=True)
            except Exception as error:
                output = UnitResult(
                    unit,
                    dict.fromkeys(unit.paths, "failed"),
                    warnings=[f"Scan failed: {type(error).__name__}"],
                )
        return output

    async def finish_locations(output: UnitResult) -> None:
        await engine.complete_scan_locations(output, cancel=engine.cancelled)
        location_map = {item.candidate_id: item for item in output.locations}
        for candidate in output.candidates:
            if candidate.candidate_id not in location_map and not engine.cancelled:
                location = (completed_locations or {}).get(candidate.candidate_id)
                if location is None:
                    location = await engine.locate(candidate)
                output.locations.append(location)
                location_map[candidate.candidate_id] = location

    def collect(output: UnitResult) -> None:
        location_map = {item.candidate_id: item for item in output.locations}
        if not output.raw_candidates:
            output.raw_candidates = list(output.candidates)
        result.raw_candidates.extend(output.raw_candidates)
        output.candidates = [
            engine.effective_candidate(item, location_map.get(item.candidate_id))
            for item in output.candidates
        ]
        result.candidates.extend(output.candidates)
        result.locations.extend(output.locations)
        result.coverage.update(output.coverage)
        result.warnings.extend(output.warnings)

    for batch in engine.scan_batches:
        tasks = [asyncio.create_task(execute(by_path[path])) for path in batch.paths]
        try:
            outputs = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            engine.cancelled = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            outputs = [
                task.result() for task in tasks if not task.cancelled() and task.exception() is None
            ]
        location_tasks = [asyncio.create_task(finish_locations(output)) for output in outputs]
        try:
            await asyncio.gather(*location_tasks)
        except asyncio.CancelledError:
            engine.cancelled = True
            for location_task in location_tasks:
                location_task.cancel()
            await asyncio.gather(*location_tasks, return_exceptions=True)
            await asyncio.gather(
                *(engine.complete_scan_locations(output, cancel=True) for output in outputs)
            )
            result.warnings.append("Scan locations cancelled; submitted comments retained")
        for output in outputs:
            collect(output)
        try:
            with unit_scope(batch.unit_id):
                safe = None if engine.cancelled else await deduplicate_batch(engine, result, batch)
        except asyncio.CancelledError:
            engine.cancelled = True
            safe = None
            result.warnings.append("Scan dedup cancelled; original batch comments retained")
        for output in outputs:
            if engine.cancelled:
                output.coverage = dict.fromkeys(output.unit.paths, "partial")
            if output.unit.unit_id not in (completed or {}):
                path = output.unit.paths[0]
                output.candidates = (
                    safe.get(path, [])
                    if safe is not None
                    else [item for item in result.candidates if item.path == path]
                )
            engine.units.append(output)
            if checkpoint:
                await checkpoint(output)
        if batch_checkpoint:
            await batch_checkpoint(
                batch, [item for item in result.candidates if item.path in batch.paths]
            )
        if engine.cancelled:
            break
    if not engine.cancelled and engine.settings.scan_summary and result.candidates:
        values = {
            "comment_count": str(len(result.candidates)),
            "file_count": str(len({item.path for item in result.candidates})),
            "all_comments": "".join(
                f"- `{item.path}`: {_summary_content(item.content)}\n" for item in result.candidates
            ),
        }
        try:
            system, user = scan_prompt("PROJECT_SUMMARY_TASK", values)
            summary = await engine.runner.run("scan.summary", system, user, single_call=True)
            if summary.completed:
                result.project_summary = engine.summary = summary.text.strip()
            else:
                result.warnings.append("Project summary incomplete")
        except asyncio.CancelledError:
            engine.cancelled = True
            result.warnings.append("Project summary cancelled; scan comments retained")
        except Exception as error:
            result.warnings.append(f"Project summary failed: {type(error).__name__}")
    result.usage = list(engine.runner.usage)
    reviewed = sum(state == "reviewed" for state in result.coverage.values())
    result.status = (
        RunStatus.COMPLETED
        if reviewed == len(paths) and not engine.cancelled
        else RunStatus.PARTIAL
        if reviewed or result.candidates
        else RunStatus.FAILED
    )
    return result


def _summary_content(content: str) -> str:
    text = content.replace("\n", " ")
    encoded = text.encode()
    return encoded[:280].decode(errors="replace") + "..." if len(encoded) > 280 else text
