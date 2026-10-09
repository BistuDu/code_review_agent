"""Go 兼容定位顺序；记录匹配依据，不以定位失败删除评论。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..contracts import (
    FindingCandidate,
    InputMode,
    Location,
    LocationAttempt,
    LocationDecision,
    ReviewInput,
    Side,
)
from .cross_file import cross_file_matches
from .file_scan import file_matches
from .hunk import hunk_matches

Relocator = Callable[[FindingCandidate], Awaitable[str | None]]
LOCATION_POLICY = "go-compatible-v1"


async def locate_candidate(
    candidate: FindingCandidate,
    review_input: ReviewInput,
    *,
    level: int = 3,
    cross_file: bool = True,
    relocator: Relocator | None = None,
) -> LocationDecision:
    if level not in {1, 2, 3}:
        raise ValueError("Location level must be 1, 2 or 3")
    files = {file.path: file for file in review_input.targets}
    files.update({file.old_path: file for file in review_input.targets if file.old_path})
    attempts: list[LocationAttempt] = []
    file = files.get(candidate.path)

    if file is not None and (candidate.proposed_start or candidate.proposed_end):
        start, end = candidate.proposed_start, candidate.proposed_end
        location = (
            Location(candidate.path, candidate.side, start, end, candidate.existing_code)
            if start is not None and end is not None and end >= start
            else None
        )
        # Go 接受任一正行号；不完整/逆序坐标通过原始候选输出，不伪造有效区间。
        return LocationDecision(
            candidate.candidate_id,
            "provided",
            location,
            (
                LocationAttempt(
                    "provided", (), "Accepted supplied coordinates without text matching"
                ),
            ),
        )

    def search_own(item: FindingCandidate, prefix: str = "") -> LocationDecision | None:
        if file is None:
            attempts.append(LocationAttempt(prefix + "scope", (), "Path outside review targets"))
            return None
        if review_input.mode == InputMode.SCAN:
            stages = [("scan", file_matches(file, item.existing_code, Side.RIGHT))]
        else:
            # 所有 Hunk 的新侧先于任何旧侧；评论指定的 side 不限制文本匹配。
            matches = hunk_matches(file, item.existing_code, Side.RIGHT) or hunk_matches(
                file, item.existing_code, Side.LEFT
            )
            stages = [("hunk", matches)]
            if level >= 2:
                stages.append(("file", file_matches(file, item.existing_code, Side.RIGHT)))
        for name, matches in stages:
            attempts.append(
                LocationAttempt(
                    prefix + name, matches, "First text match" if matches else "No text match"
                )
            )
            if matches:
                return LocationDecision(
                    candidate.candidate_id, "verified", matches[0], tuple(attempts)
                )
        return None

    found = search_own(candidate)
    if found:
        return found
    ambiguous = False
    if cross_file:
        matches = cross_file_matches(review_input.targets, candidate.path, candidate.existing_code)
        ambiguous = len(matches) > 1
        attempts.append(
            LocationAttempt(
                "cross_file",
                matches,
                "Multiple matching files"
                if ambiguous
                else "Unique matching file"
                if matches
                else "No text match",
            )
        )
        if len(matches) == 1:
            return LocationDecision(candidate.candidate_id, "verified", matches[0], tuple(attempts))
    if file is not None and level == 3 and relocator is not None:
        try:
            snippet = await relocator(candidate)
            attempts.append(
                LocationAttempt(
                    "relocation", (), "Returned snippet" if snippet else "No valid snippet"
                )
            )
            if snippet:
                # Re-location 只重试原文件，失败时保留原始 ExistingCode。
                found = search_own(replace(candidate, existing_code=snippet), "relocation.")
                if found:
                    return found
        except Exception as error:
            attempts.append(
                LocationAttempt("relocation", (), f"{type(error).__name__}: relocation failed")
            )
    return LocationDecision(
        candidate.candidate_id, "ambiguous" if ambiguous else "unlocated", None, tuple(attempts)
    )
