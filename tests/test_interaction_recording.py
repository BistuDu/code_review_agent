import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.tool import ToolChunk, ToolResponse
from test_review_sessions import configured, scripted_factory
from test_runtime import mock_model, response, tool_call

from code_review_agent.config import ModelSettings, Settings
from code_review_agent.inputs.snapshots import prepare_input
from code_review_agent.runtime.agentscope_adapter import run_agent
from code_review_agent.runtime.recording import ToolRecordingMiddleware, unit_scope
from code_review_agent.runtime.stages import StageRunner
from code_review_agent.tools.context import ContextTools
from code_review_agent.tools.findings import FindingCollector
from code_review_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_sdk_tool_loop_records_complete_operations_without_extra_start_call(tmp_path):
    (tmp_path / "a.py").write_text("return 1 / x\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    requests = []
    runner = StageRunner(configured(), model_factory=scripted_factory(requests))
    collector = FindingCollector(frozen, ("a.py",), frozen.snapshot_id)
    with unit_scope("file-a"):
        result = await runner.run(
            "review.round1",
            "Review",
            "a.py",
            registry=ToolRegistry(ContextTools(frozen), collector),
        )
    assert result.completed and not result.events and not result.state
    assert [record["name"] for record in runner.records] == [
        "model_call",
        "tool_execution",
        "model_call",
        "tool_execution",
    ]
    model_ids = {
        record["metadata"]["operation_id"]
        for record in runner.records
        if record["name"] == "model_call"
    }
    tools = [record for record in runner.records if record["name"] == "tool_execution"]
    assert all(record["metadata"]["parent_operation_id"] in model_ids for record in tools)
    assert all(record["metadata"]["unit_id"] == "file-a" for record in runner.records)
    assert tools[0]["value"]["output"]["content"][0]["text"] == "Recorded 1 candidate comments"
    assert len(requests) == 2 and len(runner.usage) == 2


@pytest.mark.asyncio
async def test_direct_calls_and_parallel_unit_ids_are_isolated():
    runner = StageRunner(configured(), model_factory=scripted_factory([]))

    async def execute(unit):
        with unit_scope(unit):
            return await runner.run("plan", "Plan", unit, single_call=True)

    await asyncio.gather(execute("a"), execute("b"))
    assert len(runner.records) == 2
    assert {record["metadata"]["unit_id"] for record in runner.records} == {"a", "b"}
    for record in runner.records:
        assert (
            record["value"]["request"]["messages"][1]["content"][0]["text"]
            == record["metadata"]["unit_id"]
        )


@pytest.mark.asyncio
async def test_retry_is_one_logical_record_with_attempt_usage():
    requests, records = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "retry"}})
        return response({"role": "assistant", "content": "done"})

    settings = ModelSettings(
        base_url="https://example.invalid/v1", model="test", api_key="test-key", retries=1
    )
    model, usage = await mock_model(handler, settings)
    model.retry_delay, model.record_sink = 0, records.append
    try:
        await model(messages=[UserMsg(name="user", content="test")])
        assert len(records) == len(usage) == 1 and len(requests) == 2
        value = records[0]["value"]
        assert value["status"] == "success" and value["attempt_count"] == 2
        assert value["usage"]["input_tokens"] is None
        assert value["attempts"][1]["usage"]["input_tokens"] == 10
        assert "request" not in value["attempts"][1]
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_compression_is_recorded_without_model_middleware_and_no_double_logging():
    bodies, records = [], []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tools"):
            properties = body["tools"][0]["function"]["parameters"]["properties"]
            return response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        tool_call(
                            "generate_structured_output",
                            {key: "facts" for key in properties},
                            "summary",
                        ),
                    ],
                }
            )
        return response({"role": "assistant", "content": "done"})

    settings = replace(configured().reviewer, context_tokens=1200, max_output_tokens=128)
    model, usage = await mock_model(handler, settings)
    model.record_sink = records.append
    try:
        result = await run_agent(
            model,
            "Continue",
            "history " * 2000,
            stage="review",
        )
        assert result.completed
        assert len(bodies) == len(records) == len(usage)
        assert any(record["metadata"]["stage"].endswith(".compression") for record in records)
        assert all(record["name"] == "model_call" for record in records)
    finally:
        await model.client.close()


