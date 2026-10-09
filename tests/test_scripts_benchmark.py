import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_runtime import tool_call

from code_review_agent.project_paths import project_root


def execute(script, *arguments, environment=None):
    root = project_root()
    return subprocess.run(
        [str(root / ".venv/bin/python"), "-I", str(root / script), *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        env=environment or os.environ,
        timeout=30,
    )


def test_scripts_preview_language_protocol_budget_and_output_boundary(tmp_path: Path):
    (tmp_path / "a.py").write_text("return x\n")
    result = execute(
        "main.py", "--mode", "scan", "--repo", str(tmp_path), "--preview", "--format", "json"
    )
    value = json.loads(result.stdout)
    assert result.returncode == 0 and value["status"] == "preview"
    assert "准备" in result.stderr and value["coverage"] == {"a.py": "pending"}
    english = execute(
        "main.py", "--mode", "scan", "--repo", str(tmp_path), "--preview", "--language", "en"
    )
    assert "Review status: preview" in english.stdout and "Preparing" in english.stderr
    for arguments in (
        ("--protocol", "responses"),
        ("--token-budget", "100"),
        ("--format", "sarif"),
    ):
        invalid = execute("main.py", *arguments)
        assert invalid.returncode == 2 and not invalid.stdout
    invalid = execute(
        "main.py",
        "--mode",
        "scan",
        "--repo",
        str(tmp_path),
        "--preview",
        "--output",
        "/outside/report.json",
    )
    assert invalid.returncode == 2 and "outside project" in invalid.stderr
    listing = execute("session.py", "--action", "list", "--format", "json")
    assert listing.returncode == 0 and json.loads(listing.stdout)["schema_version"] == 2


def test_real_script_keeps_json_stdout_pure_and_passes_english_prompt(tmp_path: Path):
    (tmp_path / "a.py").write_text("return x\n")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            tools = {tool["function"]["name"] for tool in body.get("tools", [])}
            assert "English" in json.dumps(body["messages"][0]["content"])
            if "task_done" in tools:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        tool_call(
                            "task_done", {"state": "DONE", "reviewed_paths": ["a.py"]}, "done"
                        )
                    ],
                }
                reason = "tool_calls"
            else:
                message = {
                    "role": "assistant",
                    "content": "[]",
                }
                reason = "stop"
            data = json.dumps(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "finish_reason": reason, "message": message}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = {
            **os.environ,
            "CODE_REVIEW_REVIEWER_API_KEY": "test-key",
            "CODE_REVIEW_REVIEWER_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "CODE_REVIEW_REVIEWER_MODEL": "test",
        }
        result = execute(
            "main.py",
            "--mode",
            "scan",
            "--repo",
            str(tmp_path),
            "--language",
            "en",
            "--effort",
            "low",
            "--format",
            "json",
            environment=environment,
        )
        value = json.loads(result.stdout)
        assert result.returncode == 0 and value["status"] == "completed" and value["comments"] == []
        assert requests and "test-key" not in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
