import asyncio
import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from code_review_agent.config import ModelSettings, Settings
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.runtime.agentscope_adapter import run_agent
from code_review_agent.runtime.model_factory import create_model
from code_review_agent.tools.context import ContextTools
from code_review_agent.tools.findings import FindingCollector
from code_review_agent.tools.registry import ToolRegistry


def response(message, *, usage=True):
    body = {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": message,
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return httpx.Response(200, json=body)


def tool_call(name, arguments, identity):
    return {
        "id": identity,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def mock_model(handler, settings=None):
    usage = []
    model = create_model(
        settings
        or ModelSettings(
            base_url="https://example.invalid/v1", model="test-model", api_key="test-key", retries=0
        ),
        "review",
        usage,
    )
    await model.client.close()
    model.client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return model, usage


@pytest.mark.asyncio
async def test_iteration_limit_retains_candidates_without_archived_history(tmp_path: Path):
    (tmp_path / "a.py").write_text("return x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    collector = FindingCollector(frozen, ("a.py",), frozen.snapshot_id)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call(
                        "code_comment",
                        {
                            "comments": [
                                {
                                    "path": "a.py",
                                    "existing_code": "return x",
                                    "content": "issue",
                                    "category": "bug",
                                    "severity": "high",
                                }
                            ]
                        },
                        "comment",
                    )
                ],
            }
        )

    model, usage = await mock_model(handler)
    try:
        result = await run_agent(
            model,
            "review",
            "a.py",
            stage="review",
            registry=ToolRegistry(ContextTools(frozen), collector),
            max_iterations=1,
        )
        assert not result.completed and len(calls) == 1 and collector.candidates
        assert not result.state and not result.events
        assert not hasattr(collector, "histories")
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_real_agentscope_tool_loop_and_termination(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("return 1 / x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    collector = FindingCollector(frozen, ("a.py", "b.py"), frozen.snapshot_id)
    registry = ToolRegistry(ContextTools(frozen), collector)
    requests = []

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            calls = [
                tool_call(
                    "code_comment",
                    {
                        "comments": [
                            {
                                "path": path,
                                "content": "division by zero",
                                "existing_code": "return 1 / x",
                                "category": "bug",
                                "severity": "high",
                            }
                            for path in ("a.py", "b.py")
                        ]
                    },
                    "comments",
                )
            ]
        else:
            calls = [
                tool_call(
                    "task_done", {"state": "DONE", "reviewed_paths": ["a.py", "b.py"]}, "done"
                )
            ]
        return response({"role": "assistant", "content": None, "tool_calls": calls})

    model, usage = await mock_model(handler)
    try:
        result = await run_agent(
            model, "Review files", "Review a.py and b.py", stage="review", registry=registry
        )
        assert result.completed
        assert len(collector.candidates) == 2
        assert collector.covered == {"a.py", "b.py"}
        assert not hasattr(collector, "histories")
        assert len(requests) == 2
        assert len(usage) == 2
        assert all(item.input_tokens == 10 for item in usage)
        names = {tool["function"]["name"] for tool in requests[0]["tools"]}
        assert names == set(registry.definitions)
        assert "bash" not in names
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_retry_records_failed_attempts_without_sdk_retry_multiplication() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                500, json={"error": {"message": "temporary", "type": "server_error"}}
            )
        return response({"role": "assistant", "content": "done"})

    settings = ModelSettings(
        base_url="https://example.invalid/v1", model="test", api_key="test-key", retries=1
    )
    model, usage = await mock_model(handler, settings)
    model.retry_delay = 0
    try:
        result = await run_agent(model, "test", "test", stage="plan")
        assert result.completed
        assert len(requests) == 2
        assert len(usage) == 1 and usage[0].input_tokens is None
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_compression_keeps_pinned_identity_and_accounts_for_calls() -> None:
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if (
            body.get("tools")
            and body["tools"][0]["function"]["name"] == "generate_structured_output"
        ):
            fields = body["tools"][0]["function"]["parameters"]["properties"]
            values = {field: "preserved facts" for field in fields}
            return response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call("generate_structured_output", values, "summary")],
                }
            )
        return response({"role": "assistant", "content": "done"})

    settings = ModelSettings(
        base_url="https://example.invalid/v1",
        model="test",
        api_key="test-key",
        retries=0,
        context_tokens=1200,
        max_output_tokens=128,
    )
    model, usage = await mock_model(handler, settings)
    try:
        result = await run_agent(
            model,
            "Continue",
            "history " * 2000,
            stage="review",
            pinned_context="Frozen revision: abc; files: a.py; confirmed issue: candidate-1",
        )
        assert result.completed
        assert any(item.stage.endswith(".compression") for item in usage)
        assert "Frozen revision: abc" in json.dumps(bodies[-1])
        assert "candidate-1" in json.dumps(bodies[-1])
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_single_call_does_not_silently_compress_oversized_history() -> None:
    settings = ModelSettings(
        base_url="https://example.invalid/v1",
        model="test",
        api_key="test-key",
        retries=0,
        context_tokens=1200,
        max_output_tokens=128,
    )
    requests = []

    def handler(request):
        requests.append(request)
        return response({"role": "assistant", "content": "done"})

    model, usage = await mock_model(handler, settings)
    try:
        result = await run_agent(
            model, "test", "huge input " * 2000, stage="review", allow_compression=False
        )
        assert not result.completed
        assert not requests and not usage
        assert "capacity" in result.error
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_stage_timeout_is_not_success() -> None:
    async def handler(request):
        await asyncio.sleep(0.1)
        return response({"role": "assistant", "content": "done"})

    model, usage = await mock_model(handler)
    try:
        result = await run_agent(model, "test", "test", stage="review", timeout_seconds=0.01)
        assert not result.completed
        assert len(usage) == 1 and usage[0].input_tokens is None
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_and_plain_response() -> None:
    model, usage = await mock_model(
        lambda request: response({"role": "assistant", "content": '{"groups": []}'}, usage=False)
    )
    try:
        result = await run_agent(model, "Return JSON", "Group files", stage="grouping")
        assert result.completed
        assert json.loads(result.text) == {"groups": []}
        assert usage[0].input_tokens is None
    finally:
        await model.client.close()