@pytest.mark.asyncio
async def test_tool_chunks_use_sdk_final_response_once():
    records = []
    middleware = ToolRecordingMiddleware(records.append, "review", "file", {"tool": "model"})

    async def next_handler(**kwargs):
        combined = ToolResponse()
        for text in ("line 1\n", "line 2\n"):
            chunk = ToolChunk(content=[TextBlock(text=text)])
            combined.append_chunk(chunk)
            yield chunk
        yield combined

    outputs = [
        chunk
        async for chunk in middleware.on_acting(
            None,
            {
                "tool_call": ToolCallBlock(
                    id="tool", name="file_read", input=json.dumps({"path": "a.py"})
                ),
            },
            next_handler,
        )
    ]
    assert len(outputs) == 3 and len(records) == 1
    assert (
        "".join(item["text"] for item in records[0]["value"]["output"]["content"])
        == "line 1\nline 2\n"
    )


@pytest.mark.asyncio
async def test_cancel_and_timeout_have_distinct_complete_records():
    async def handler(request):
        await asyncio.sleep(10)
        return response({"role": "assistant", "content": "late"})

    for timeout in (True, False):
        records = []
        model, usage = await mock_model(handler)
        model.record_sink = records.append
        try:
            task = asyncio.create_task(
                run_agent(
                    model, "review", "task", stage="review", timeout_seconds=0.01 if timeout else 30
                )
            )
            if not timeout:
                await asyncio.sleep(0.02)
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert len(records) == 1
            assert records[0]["value"]["status"] == ("timeout" if timeout else "cancelled")
        finally:
            await model.client.close()


@pytest.mark.asyncio
async def test_actual_tool_error_and_cancel_record_status(tmp_path):
    (tmp_path / "a.py").write_text("code\n")
    frozen = prepare_input(tmp_path, Settings(), mode="scan")
    records, requests = [], []

    def handler(request):
        requests.append(request)
        return (
            response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        tool_call("file_read", {"file_path": "../escape"}, "read"),
                    ],
                }
            )
            if len(requests) == 1
            else response({"role": "assistant", "content": "done"})
        )

    model, usage = await mock_model(handler)
    model.record_sink = records.append
    try:
        await run_agent(
            model,
            "read",
            "read",
            stage="plan",
            registry=ToolRegistry(ContextTools(frozen), stage="relocation"),
        )
        tools = [record for record in records if record["name"] == "tool_execution"]
        assert len(tools) == 1 and tools[0]["value"]["status"] == "error"
        assert tools[0]["value"]["arguments"] == {"file_path": "../escape"}
    finally:
        await model.client.close()

    records = []
    middleware = ToolRecordingMiddleware(records.append, "review", "file", {})

    async def cancelled(**kwargs):
        raise asyncio.CancelledError
        yield

    with pytest.raises(asyncio.CancelledError):
        async for _ in middleware.on_acting(
            None, {"tool_call": ToolCallBlock(id="c", name="read", input="{}")}, cancelled
        ):
            pass
    assert len(records) == 1 and records[0]["value"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_background_relocation_keeps_originating_unit_context(tmp_path):
    from code_review_agent.application.review_service import review
    from code_review_agent.project_paths import ProjectPaths
    from code_review_agent.sessions.replay import load_session

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "a.py").write_text("return 1 / x\n")
    result = await review(
        ProjectPaths(tmp_path),
        configured(),
        repo,
        mode="scan",
        model_factory=scripted_factory([], snippet="missing code"),
    )
    view = load_session(ProjectPaths(tmp_path), result.manifest.session_id)
    review_unit = next(
        record["metadata"]["unit_id"]
        for record in view.records
        if record["name"] == "model_call" and record["metadata"]["stage"].startswith("review")
    )
    relocations = [
        record
        for record in view.records
        if record["name"] == "model_call" and record["metadata"]["stage"] == "location.relocation"
    ]
    assert len(relocations) == 1 and relocations[0]["metadata"]["unit_id"] == review_unit
    assert result.locations[0].location.start_line == 1
