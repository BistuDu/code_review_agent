"""CLI 协议、受控工具及进程生命周期；不调用真实服务。"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from test_review_sessions import configured

from code_review_agent.evaluation.config import ClaudeConfig
from code_review_agent.evaluation.reviewers.claude import (
    ClaudeReviewer,
    normalize_comments,
    parse_usage,
)
from code_review_agent.evaluation.reviewers.claude_tool_guard import permitted
from code_review_agent.evaluation.task import build_task

pytest_plugins = ["test_repository_checkout"]


def config(command="claude"):
    return ClaudeConfig("test", "https://example.invalid/anthropic", "secret-key", command)


def finding(**changes):
    return {
        "path": "app.py",
        "side": "right",
        "start_line": 1,
        "end_line": 1,
        "content": "issue",
        **changes,
    }


def test_json_keeps_duplicates_and_unknown_coordinates():
    rows = normalize_comments(
        {"comments": [finding(), finding(), finding(side=None, start_line=None, end_line=None)]},
        "job",
        ("app.py",),
    )
    assert len({row["candidate_id"] for row in rows}) == 3
    assert rows[-1]["start_line"] is None
    assert normalize_comments({"comments": []}, "job", ()) == []
    for value in [
        "text",
        {"comments": [], "extra": 1},
        {"comments": [finding(path="../answer.json")]},
        {"comments": [finding(side="bad")]},
        {"comments": [finding(start_line=True)]},
        {"comments": [finding(end_line=0)]},
        {"comments": [finding(end_line=None)]},
        {"comments": [finding(extra=1)]},
    ]:
        with pytest.raises(ValueError):
            normalize_comments(value, "job", ("app.py",))


def test_cache_usage_known_unknown_and_model_usage():
    anthropic = parse_usage(
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            }
        },
        1,
    )[0]
    assert anthropic["input_tokens"] == 170
    openai = parse_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 30},
            }
        },
        1,
    )[0]
    assert openai["input_tokens"] == 100 and openai["cached_tokens"] == 30
    unknown = parse_usage({"usage": {"input_tokens": 100, "output_tokens": 20}}, 1)[0]
    assert unknown["input_tokens"] is None
    model = parse_usage(
        {
            "model_usage": {
                "test": {
                    "inputTokens": 2,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 4,
                    "cacheCreationInputTokens": 5,
                }
            }
        },
        1,
    )[0]
    assert model["input_tokens"] == 11


def test_tool_guard_boundaries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("safe\n")
    secret = tmp_path / "answers.json"
    secret.write_text("REFERENCE_SENTINEL")
    (root / "escape.py").symlink_to(secret)
    (root / "binary").write_bytes(b"x\x00y")
    paths = {"app.py", "escape.py", "binary"}
    versions = {"a" * 40, "b" * 40}
    assert permitted(root, paths, "Read", {"file_path": "app.py"}, versions)
    assert permitted(root, paths, "Grep", {"pattern": "safe"}, versions)
    assert permitted(root, paths, "Glob", {"pattern": "*.py"}, versions)
    for tool, arguments in [
        ("Read", {"file_path": str(secret)}),
        ("Read", {"file_path": "escape.py"}),
        ("Read", {"file_path": "binary"}),
        ("Read", {"file_path": "../answers.json"}),
        ("Read", {"file_path": ".git/config"}),
        ("Glob", {"pattern": "/outside/*"}),
        ("Grep", {"glob": "../*", "pattern": "x"}),
        ("Bash", {"command": "git diff a b; touch x"}),
        ("Bash", {"command": "cat app.py"}),
        ("Bash", {"command": "rg -L x"}),
        ("Bash", {"command": "git reset --hard"}),
        ("Write", {"file_path": "app.py"}),
    ]:
        assert not permitted(root, paths, tool, arguments, versions)
    assert permitted(
        root, paths, "Bash", {"command": "git diff " + " ".join(sorted(versions))}, versions
    )
    assert permitted(root, paths, "Bash", {"command": f"git show {'a' * 40}:app.py"}, versions)
    assert not permitted(
        root, paths, "Bash", {"command": f"git show {'a' * 40}:answers.json"}, versions
    )


def fake_cli(root: Path, *, result=None, exit_code=0, guard=True, wait=False):
    command = root / "fake-claude"
    capture = root / "capture.json"
    script = f"""#!{sys.executable}
import json, os, sys, time, subprocess, signal
from pathlib import Path
if '--version' in sys.argv:
 print('test CLI 1'); raise SystemExit
if '--help' in sys.argv:
 print('--setting-sources --settings --strict-mcp-config --no-session-persistence --disable-slash-commands --json-schema'); raise SystemExit
