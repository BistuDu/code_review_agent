from dataclasses import replace

from code_review_agent.contracts import (
    FindingCandidate,
    Location,
    LocationDecision,
    ReflectionDecision,
    ReviewUnit,
    Side,
    UnitResult,
)
from code_review_agent.sessions.results import decode_file, encode_file


def candidate(identity="one"):
    return FindingCandidate(identity, "a.py", "issue", "call()", "bug", "high")


def test_duplicate_submissions_and_relocated_display_keep_source_ownership():
    candidates = [candidate("first"), candidate("second")]
    output = UnitResult(
        ReviewUnit("group", ("a.py", "b.py")),
        {"a.py": "reviewed", "b.py": "failed"},
        candidates=candidates,
        locations=[
            LocationDecision(
                item.candidate_id, "verified", Location("b.py", Side.RIGHT, 2, 2, "call()"), ()
            )
            for item in candidates
        ],
        reflections=[
            ReflectionDecision(item.candidate_id, "keep", "reviewed") for item in candidates
        ],
    )
    payload = encode_file(output, "a.py")
    assert len(payload["findings"]) == 2 and len(set(payload["restore_refs"])) == 2
    restored = decode_file("a.py", payload)
    assert restored.candidates == candidates
    assert [item.location.path for item in restored.locations] == ["b.py", "b.py"]
    assert restored.coverage == {"a.py": "reviewed"}
    assert not encode_file(output, "b.py")["findings"]


def test_raw_and_same_file_canonical_are_distinct_but_not_duplicated_when_equal():
    raw = [candidate("first"), candidate("second")]
    canonical = replace(raw[0], content="merged issue")
    output = UnitResult(
        ReviewUnit("group", ("a.py",)),
        {"a.py": "reviewed"},
        candidates=[canonical],
        raw_candidates=raw,
    )
    payload = encode_file(output, "a.py")
    assert len(payload["findings"]) == 3
    assert decode_file("a.py", payload).raw_candidates == raw
    output.raw_candidates = [canonical]
    assert len(encode_file(output, "a.py")["findings"]) == 1


def test_failed_provisional_result_is_preserved_as_diagnostic_payload():
    output = UnitResult(
        ReviewUnit("group", ("a.py",)),
        {"a.py": "partial"},
        candidates=[candidate()],
        warnings=["round interrupted"],
    )
    restored = decode_file("a.py", encode_file(output, "a.py"))
    assert restored.coverage["a.py"] == "partial"
    assert restored.candidates == output.candidates and restored.warnings == output.warnings
