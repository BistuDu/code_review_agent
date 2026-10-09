import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from code_review_agent.config import ModelSettings, Settings
from code_review_agent.contracts import FileSnapshot, FindingCandidate
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.location.pipeline import locate_candidate
from code_review_agent.location.relocation import relocate
from code_review_agent.reflection.reviewer import reflect_group
from code_review_agent.runtime.model_factory import create_model
from code_review_agent.runtime.stages import StageRunner


def runner_with_response(content, requests, settings=None, *, tool_calls=None, status_code=200):
    settings = settings or ModelSettings(
        base_url="https://example.invalid/v1", model="test", api_key="test-key", retries=0
    )

    def factory(configuration, stage, usage):
        model = create_model(configuration, stage, usage)
        original_client = model.client

        async def handler(request):
            await original_client.close()
            body = json.loads(request.content)
            requests.append(body)
            if status_code != 200:
                return httpx.Response(
                    status_code, json={"error": {"message": "mock service failure"}}
                )
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": content,
                                **({"tool_calls": tool_calls} if tool_calls else {}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )

        model.client = AsyncOpenAI(
            api_key="test-key",
            base_url=settings.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return model

    return StageRunner(replace(Settings(), reviewer=settings, reflection=settings), factory)


def finding():
    return FindingCandidate.from_json(
        {
            "path": "a.py",
            "existing_code": "return x",
            "content": "must be broken",
            "category": "bug",
            "severity": "high",
        },
        "frozen",
    )


async def reflect_candidate(candidate, frozen, runner):
    return (await reflect_group((candidate,), frozen, runner, target_paths=(candidate.path,)))[0]


@pytest.mark.asyncio
async def test_isolated_filter_includes_go_user_constraints(tmp_path: Path):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    requests = []
    decision = await reflect_candidate(finding(), frozen, runner_with_response("[]", requests))
    assert decision.decision == "keep" and len(requests) == 1
    text = json.dumps(requests[0])
    assert "Ground A" in text and "Ground B" in text
    assert "protected-subject veto" in text and "Memory safety" in text
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {
        "report_incorrect_comments",
        "approve_all_comments",
    }
    assert "label" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        'Analysis before JSON\n{"decision":"keep","reason":"valid","evidence":[]}',
        '{"decision":"reject","reason":"probably wrong","evidence":[]}',
        '{"decision":"reject","reason":"wrong","evidence":["a.py:999: imagined fact"]}',
    ],
)
async def test_invalid_filter_response_is_retained(tmp_path: Path, content):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    decision = await reflect_candidate(finding(), frozen, runner_with_response(content, []))
    assert decision.decision == "undecided"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_text",
    [
        "```python\nreturn x\n```",
        "Explanation\n```python\nreturn x\n```\n```python\nreturn invented\n```\nDone",
    ],
)
async def test_verified_rejection_has_evidence_and_relocation_is_text_checked(
    tmp_path: Path, response_text
):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    rejected = await reflect_candidate(
        finding(),
        frozen,
        runner_with_response(
            '["c-0"]',
            [],
        ),
    )
    assert rejected.decision == "reject"
    invalid = replace(finding(), existing_code="return missing")
    runner = runner_with_response("```python\nreturn invented\n```", [])

    async def call_relocation(candidate):
        return await relocate(candidate, frozen, runner)

    location = await locate_candidate(invalid, frozen, cross_file=False, relocator=call_relocation)
    assert location.status == "unlocated" and location.location is None
    correct = runner_with_response(response_text, [])

    async def correct_relocation(candidate):
        return await relocate(candidate, frozen, correct)

    located = await locate_candidate(
        invalid, frozen, cross_file=False, relocator=correct_relocation
    )
    assert located.location.start_line == 1