args = sys.argv[1:]
Path({str(capture)!r}).write_text(json.dumps({{'args': args, 'cwd': os.getcwd(), 'model': os.environ.get('ANTHROPIC_MODEL'), 'token_present': 'ANTHROPIC_AUTH_TOKEN' in os.environ, 'config': os.environ.get('CLAUDE_CONFIG_DIR'), 'mds': os.environ.get('CLAUDE_CODE_DISABLE_CLAUDE_MDS')}}))
settings = json.loads(Path(args[args.index('--settings') + 1]).read_text())
if {guard!r}:
 hook = settings['hooks']['SessionStart'][0]['hooks'][0]['command']
 subprocess.run(hook, shell=True, input=json.dumps({{'hook_event_name': 'SessionStart', 'cwd': os.getcwd()}}), text=True, capture_output=True, check=True)
if {wait!r}:
 child = subprocess.Popen(["sleep", "30"])
 Path({str(root / "child-pid")!r}).write_text(str(child.pid))
 signal.signal(signal.SIGTERM, lambda *_: (child.wait(), sys.exit(143)))
 Path({str(root / "pid")!r}).write_text(str(os.getpid()))
 time.sleep(30)
print({json.dumps(result or {"subtype": "success", "is_error": False, "structured_output": {"comments": []}, "modelUsage": {"test": {}}, "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})!r})
raise SystemExit({exit_code})
"""
    command.write_text(script)
    command.chmod(0o700)
    return str(command), capture


async def test_cli_arguments_environment_and_output(fixture, monkeypatch):
    pool, pr, _ = fixture
    pool.prepare(pr)
    command, capture = fake_cli(pool.paths.root)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited-secret")
    async with pool.review_checkout(pr) as prepared:
        task = build_task(pool.paths, configured(), prepared.path, pr.base_sha, pr.head_sha)
        value = await ClaudeReviewer(pool.paths, configured(), config=config(command)).run(
            task, "job", pool, pr
        )
    assert value["status"] == "completed" and value["comments"] == []
    observed = json.loads(capture.read_text())
    assert "--bare" not in observed["args"] and not observed["token_present"]
    assert observed["mds"] == "1" and observed["cwd"] == str(prepared.path)
    assert "secret-key" not in json.dumps(observed)
    assert not pool._sidecar(pr, ".lease.json").exists()


@pytest.mark.parametrize(
    "result,code,guard",
    [
        ({"subtype": "success", "is_error": True, "structured_output": {"comments": []}}, 0, True),
        ({"subtype": "success", "is_error": False, "result": "prose"}, 0, True),
        ({"subtype": "success", "is_error": False, "structured_output": {"comments": []}}, 1, True),
        (
            {"subtype": "success", "is_error": False, "structured_output": {"comments": []}},
            0,
            False,
        ),
        (
            {
                "subtype": "success",
                "is_error": False,
                "structured_output": {"comments": []},
                "modelUsage": {"other": {}},
            },
            0,
            True,
        ),
    ],
)
async def test_cli_failure_is_never_success(fixture, result, code, guard):
    pool, pr, _ = fixture
    command, _ = fake_cli(pool.paths.root, result=result, exit_code=code, guard=guard)
    async with pool.review_checkout(pr) as prepared:
        task = build_task(pool.paths, configured(), prepared.path, pr.base_sha, pr.head_sha)
        value = await ClaudeReviewer(pool.paths, configured(), config=config(command)).run(
            task, "job", pool, pr
        )
    assert value["status"] == "failed" and value["comments"] == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_cancel_reap_before_unlock(fixture, cancel):
    pool, pr, _ = fixture
    command, _ = fake_cli(pool.paths.root, wait=True)
    cli = ClaudeReviewer(
        pool.paths, configured(), timeout=0.2 if not cancel else 30, config=config(command)
    )
    started = asyncio.Event()

    async def run():
        async with pool.review_checkout(pr) as prepared:
            task = build_task(pool.paths, configured(), prepared.path, pr.base_sha, pr.head_sha)
            started.set()
            return await cli.run(task, "job", pool, pr)

    running = asyncio.create_task(run())
    await started.wait()
    for _ in range(100):
        if (pool.paths.root / "pid").exists():
            break
        await asyncio.sleep(0.01)
    assert (pool.paths.root / "pid").exists()
    pid = int((pool.paths.root / "pid").read_text())
    if cancel:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        value = await running
        assert value["status"] == "failed" and "timeout" in value["error"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    child_pid = int((pool.paths.root / "child-pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    async with pool.review_checkout(pr):
        pass
