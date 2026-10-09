from pathlib import Path

import pytest

from code_review_agent.config import Settings
from code_review_agent.contracts import FileSnapshot, FindingCandidate, InputMode, ReviewInput, Side
from code_review_agent.inputs.diff import parse_hunks
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.location.pipeline import locate_candidate


def candidate(path="a.py", snippet="danger()", **extra):
    return FindingCandidate.from_json(
        {
            "path": path,
            "existing_code": snippet,
            "content": "dangerous operation",
            "category": "bug",
            "severity": "high",
            **extra,
        },
        "input",
    )


def input_with_files(*files):
    from code_review_agent.contracts import FileSelection

    return ReviewInput(
        "repo",
        InputMode.RANGE,
        "base",
        "head",
        files,
        tuple(FileSelection(file.path, True, "included") for file in files),
        "snapshot",
    )


@pytest.mark.asyncio
async def test_hunk_uses_absolute_new_and_old_lines_and_trusts_proposed_coordinates():
    diff = "@@ -10 +12 @@\n-old()\n+danger()\n"
    file = FileSnapshot(
        "a.py", "\n" * 11 + "danger()\n", "\n" * 9 + "old()\n", diff, parse_hunks(diff)
    )
    frozen = input_with_files(file)
    provided = await locate_candidate(candidate(start_line=1, end_line=1000), frozen)
    assert provided.status == "provided"
    assert provided.location.start_line == 1 and provided.location.end_line == 1000
    new = await locate_candidate(candidate(side="left"), frozen)
    assert new.location.start_line == new.location.end_line == 12
    assert new.location.side == Side.RIGHT
    old = await locate_candidate(candidate(snippet="old()"), frozen)
    assert old.location.start_line == 10 and old.location.side == Side.LEFT


@pytest.mark.asyncio
async def test_l1_l2_l3_and_cross_file_are_separate():
    file = FileSnapshot("a.py", "safe()\ndanger()\n")
    other = FileSnapshot("b.py", "elsewhere()\n")
    frozen = input_with_files(file, other)
    first = await locate_candidate(candidate(), frozen, level=1, cross_file=False)
    second = await locate_candidate(candidate(), frozen, level=2, cross_file=False)

    async def relocate(item):
        return "danger()"

    third = await locate_candidate(
        candidate(snippet="invented()"), frozen, cross_file=False, relocator=relocate
    )
    assert first.status == "unlocated"
    assert second.location.start_line == 2
    assert third.location.start_line == 2
    assert third.attempts[-1].stage == "relocation.file"
    crossing = await locate_candidate(candidate(snippet="elsewhere()"), frozen)
    assert crossing.location.path == "b.py"
    without_cross = await locate_candidate(
        candidate(snippet="elsewhere()"), frozen, level=2, cross_file=False
    )
    assert without_cross.status == "unlocated"


@pytest.mark.asyncio
async def test_duplicate_text_uses_first_match_and_fake_relocation_stays_unlocated():
    frozen = input_with_files(FileSnapshot("a.py", "danger()\ndanger()\n"))
    duplicate = await locate_candidate(candidate(), frozen, cross_file=False)
    assert duplicate.status == "verified" and duplicate.location.start_line == 1
    assert len(duplicate.attempts[-1].matches) == 2

    async def invented(item):
        return "nonexistent()"

    failed = await locate_candidate(
        candidate(snippet="missing()"), frozen, cross_file=False, relocator=invented
    )
    assert failed.status == "unlocated"
    assert failed.location is None


@pytest.mark.asyncio
async def test_scan_has_real_file_coordinates_without_hunks(tmp_path: Path):
    (tmp_path / "a.py").write_text("safe()\ndanger()\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    result = await locate_candidate(candidate(), frozen)
    assert result.location.start_line == 2 and result.attempts[0].stage == "scan"


@pytest.mark.asyncio
async def test_new_side_in_later_hunk_precedes_old_side_in_first_hunk():
    diff = "@@ -1 +1 @@\n-danger()\n+safe()\n@@ -10 +10 @@\n-safe()\n+danger()\n"
    frozen = input_with_files(FileSnapshot("a.py", "", "", diff, parse_hunks(diff)))
    decision = await locate_candidate(candidate(side="left"), frozen)
    assert decision.location.side == Side.RIGHT
    assert decision.location.start_line == 10


@pytest.mark.asyncio
async def test_full_file_skips_blank_lines_but_hunk_preserves_them():
    diff = "@@ -1,3 +1,3 @@\n danger()\n \n next()\n"
    frozen = input_with_files(
        FileSnapshot("a.py", "danger()\n\nnext()\n", "", diff, parse_hunks(diff))
    )
    snippet = "+ danger()\n\n- next()"
    hunk = await locate_candidate(candidate(snippet=snippet), frozen, level=1, cross_file=False)
    full = await locate_candidate(candidate(snippet=snippet), frozen, level=2, cross_file=False)
    assert hunk.status == "unlocated"
    assert full.location.start_line == 1 and full.location.end_line == 3
    assert full.attempts[-1].stage == "file"


@pytest.mark.asyncio
async def test_cross_file_accepts_first_match_per_file_and_declines_multiple_files():
    diff = "@@ -10 +12 @@\n-old()\n+danger()\n"
    original = FileSnapshot("a.py", "safe()\n")
    repeated = FileSnapshot("b.py", "danger()\ndanger()\n", "", diff, parse_hunks(diff))
    one = await locate_candidate(candidate(), input_with_files(original, repeated))
    assert one.location.path == "b.py" and one.location.start_line == 12
    ambiguous = await locate_candidate(
        candidate(), input_with_files(original, repeated, FileSnapshot("c.py", "danger()\n"))
    )
    assert ambiguous.status == "ambiguous" and ambiguous.location is None


@pytest.mark.asyncio
async def test_relocation_does_not_search_other_files_again():
    frozen = input_with_files(FileSnapshot("a.py", "safe()"), FileSnapshot("b.py", "danger()"))

    async def relocate(item):
        return "danger()"

    decision = await locate_candidate(candidate(snippet="missing()"), frozen, relocator=relocate)
    assert decision.status == "unlocated"
    assert decision.attempts[-1].stage == "relocation.file"
    assert all(item.stage != "relocation.cross_file" for item in decision.attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coordinates", [{"start_line": 8}, {"end_line": 8}, {"start_line": 8, "end_line": 2}]
)
async def test_partial_or_reversed_supplied_coordinates_are_accepted(coordinates):
    decision = await locate_candidate(
        candidate(snippet="missing()", **coordinates),
        input_with_files(FileSnapshot("a.py", "safe()")),
    )
    assert decision.status == "provided" and decision.location is None
    assert [attempt.stage for attempt in decision.attempts] == ["provided"]
