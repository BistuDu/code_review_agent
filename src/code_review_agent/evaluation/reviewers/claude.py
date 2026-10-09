"""原生 Claude Code、独立配置、工具守卫与最终 JSON；无 MCP。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import cast

from ...config import Settings
from ...contracts import JsonObject, JsonValue, UsageRecord, stable_digest
from ...inputs.snapshots import safe_relative_path
from ...project_paths import ProjectPaths
from ...sessions.serialization import json_object
from ..config import ClaudeConfig, load_claude_config
from ..dataset import PullRequest
from ..repositories import RepositoryPool, _settle
from ..task import ReviewTask

SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "side": {"type": ["string", "null"], "enum": ["left", "right", None]},
                    "start_line": {"type": ["integer", "null"]},
                    "end_line": {"type": ["integer", "null"]},
                },
                "required": ["path", "content", "side", "start_line", "end_line"],
            },
        }
    },
    "required": ["comments"],
}


def normalize_comments(value: object, identity: str, readable: tuple[str, ...]) -> list[JsonObject]:
    if (
        not isinstance(value, dict)
        or set(value) != {"comments"}
        or not isinstance(value.get("comments"), list)
    ):
        raise ValueError("Expected final comments JSON")
    result: list[JsonObject] = []
    for ordinal, row in enumerate(value["comments"]):
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "content", "side", "start_line", "end_line"}
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("content"), str)
            or not row["content"].strip()
        ):
            raise ValueError("Invalid finding fields")
        path = safe_relative_path(row["path"])
        if path not in readable or row.get("side") not in {"left", "right", None}:
            raise ValueError("Invalid finding path or side")
        start, end = row.get("start_line"), row.get("end_line")
        if (start is None) != (end is None):
            raise ValueError("Partial finding interval is invalid")
        if start is None:
            start = end = None
        elif type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise ValueError("Invalid finding line interval")
        result.append(
            {
                "candidate_id": stable_digest([identity, ordinal]),
                "path": path,
                "content": row["content"],
                "side": row.get("side"),
                "start_line": start,
                "end_line": end,
            }
        )
    return result


def parse_usage(envelope: JsonObject, duration: float) -> list[JsonObject]:
    raw = envelope.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    model_usage = envelope.get("modelUsage", envelope.get("model_usage"))
    if not raw and isinstance(model_usage, dict):
        rows = [item for item in model_usage.values() if isinstance(item, dict)]
        if rows:
            keys = (
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
            )
            totals = {
                key: sum(cast(int, item[key]) for item in rows)
                if all(type(item.get(key)) is int and cast(int, item[key]) >= 0 for item in rows)
                else None
                for key in keys
            }
            raw = dict(
                zip(
                    (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ),
                    totals.values(),
                    strict=True,
                )
            )

    def number(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    if "prompt_tokens" in raw:
        total_input, output = number("prompt_tokens"), number("completion_tokens")
        detail = raw.get("prompt_tokens_details")
        cached = detail.get("cached_tokens") if isinstance(detail, dict) else None
        cached = cached if type(cached) is int and cached >= 0 else None
    else:
        uncached, cached, created = (
            number(key)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        total_input = (
            sum(value or 0 for value in (uncached, cached, created))
            if all(value is not None for value in (uncached, cached, created))
            else None
        )
        output = number("output_tokens")
    return [
        json_object(
            asdict(
                UsageRecord("review.claude", total_input, output, cached, duration_seconds=duration)
            )
        )
    ]


async def reap(process: asyncio.subprocess.Process) -> None:
    # 即使主进程刚退出，也回收同进程组的存活子进程。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


class ClaudeReviewer:
    def __init__(
        self,
        paths: ProjectPaths,
        settings: Settings,
        timeout: float = 1800,
        config: ClaudeConfig | None = None,
    ) -> None:
        self.paths, self.settings, self.timeout = paths, settings, timeout
        self.config = config or load_claude_config(paths, settings)
        self.command = self.config.command

    def identity(self) -> JsonObject:
        identity = self.config.public_json()
        try:
            identity["cli_version"] = subprocess.check_output(
                [self.command, "--version"], text=True, timeout=10
            ).strip()
        except (OSError, subprocess.SubprocessError):
            identity["cli_version"] = None
        return identity

    async def run(
        self, task: ReviewTask, job_id: str, pool: RepositoryPool, pr: PullRequest
    ) -> JsonObject:
        if not task.payload["targets"]:
            return {
                "status": "no_files",
                "comments": [],
                "usage": [],
                "review_seconds": 0.0,
                "no_model_required": True,
            }
        self.config.require()
        version = subprocess.check_output(
            [self.command, "--version"], text=True, timeout=10
        ).strip()
        help_text = subprocess.check_output([self.command, "--help"], text=True, timeout=10)
        for flag in (
            "--setting-sources",
            "--settings",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--json-schema",
        ):
            if flag not in help_text:
                raise ValueError(f"Claude CLI missing required option: {flag}")
        directory = self.paths.directory(f".cache/claude-review/{job_id}")
        audit = directory / "guard-audit.json"
        audit.unlink(missing_ok=True)
        config = directory / "guard.json"
        config.write_text(
            json.dumps(
                {
                    "root": str(task.path),
                    "paths": task.readable_paths,
                    "versions": [task.source, task.target],
                    "version_paths": task.version_paths,
                    "max_file_bytes": self.settings.max_file_bytes,
                    "audit": str(audit),
                }
            )
        )
        guard = Path(__file__).with_name("claude_tool_guard.py")
        hook = shlex.join([sys.executable, str(guard), str(config)])
        settings: JsonObject = {
            "autoMemoryEnabled": False,
            "disableAllHooks": False,
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": hook}]}],
                "PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": hook}]}],
            },
            "permissions": {"deny": ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]},
        }
        settings_path = directory / "settings.json"
        settings_path.write_text(json.dumps(settings))
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ANTHROPIC_", "CLAUDE_", "GIT_"))
            and key != "RIPGREP_CONFIG_PATH"
        }
        model = self.config.model
        environment.update(
            {
                "ANTHROPIC_API_KEY": self.config.api_key,
                "ANTHROPIC_BASE_URL": self.config.base_url,
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model,
                "CLAUDE_CONFIG_DIR": str(self.paths.directory(directory / "config")),
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        arguments = [
            self.command,
            "--setting-sources",
            "",
            "--settings",
            str(settings_path),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--disable-slash-commands",
            "--no-chrome",
            "--model",
            model,
            "--tools",
            "Read,Grep,Glob,Bash",
            "--allowedTools",
            "Read,Grep,Glob,Bash",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "-p",
            task.prompt(),
        ]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=task.path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout = stderr = b""
        communication = asyncio.create_task(process.communicate())
        failure: str | None = None
        registered = False
        try:
            pool.register_process(pr, process.pid, job_id)
            registered = True
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), self.timeout)
        except TimeoutError:
            failure = "Claude review timeout"
        finally:
            cleanup = asyncio.create_task(reap(process))
            try:
                await _settle(cleanup)
            finally:
                if registered:
                    pool.release_process(pr)
                stdout, stderr = await _settle(communication)

        duration = time.monotonic() - started
        diagnostic: JsonObject = {
            "review_seconds": duration,
            "cli_version": version,
            "exit_code": process.returncode,
            "pid": process.pid,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
        try:
            envelope = json_object(json.loads(stdout))
            if not isinstance(envelope, dict):
                raise ValueError("Claude CLI envelope must be an object")
            diagnostic["raw"] = envelope
            diagnostic["usage"] = cast(list[JsonValue], parse_usage(envelope, duration))
            if (
                failure is not None
                or process.returncode != 0
                or envelope.get("is_error") is True
                or envelope.get("subtype") != "success"
            ):
                raise ValueError(failure or "Claude CLI did not complete successfully")
            if not audit.exists() or not json.loads(audit.read_text()).get("initialized"):
                raise ValueError("Explicit Claude tool guard did not initialize")
            payload = envelope.get("structured_output")
            if payload is None:
                text = str(envelope.get("result", "")).strip()
                match = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
                payload = json.loads(match.group(1) if match else text)
            comments = normalize_comments(payload, job_id, task.readable_paths)
            model_usage = envelope.get("modelUsage", envelope.get("model_usage"))
            if isinstance(model_usage, dict) and any(key != model for key in model_usage):
                raise ValueError("Claude provider reported a different model")
            diagnostic.update(
                json_object(
                    {
                        "status": "completed",
                        "comments": comments,
                        "usage": parse_usage(envelope, duration),
                        "guard": json_object(json.loads(audit.read_text())),
                        "raw": envelope,
                    }
                )
            )
        except (ValueError, TypeError) as error:
            diagnostic.update(
                json_object({"status": "failed", "comments": [], "error": failure or str(error)})
            )
        diagnostic.setdefault("usage", cast(list[JsonValue], parse_usage({}, duration)))
        return diagnostic
