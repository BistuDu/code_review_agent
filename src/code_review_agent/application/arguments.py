"""脚本参数和配置覆盖；只支持普通 Python 执行方式。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..contracts import JsonObject


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", help="Project-local output path")
    parser.add_argument("--language", choices=("zh", "en"))
    parser.add_argument("--effort", choices=("low", "medium", "high"))
    parser.add_argument("--protocol", choices=("chat_completions",))
    parser.add_argument(
        "--timeout",
        dest="group_timeout_minutes",
        type=float,
        help="Per-group minutes multiplied by review rounds; scan uses one round (default: 15, 0: unlimited)",
    )
    parser.add_argument(
        "--review-timeout",
        dest="review_timeout_seconds",
        type=float,
        help="Optional overall review limit in seconds; benchmark defaults to 1800, daily review has no overall limit",
    )
    parser.add_argument("--background")
    parser.add_argument("--rule-file")
    parser.add_argument("--global-rule-file")
    parser.add_argument("--include", action="append")
    parser.add_argument("--exclude", action="append")
    for name in (
        "max-concurrency",
        "max-tool-iterations",
        "max-file-bytes",
        "tool-result-chars",
        "batch-size",
        "plan-line-threshold",
        "group-min-files",
        "group-max-files",
        "dedup-min-comments",
    ):
        parser.add_argument("--" + name, type=int)
    parser.add_argument("--batch-strategy", choices=("none", "by-language", "by-directory"))
    for stage in ("plan", "dedup", "summary"):
        parser.add_argument(
            "--no-" + stage, dest="scan_" + stage, action="store_false", default=None
        )
    for role in ("reviewer", "reflection", "judge"):
        for name in ("base-url", "model"):
            parser.add_argument(f"--{role}-{name}")
        for name in ("context-tokens", "max-output-tokens", "retries"):
            parser.add_argument(f"--{role}-{name}", type=int)
        for name in ("timeout-seconds", "temperature"):
            parser.add_argument(f"--{role}-{name}", type=float)


def overrides(arguments: argparse.Namespace) -> JsonObject:
    result: JsonObject = {}
    allowed = {
        "language",
        "effort",
        "protocol",
        "background",
        "rule_file",
        "global_rule_file",
        "include",
        "exclude",
        "max_concurrency",
        "max_tool_iterations",
        "review_timeout_seconds",
        "group_timeout_minutes",
        "max_file_bytes",
        "tool_result_chars",
        "batch_size",
        "plan_line_threshold",
        "group_min_files",
        "group_max_files",
        "batch_strategy",
        "scan_plan",
        "scan_dedup",
        "scan_summary",
        "dedup_min_comments",
    }
    for key, value in vars(arguments).items():
        if value is None:
            continue
        if key in allowed:
            result[key] = value
        for role in ("reviewer", "reflection", "judge"):
            if key.startswith(role + "_"):
                model = result.setdefault(role, {})
                if isinstance(model, dict):
                    model[key[len(role) + 1 :]] = value
    return result


def review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("review", "scan"), default="review")
    parser.add_argument("--from", dest="from_ref")
    parser.add_argument("--to", dest="to_ref")
    parser.add_argument("--commit")
    parser.add_argument("--scan-path")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--resume")