def filter_call(name, arguments):
    return {
        "id": "filter",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.mark.asyncio
async def test_group_filter_uses_one_request_two_tools_and_only_group_diffs():
    from test_location import input_with_files

    frozen = input_with_files(
        FileSnapshot("a.py", "FULL_SOURCE_SECRET_SENTINEL", diff="@@ -1 +1 @@\n-old()\n+new()"),
        FileSnapshot("b.py", "FULL_SOURCE_OTHER_SENTINEL", diff="@@ -1 +1 @@\n-old_b()\n+new_b()"),
        FileSnapshot("outside.py", "OUTSIDE_SOURCE", diff="OUTSIDE_DIFF"),
    )
    candidates = (
        finding(),
        replace(finding(), candidate_id="b", path="b.py"),
        replace(finding(), candidate_id="a2"),
    )
    requests = []
    runner = runner_with_response(
        None,
        requests,
        tool_calls=[
            filter_call(
                "report_incorrect_comments",
                {
                    "analysis": ["c-1: a specific diff line disproves this comment"],
                    "comment_ids": ["c-1", "c-999", "invalid", "c-1"],
                },
            )
        ],
    )
    runner.settings = replace(runner.settings, background="BACKGROUND_NOT_FOR_FILTER")
    decisions = await reflect_group(candidates, frozen, runner, target_paths=("a.py", "b.py"))
    assert [item.candidate_id for item in decisions] == [item.candidate_id for item in candidates]
    assert [item.decision for item in decisions] == ["keep", "keep", "reject"]
    assert len(requests) == len(runner.usage) == len(runner.records) == 1
    body = requests[0]
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    user = "".join(block["text"] for block in body["messages"][1]["content"])
    assert '<file path="a.py">' in user and '<file path="b.py">' in user
    assert "+new_b()" in user and "FULL_SOURCE" not in user and "OUTSIDE" not in user
    assert "BACKGROUND_NOT_FOR_FILTER" not in user
    assert "Ground A" in user and "Ground B" in user and "Step 1 — protected-subject veto" in user
    assert "{{diff}}" not in user and "{{comments}}" not in user
    tools = body["tools"]
    assert [item["function"]["name"] for item in tools] == [
        "report_incorrect_comments",
        "approve_all_comments",
    ]
    assert list(tools[0]["function"]["parameters"]["properties"]) == ["analysis", "comment_ids"]
    assert runner.records[0]["name"] == "model_call"
    assert len(runner.records[0]["value"]["response"]["content"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "calls,text,expected",
    [
        ([filter_call("approve_all_comments", {})], None, "keep"),
        (
            [filter_call("report_incorrect_comments", {"analysis": [], "comment_ids": ["c-0"]})],
            None,
            "reject",
        ),
        (
            [
                filter_call(
                    "report_incorrect_comments",
                    {"analysis": ["No line anchor"], "comment_ids": ["c-0", "c-99", "bad"]},
                )
            ],
            None,
            "reject",
        ),
        (
            [filter_call("report_incorrect_comments", {"comment_ids": ["c-99", "bad"]})],
            None,
            "keep",
        ),
        ([filter_call("file_read", {"file_path": "a.py"})], None, "undecided"),
        ([], '["c-0", "invalid", "c-999"]', "reject"),
        ([], "```json\n[]\n```", "keep"),
    ],
)
async def test_filter_results_follow_go_protocol_without_evidence_anchor_gate(
    tmp_path, calls, text, expected
):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    requests = []
    decision = await reflect_candidate(
        finding(), frozen, runner_with_response(text, requests, tool_calls=calls)
    )
    assert decision.decision == expected and len(requests) == 1


def test_malformed_filter_arguments_fall_back_to_text_and_partial_invalid_ids_are_ignored():
    from code_review_agent.reflection.reviewer import parse_filter_result
    from code_review_agent.runtime.agentscope_adapter import AgentRun

    result = AgentRun(
        '["c-1"]',
        True,
        {},
        (),
        tool_calls=(
            {"name": "report_incorrect_comments", "arguments": "invalid JSON"},
            {"name": "unknown", "arguments": "{}"},
        ),
    )
    assert parse_filter_result(result, 2)[0] == {1}
    result = replace(
        result,
        tool_calls=(
            {
                "name": "report_incorrect_comments",
                "arguments": '{"comment_ids":["c-0","c-999","bad"]}',
            },
            {"name": "approve_all_comments", "arguments": "{}"},
        ),
    )
    assert parse_filter_result(result, 2)[0] == {0}
    assert (
        parse_filter_result(replace(result, text="", tool_calls=({"name": "bad"},)), 2)[0] is None
    )


@pytest.mark.asyncio
async def test_group_with_no_comments_or_only_refiled_comments_does_not_call_model():
    from test_location import input_with_files

    frozen = input_with_files(FileSnapshot("a.py", "safe()"), FileSnapshot("b.py", "return x"))
    requests = []
    runner = runner_with_response("not JSON", requests)
    assert await reflect_group((), frozen, runner, target_paths=("a.py",)) == []
    decision = await reflect_group(
        (replace(finding(), path="b.py"),), frozen, runner, target_paths=("a.py",)
    )
    assert decision[0].decision == "keep" and not requests


@pytest.mark.asyncio
async def test_failed_single_request_keeps_every_group_candidate(tmp_path):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    requests = []
    runner = runner_with_response(None, requests, status_code=503)
    decisions = await reflect_group(
        (finding(), replace(finding(), candidate_id="other")),
        frozen,
        runner,
        target_paths=("a.py",),
    )
    assert len(requests) == 1 and len(decisions) == 2
    assert all(item.decision == "undecided" for item in decisions)
    assert runner.records[0]["value"]["status"] == "error" and runner.usage[0].input_tokens is None
