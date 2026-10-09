"""三个薄脚本的终端边界：stdout 结果，stderr 进度与参数错误。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ..config import load_settings
from ..contracts import JsonObject
from ..output.render import exit_code, output_json, output_text
from ..project_paths import ProjectPaths
from ..sessions.replay import load_session
from .arguments import common, overrides, review_arguments
from .review_service import review
from .session_service import list_sessions, show_session


def emit(
    paths: ProjectPaths, arguments: argparse.Namespace, value: JsonObject, language: str
) -> None:
    text = (
        json.dumps(value, ensure_ascii=False, indent=2)
        if arguments.format == "json"
        else output_text(value, language)
    )
    if arguments.output:
        destination = paths.writable(arguments.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n")
    print(text)


def main(paths: ProjectPaths) -> int:
    parser = argparse.ArgumentParser(description="Frozen-input Python/AgentScope review")
    common(parser)
    review_arguments(parser)
    arguments = parser.parse_args()
    try:
        settings = load_settings(paths, arguments.config, overrides(arguments))
        print(
            "Preparing frozen input..." if settings.language == "en" else "准备冻结输入……",
            file=sys.stderr,
        )
        result = asyncio.run(
            review(
                paths,
                settings,
                arguments.repo,
                mode=arguments.mode,
                from_ref=arguments.from_ref,
                to_ref=arguments.to_ref,
                commit=arguments.commit,
                scan_path=arguments.scan_path,
                preview=arguments.preview,
                resume=arguments.resume,
            )
        )
        value = output_json(
            result,
            settings.public_json(),
            (settings.reviewer.api_key, settings.reflection.api_key, settings.judge.api_key),
        )
        emit(paths, arguments, value, settings.language)
        return exit_code(result.status)
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; inspect session checkpoint.", file=sys.stderr)
        return 130


def session(paths: ProjectPaths) -> int:
    parser = argparse.ArgumentParser(description="Inspect or resume project-local sessions")
    common(parser)
    parser.add_argument("--action", choices=("list", "show", "resume"), default="list")
    parser.add_argument("--session-id")
    parser.add_argument("--repo")
    arguments = parser.parse_args()
    try:
        settings = load_settings(paths, arguments.config, overrides(arguments))
        if arguments.action == "list":
            value = list_sessions(paths)
        elif arguments.action == "show":
            if not arguments.session_id:
                raise ValueError("--session-id required")
            value = show_session(paths, arguments.session_id)
        else:
            from pathlib import Path

            if not arguments.session_id:
                raise ValueError("--session-id required")
            view = load_session(paths, arguments.session_id)
            manifest = view.manifest
            result = asyncio.run(
                review(
                    paths,
                    settings,
                    Path(arguments.repo or str(manifest["repo"])),
                    resume=arguments.session_id,
                )
            )
            value = output_json(
                result,
                settings.public_json(),
                (settings.reviewer.api_key, settings.reflection.api_key, settings.judge.api_key),
            )
            emit(paths, arguments, value, settings.language)
            return exit_code(result.status)
        if arguments.format == "text":
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            emit(paths, arguments, value, settings.language)
        return 0
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


def benchmark(paths: ProjectPaths) -> int:
    from ..evaluation.config import load_claude_config
    from .benchmark_service import BenchmarkService

    parser = argparse.ArgumentParser(description="AACR: project review versus native Claude Code")
    common(parser)
    parser.add_argument("--claude-config", help="Optional project-local Claude evaluation JSON")
    parser.add_argument("--run-id", default="aacr-project-claude-seed42")
    parser.add_argument(
        "--stage", choices=("prepare", "review", "score", "report", "all"), required=True
    )
    parser.add_argument("--reviewer", choices=("project", "claude", "both"), default="both")
    parser.add_argument("--repo", help="Explicit owner/repository subset")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--repository-concurrency", type=int, default=1)
    parser.add_argument("--repo-count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--score-id")
    data_source = parser.add_mutually_exclusive_group()
    data_source.add_argument(
        "--dataset-dir",
        help="Read-only AACR JSON directory; default: this project's dataset directory",
    )
    data_source.add_argument("--mirror", help="Project-local offline JSON mirror; flow tests only")
    parser.add_argument("--mock-judge", action="store_true", help="Flow test only")
    arguments = parser.parse_args()
    try:
        settings = load_settings(paths, arguments.config, overrides(arguments))
        service = BenchmarkService(
            paths,
            settings,
            arguments.run_id,
            repository_concurrency=arguments.repository_concurrency,
            claude_config=load_claude_config(paths, settings, arguments.claude_config),
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
        print(f"Benchmark stage: {arguments.stage}", file=sys.stderr, flush=True)
        value: JsonObject = {}
        if arguments.stage in {"prepare", "all"}:
            print("[dataset] 读取本地数据集……", file=sys.stderr, flush=True)
            dataset = service.prepare(
                seed=arguments.seed,
                repo_count=arguments.repo_count,
                repo=arguments.repo,
                limit=arguments.limit,
                mirror=arguments.mirror,
                dataset_dir=(
                    arguments.dataset_dir or str(paths.root / "dataset")
                    if not arguments.mirror
                    else None
                ),
            )
            audit = dataset.get("audit", {})
            if isinstance(audit, dict):
                print(
                    f"[dataset] {audit.get('rows', '?')} 条评论，"
                    f"{audit.get('repos', '?')} 个仓库，{audit.get('prs', '?')} 个 PR",
                    file=sys.stderr,
                    flush=True,
                )
            selection = dataset.get("selection", {})
            if isinstance(selection, dict):
                print(
                    f"[selection] 本次评测 {selection.get('selected_repo_count', '?')} 个仓库，"
                    f"{selection.get('selected_pr_count', '?')} 个 PR",
                    file=sys.stderr,
                    flush=True,
                )
            value = asyncio.run(
                service.prepare_repositories(
                    repo=arguments.repo, limit=arguments.limit, repetitions=arguments.repetitions
                )
            )
        if arguments.stage in {"review", "all"}:
            value = asyncio.run(
                service.run_reviews(
                    reviewer=arguments.reviewer,
                    repetitions=arguments.repetitions,
                    repo=arguments.repo,
                    limit=arguments.limit,
                )
            )
        if arguments.stage in {"score", "all"}:
            value = asyncio.run(service.score_reviews(mock=arguments.mock_judge, k=arguments.k))
        if arguments.stage in {"report", "all"}:
            value = service.report_reviews(score_id=arguments.score_id)
        if arguments.format == "text":
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            emit(paths, arguments, value, settings.language)
        return 0
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; completed benchmark jobs can be reused.", file=sys.stderr)
        return 130
