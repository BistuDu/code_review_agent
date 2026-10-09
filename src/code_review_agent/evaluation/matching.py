"""路径、侧、行号及语义约束后的稳定最大基数匹配。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..contracts import JsonObject, Side
from ..sessions.serialization import json_object
from .dataset import Reference
from .judge import SemanticJudge


@dataclass(frozen=True)
class Generated:
    candidate_id: str
    path: str
    side: Side | None
    start_line: int | None
    end_line: int | None
    content: str


def nearby(start: int, end: int, other_start: int, other_end: int, k: int = 1) -> bool:
    return max(start - other_end, other_start - end, 0) <= k


def location_eligible(reference: Reference, generated: Generated, k: int = 1) -> bool:
    if (
        reference.path.replace("\\", "/") != generated.path.replace("\\", "/")
        or reference.side != generated.side
    ):
        return False
    numbers = reference.start_line, reference.end_line, generated.start_line, generated.end_line
    if any(type(number) is not int or number < 1 for number in numbers):
        return False
    assert reference.start_line is not None and reference.end_line is not None
    assert generated.start_line is not None and generated.end_line is not None
    if reference.end_line < reference.start_line or generated.end_line < generated.start_line:
        return False
    return nearby(
        reference.start_line, reference.end_line, generated.start_line, generated.end_line, k
    )


def maximum_matching(edges: dict[str, set[str]]) -> dict[str, str]:
    assigned: dict[str, str] = {}

    def augment(reference: str, visited: set[str]) -> bool:
        for candidate in sorted(edges[reference]):
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in assigned or augment(assigned[candidate], visited):
                assigned[candidate] = reference
                return True
        return False

    for reference in sorted(edges):
        augment(reference, set())
    return {reference: candidate for candidate, reference in sorted(assigned.items())}


async def match_comments(
    references: list[Reference],
    generated: list[Generated],
    judge: SemanticJudge,
    version: str,
    *,
    k: int = 1,
) -> JsonObject:
    if len({item.candidate_id for item in generated}) != len(generated):
        raise ValueError("Generated candidate IDs must be unique")
    valid = [item for item in references if item.label == 1 and item.eligibility == "valid"]
    edges: dict[str, set[str]] = {item.reference_id: set() for item in valid}
    potential: dict[str, set[str]] = {item.reference_id: set() for item in valid}
    verdicts: list[JsonObject] = []
    for reference in sorted(valid, key=lambda item: item.reference_id):
        for candidate in sorted(generated, key=lambda item: item.candidate_id):
            if not location_eligible(reference, candidate, k):
                continue
            judgment = await judge.compare(reference.content, candidate.content, version)
            verdicts.append(
                {
                    "reference_id": reference.reference_id,
                    "candidate_id": candidate.candidate_id,
                    **asdict(judgment),
                }
            )
            if judgment.decision == "match":
                edges[reference.reference_id].add(candidate.candidate_id)
            if judgment.decision != "no_match":
                potential[reference.reference_id].add(candidate.candidate_id)
    matching = maximum_matching(edges)
    upper = maximum_matching(potential)
    return json_object(
        {
            "matched": len(matching),
            "matched_upper": len(upper),
            "references": len(valid),
            "generated": len(generated),
            "matching": matching,
            "undecided": sum(item["decision"] == "undecided" for item in verdicts),
            "verdicts": verdicts,
            "excluded_reference_rows": len(references) - len(valid),
        }
    )
