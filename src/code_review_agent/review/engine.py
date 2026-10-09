"""单元调度、多轮保留、扫描整合和最终资格判定。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..config import Settings
from ..contracts import (
    FindingCandidate,
    InputMode,
    LocationDecision,
    ReflectionDecision,
    ReviewInput,
    ReviewResult,
    ReviewUnit,
    RunStatus,
    UnitResult,
)
from ..inputs.selection import RESOURCE_ROOT
from ..location.pipeline import locate_candidate
from ..location.relocation import relocate
from ..reflection.reviewer import reflect_group
from ..runtime.agentscope_adapter import AgentRun
from ..runtime.deadline import TASK_DEADLINE, TaskDeadline, deadline_scope, effective_deadline
from ..runtime.recording import unit_scope
from ..runtime.stages import StageRunner, render_prompt
from ..tools.context import ContextTools
from ..tools.findings import FindingCollector
from ..tools.registry import ToolRegistry
from .grouping import group_files
from .prompts import candidate_json, prompt_values, review_prompt, scan_prompt
from .rules import RuleResolver

Checkpoint = Callable[[UnitResult], Awaitable[None]]


class ReviewEngine:
    def __init__(
        self,
        review_input: ReviewInput,
        settings: Settings,
        rules: RuleResolver,
        runner: StageRunner,
    ) -> None:
        self.input, self.settings, self.rules, self.runner = review_input, settings, rules, runner
        self.units: list[UnitResult] = []
        self.scan_batches: tuple[ReviewUnit, ...] = ()
        self.summary = ""
        self.cancelled = False
        self._location_semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._pending_scan_locations: dict[str, FindingCollector] = {}
        self._unit_deadlines: dict[str, TaskDeadline] = {}
        self._path_deadlines: dict[str, TaskDeadline] = {}

    async def plan(self, unit: ReviewUnit) -> str:
        files = {file.path: file for file in self.input.files}
        lines = sum(
            len(
                (
                    files[path].content if self.input.mode == InputMode.SCAN else files[path].diff
                ).splitlines()
            )
            for path in unit.paths
        )
        if (self.input.mode != InputMode.SCAN and lines < self.settings.plan_line_threshold) or (
            self.input.mode == InputMode.SCAN and not self.settings.scan_plan
        ):
            return ""
        values = prompt_values(self.input, unit, self.settings, self.rules)
        if self.input.mode == InputMode.SCAN:
            system, user = scan_prompt("PLAN_TASK", values)
            registry = None
        else:
            system = (RESOURCE_ROOT / "prompts/plan_task_system.md").read_text()
            user = render_prompt("plan_task_user.md", values)
            registry = ToolRegistry(
                ContextTools(self.input, self.settings.tool_result_chars), stage="plan"
            )
        result = await self.runner.run(
            "plan",
            system,
            user,
            registry=registry,
            single_call=self.input.mode == InputMode.SCAN,
        )
        if not result.completed:
            raise ValueError(result.error or "Planning incomplete")
        return result.text

    async def review_unit(self, unit: ReviewUnit, *, defer_locations: bool = False) -> UnitResult:
        # 调度方已取得并发槽位；等待槽位不消耗该组的时间额度。
        rounds = 1 if self.input.mode == InputMode.SCAN else self.settings.review_rounds
        seconds = self.settings.group_timeout_minutes * 60 * rounds
        label = "Scan file" if self.input.mode == InputMode.SCAN else "Group"
        budget = TaskDeadline(
            time.monotonic() + seconds if seconds > 0 else None,
            f"{label} timeout: {seconds:g}s deadline exhausted for {', '.join(unit.paths)}",
        )
        self._unit_deadlines[unit.unit_id] = budget
        self._path_deadlines.update(dict.fromkeys(unit.paths, budget))
        with unit_scope(unit.unit_id), deadline_scope(budget):
            timeout = asyncio.timeout_at(budget.at)
            try:
                async with timeout:
                    output = await self._review_unit(unit, defer_locations=defer_locations)
            except TimeoutError:
                output = UnitResult(unit, dict.fromkeys(unit.paths, "failed"))
            if timeout.expired() or budget.expired:
                output.coverage = dict.fromkeys(
                    unit.paths, "partial" if output.candidates else "failed"
                )
                if budget.error not in output.warnings:
                    output.warnings.append(budget.error)
            return output

    def _group_timed_out(self) -> bool:
        budget = TASK_DEADLINE.get()
        return (
            budget is not None
            and budget.expired
            and (self.runner.deadline is None or time.monotonic() < self.runner.deadline)
        )

    def _deadline_for_path(self, path: str) -> TaskDeadline | None:
        return self._path_deadlines.get(path)

    async def _review_unit(self, unit: ReviewUnit, *, defer_locations: bool = False) -> UnitResult:
        output = UnitResult(unit, dict.fromkeys(unit.paths, "pending"))
        try:
            plan = await self.plan(unit)
        except Exception as error:
            plan = ""
            output.warnings.append(f"Planning fallback: {type(error).__name__}")
        rounds = 1 if self.input.mode == InputMode.SCAN else self.settings.review_rounds
        confirmed: list[FindingCandidate] = []
        round_filter = self.input.mode != InputMode.SCAN
        for number in range(rounds):
            collector = FindingCollector(
                self.input,
                unit.paths,
                self.input.snapshot_id,
                collection_key=f"{unit.unit_id}:round{number + 1}",
                locator=self.locate,
            )
            registry = ToolRegistry(
                ContextTools(self.input, self.settings.tool_result_chars), collector
            )
            values = prompt_values(
                self.input,
                unit,
                self.settings,
                self.rules,
                plan if number == 0 or self.input.mode == InputMode.SCAN else "",
                tuple(confirmed if round_filter else output.candidates),
            )
            system, user = review_prompt(self.input, values)
            pinned = json.dumps(
                {
                    "version": self.input.snapshot_id,
                    "paths": unit.paths,
                    "confirmed": [
                        candidate_json(item)
                        for item in (confirmed if round_filter else output.candidates)
                    ],
                    "rules": values["system_rule"],
                    "background": self.settings.background,
                },
                ensure_ascii=False,
            )
            try:
                result = await self.runner.run(
                    f"review.round{number + 1}",
                    system,
                    user,
                    registry=registry,
                    allow_compression=True,
                    pinned_context=pinned,
                )
                if defer_locations:
                    self._pending_scan_locations[unit.unit_id] = collector
                else:
                    await collector.wait_locations()
            except asyncio.CancelledError:
                group_expired = self._group_timed_out()
                if not group_expired:
                    self.cancelled = True
                await collector.wait_locations(cancel=True)
                result = AgentRun(
                    "",
                    False,
                    {},
                    (),
                    effective_deadline(self.runner.deadline).error
                    if group_expired
                    else "Review cancelled",
                )
            except Exception:
                await collector.wait_locations(cancel=True)
                raise
            new_candidates = list(collector.candidates)
            added = len(new_candidates)
            output.candidates.extend(new_candidates)
            output.locations.extend(
                collector.locations[item.candidate_id]
                for item in new_candidates
                if item.candidate_id in collector.locations
            )
            if round_filter and not self.cancelled:
                # 每轮先定位/复核；原始台账保持完整，只把未被拒绝的发现传给下一轮。
                added = 0
                effective = tuple(
                    self.effective_candidate(item, collector.locations.get(item.candidate_id))
                    for item in new_candidates
                )
                try:
                    reflections = (
                        await reflect_group(
                            effective, self.input, self.runner, target_paths=unit.paths
                        )
                        if effective
                        else []
                    )
                except asyncio.CancelledError:
                    group_expired = self._group_timed_out()
                    if not group_expired:
                        self.cancelled = True
                    output.coverage = dict.fromkeys(unit.paths, "partial")
                    output.warnings.append(
                        effective_deadline(self.runner.deadline).error
                        if group_expired
                        else "Reflection cancelled; submitted candidates retained"
                    )
                    return output
                output.reflections.extend(reflections)
                for candidate, reflection in zip(effective, reflections, strict=True):
                    if reflection.decision != "reject":
                        confirmed.append(candidate)
                        added += 1
            if result.completed:
                for path in collector.covered:
                    output.coverage[path] = "reviewed"
                if collector.covered != set(unit.paths):
                    output.warnings.append("task_done omitted files; those files remain incomplete")
            else:
                output.warnings.append(result.error or "Review did not call task_done")
                # 后轮失败保留前轮候选，但整单元不能标为可复用完成。
                output.coverage = dict.fromkeys(
                    unit.paths, "partial" if output.candidates else "failed"
                )
                break
            if not added or (round_filter and len(confirmed) >= 30):
                break
        return output

    async def complete_scan_locations(self, output: UnitResult, *, cancel: bool = False) -> None:
        """批次末尾回收 Scan worker，主评审结束即可释放文件并发槽位。"""
        collector = self._pending_scan_locations.pop(output.unit.unit_id, None)
        if collector is None:
            return
        try:
            await collector.wait_locations(cancel=cancel)
        finally:
            existing = {item.candidate_id for item in output.locations}
            output.locations.extend(
                decision
                for candidate_id, decision in collector.locations.items()
                if candidate_id not in existing
            )

    async def run(
        self,
        result: ReviewResult,
        *,
        completed: dict[str, UnitResult] | None = None,
        checkpoint: Checkpoint | None = None,
        planned: list[ReviewUnit] | None = None,
        completed_locations: dict[str, LocationDecision] | None = None,
        completed_reflections: dict[str, ReflectionDecision] | None = None,
        batch_checkpoint: Callable[[ReviewUnit, list[FindingCandidate]], Awaitable[None]]
        | None = None,
    ) -> ReviewResult:
        if self.input.mode == InputMode.SCAN:
            from .scan_engine import run_scan

            return await run_scan(
                self,
                result,
                completed=completed,
                checkpoint=checkpoint,
                planned=planned,
                completed_locations=completed_locations,
                batch_checkpoint=batch_checkpoint,
            )
        paths = tuple(file.path for file in self.input.targets)
        if not paths:
            result.status = RunStatus.NO_FILES
            return result
        reusable_units = [output.unit for output in (completed or {}).values()]
        reusable_paths = {path for unit in reusable_units for path in unit.paths}
        pending_paths = tuple(path for path in paths if path not in reusable_paths)
        if planned is not None:
            groups = reusable_units + [
                unit for unit in planned if not set(unit.paths) & reusable_paths
            ]
        else:
            groups = reusable_units + (
                await group_files(pending_paths, self.settings, self.runner)
                if pending_paths
                else []
            )
        result.groups = groups
        result.coverage = dict.fromkeys(paths, "pending")
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def execute(unit: ReviewUnit) -> None:
            cached = (completed or {}).get(unit.unit_id)
            if cached is not None:
                output = cached
                result.reused_units += 1
            else:
                result.rerun_units += 1
                async with semaphore:
                    output = await self.review_unit(unit)
            self.units.append(output)
            result.coverage.update(output.coverage)
            result.candidates.extend(output.candidates)
            result.locations.extend(output.locations)
            result.reflections.extend(output.reflections)
            result.warnings.extend(output.warnings)
            if checkpoint:
                await checkpoint(output)

        tasks = [asyncio.create_task(execute(unit)) for unit in groups]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            # 写入失败等异常不能留下继续向已关闭 journal 写入的并发任务。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except asyncio.CancelledError:
            self.cancelled = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            result.warnings.append("Cancelled; pending files have not completed")
        result.candidates.sort(key=lambda item: (item.path, item.candidate_id))
        if self.cancelled:
            result.status = (
                RunStatus.PARTIAL
                if result.candidates
                or any(state == "reviewed" for state in result.coverage.values())
                else RunStatus.FAILED
            )
            result.usage = list(self.runner.usage)
            return result
        location_cache = {item.candidate_id: item for item in result.locations}
        reflection_cache = {item.candidate_id: item for item in result.reflections}
        for candidate in result.candidates:
            if candidate.candidate_id in result.suppressed:
                continue
            if candidate.candidate_id not in location_cache:
                decision = (completed_locations or {}).get(candidate.candidate_id)
                if decision is None:
                    with deadline_scope(self._deadline_for_path(candidate.path)):
                        decision = await self.locate(candidate)
                result.locations.append(decision)
                location_cache[candidate.candidate_id] = decision
            reflection = (completed_reflections or {}).get(candidate.candidate_id)
            if reflection and candidate.candidate_id not in reflection_cache:
                result.reflections.append(reflection)
                reflection_cache[candidate.candidate_id] = reflection
        for unit in groups:
            pending = tuple(
                self.effective_candidate(
                    item,
                    location_cache.get(item.candidate_id),
                    result.rewritten_content.get(item.candidate_id),
                )
                for item in result.candidates
                if item.path in unit.paths
                and item.candidate_id not in reflection_cache
                and item.candidate_id not in result.suppressed
            )
            if pending:
                with deadline_scope(self._unit_deadlines.get(unit.unit_id)):
                    decisions = await reflect_group(
                        pending, self.input, self.runner, target_paths=unit.paths
                    )
                result.reflections.extend(decisions)
                reflection_cache.update({item.candidate_id: item for item in decisions})
        result.project_summary = self.summary
        result.usage = list(self.runner.usage)
        reviewed = sum(state == "reviewed" for state in result.coverage.values())
        result.status = (
            RunStatus.COMPLETED
            if reviewed == len(paths)
            else (RunStatus.PARTIAL if reviewed or result.candidates else RunStatus.FAILED)
        )
        return result

    async def locate(self, candidate: FindingCandidate) -> LocationDecision:
        async def relocalize(item: FindingCandidate) -> str | None:
            return await relocate(item, self.input, self.runner)

        with deadline_scope(TASK_DEADLINE.get() or self._deadline_for_path(candidate.path)):
            async with self._location_semaphore:
                return await locate_candidate(
                    candidate,
                    self.input,
                    level=3,
                    cross_file=self.input.mode != InputMode.SCAN,
                    relocator=relocalize,
                )

    @staticmethod
    def effective_candidate(
        candidate: FindingCandidate,
        location: LocationDecision | None,
        content: str | None = None,
    ) -> FindingCandidate:
        """复核和下一轮使用定位结果，原始候选台账保持不变。"""
        if location and location.location:
            candidate = replace(
                candidate,
                path=location.location.path,
                side=location.location.side,
                proposed_start=location.location.start_line,
                proposed_end=location.location.end_line,
                existing_code=location.location.snippet,
            )
        return replace(candidate, content=content) if content is not None else candidate

    async def deduplicate_batch(
        self, result: ReviewResult, batch: ReviewUnit
    ) -> dict[str, list[FindingCandidate]] | None:
        from .scan_engine import deduplicate_batch

        return await deduplicate_batch(self, result, batch)
