"""候选收集与逐文件完成声明；批量参数先验证后提交。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..contracts import (
    FindingCandidate,
    InputMode,
    JsonObject,
    LocationAttempt,
    LocationDecision,
    ReviewInput,
    Side,
    stable_digest,
)
from ..inputs.snapshots import safe_relative_path


class FindingCollector:
    def __init__(
        self,
        review_input: ReviewInput,
        target_paths: tuple[str, ...],
        identity: str,
        *,
        collection_key: str = "",
        locator: Callable[[FindingCandidate], Awaitable[LocationDecision]] | None = None,
    ) -> None:
        self.input = review_input
        self.target_paths = set(target_paths)
        self.identity = identity
        self.collection_identity = stable_digest(
            [identity, [path for path in sorted(self.target_paths)], collection_key]
        )
        self.candidates: list[FindingCandidate] = []
        self.covered: set[str] = set()
        self.finished = False
        self.failed = False
        self.locator = locator
        self.locations: dict[str, LocationDecision] = {}
        self._location_tasks: list[asyncio.Task[None]] = []

    def code_comment(self, comments: list[JsonObject]) -> str:
        if not comments:
            raise ValueError("comments must contain at least one candidate")
        pending: list[FindingCandidate] = []
        for value in comments:
            candidate = FindingCandidate.from_json(value, self.identity)
            normalized = safe_relative_path(candidate.path)
            if normalized not in self.target_paths:
                raise ValueError("Comment path outside this review unit")
            if candidate.side == Side.LEFT and self.input.mode == InputMode.SCAN:
                raise ValueError("Scan comments must use right-side file coordinates")
            # 同内容的多次提交分别保留，避免定位、复核和历史记录共用 ID。
            pending.append(
                replace(
                    candidate,
                    candidate_id=stable_digest(
                        [
                            self.collection_identity,
                            len(self.candidates) + len(pending),
                            candidate.candidate_id,
                        ]
                    ),
                )
            )
        self.candidates.extend(pending)
        if self.locator is not None:
            for candidate in pending:
                self._location_tasks.append(asyncio.create_task(self._locate(candidate)))
        return f"Recorded {len(pending)} candidate comments"

    async def _locate(self, candidate: FindingCandidate) -> None:
        assert self.locator is not None
        try:
            decision = await self.locator(candidate)
        except Exception as error:
            decision = LocationDecision(
                candidate.candidate_id,
                "unlocated",
                None,
                (LocationAttempt("worker", (), f"{type(error).__name__}: location failed"),),
            )
        self.locations[candidate.candidate_id] = decision

    async def wait_locations(self, *, cancel: bool = False) -> None:
        """复核前排空 worker；取消时回收任务，原始候选台账仍然保留。"""
        if cancel:
            for task in self._location_tasks:
                task.cancel()
        try:
            await asyncio.gather(*self._location_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in self._location_tasks:
                task.cancel()
            await asyncio.gather(*self._location_tasks, return_exceptions=True)
            raise

    def task_done(self, state: str = "DONE", reviewed_paths: list[str] | None = None) -> str:
        if state not in {"DONE", "FAILED"}:
            raise ValueError("Invalid task state")
        paths = (
            reviewed_paths
            if reviewed_paths is not None
            else (sorted(self.target_paths) if len(self.target_paths) == 1 else [])
        )
        normalized = {safe_relative_path(path) for path in paths}
        if not normalized <= self.target_paths:
            raise ValueError("Covered path outside this review unit")
        self.covered = normalized
        self.failed = state == "FAILED"
        self.finished = True
        return f"Task {state}; covered {len(normalized)}/{len(self.target_paths)} files"
