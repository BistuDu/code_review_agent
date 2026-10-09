"""只读取冻结评分与运行产物，重算双评审器报告；不调用 Git 或模型。"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev
from typing import cast

from ..contracts import JsonObject, JsonValue, UsageRecord
from ..sessions.serialization import decode_usage, json_object, objects
from ..sessions.store import SessionStore
from .dataset import decode_pr
from .metrics import Counts, aggregate, cost_metrics


def _mask_unknown(value: JsonObject, known: bool) -> JsonObject:
    if not known:
        value.update({"generated": None, "precision": None, "f1": None})
    return value


def build_report(store: SessionStore, plan: JsonObject, score: JsonObject) -> JsonObject:
    prs = {
        pr.pr_url: pr
        for pr in (decode_pr(row) for row in objects(store.read("dataset.json")["prs"]))
    }
    rows = objects(score["scores"])
    identity = cast(JsonObject, score["identity"])
    jobs = {str(job["job_id"]): job for job in objects(identity["jobs"])}
    paired: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in rows:
        if row["completed"]:
            paired[(str(row["pr_url"]), int(str(row["repetition"])))].add(str(row["reviewer"]))
    systems: JsonObject = {}
    per_pr: list[JsonObject] = []
    for reviewer in ("project", "claude"):
        by_repo: dict[str, Counts] = {}
        known_repo: dict[str, bool] = {}
        repeated: dict[int, Counts] = {}
        repeated_known: dict[int, bool] = {}
        side: dict[str, list[int]] = {"left": [0, 0], "right": [0, 0]}
        common = Counts()
        usage: list[UsageRecord] = []
        durations: list[float] = []
        preparation_seconds = lock_seconds = cleanup_seconds = 0.0
        attempts_count = unknown_usage_attempts = undecided = upper_matched = 0
        generated_known = unknown_jobs = 0
        for row in (row for row in rows if row["reviewer"] == reviewer):
            job = jobs[str(row["job_id"])]
            repo, repetition = str(row["repo"]), int(str(row["repetition"]))
            known = row["generated_known"] is True
            generated_known += int(str(row["generated"])) if known else 0
            unknown_jobs += int(not known)
            count = Counts(
                int(str(row["matched"])),
                int(str(row["generated"])),
                int(str(row["references"])),
                1,
                int(row["completed"] is True),
            )
            by_repo[repo] = by_repo.get(repo, Counts()) + count
            known_repo[repo] = known_repo.get(repo, True) and known
            repeated[repetition] = repeated.get(repetition, Counts()) + count
            repeated_known[repetition] = repeated_known.get(repetition, True) and known
            undecided += int(str(row["undecided"]))
            upper_matched += int(str(row["matched_upper"]))
            matching = cast(JsonObject, row["matching"]) if row["completed"] else {}
            for reference in prs[str(row["pr_url"])].references:
                if reference.label == 1 and reference.eligibility == "valid":
                    side[reference.side.value][0] += int(reference.reference_id in matching)
                    side[reference.side.value][1] += 1
            if paired[(str(row["pr_url"]), repetition)] == {"project", "claude"}:
                common += count
            task_metadata = row.get("task_metadata")
            targets = (
                set(str(path) for path in cast(list[JsonValue], task_metadata["targets"]))
                if isinstance(task_metadata, dict)
                and isinstance(task_metadata.get("targets"), list)
                else None
            )
            if targets is None:
                project_job = next(
                    (
                        item
                        for item in jobs.values()
                        if item["pr_url"] == row["pr_url"]
                        and item["repetition"] == row["repetition"]
                        and item["reviewer"] == "project"
                    ),
                    None,
                )
                artifact = f"reviews/project/{project_job['job_id']}.json" if project_job else ""
                if artifact and (store.root / artifact).exists():
                    coverage = store.read(artifact).get("coverage")
                    if isinstance(coverage, dict):
                        targets = set(coverage)
            filtered_references = [
                {"reference_id": ref.reference_id, "path": ref.path, "side": ref.side.value}
                for ref in prs[str(row["pr_url"])].references
                if ref.label == 1
                and ref.eligibility == "valid"
                and targets is not None
                and ref.path not in targets
            ]
            per_pr.append(
                {
                    "job_id": row["job_id"],
                    "pr_url": row["pr_url"],
                    "repo": repo,
                    "reviewer": reviewer,
                    "repetition": repetition,
                    "status": job["status"],
                    "filter_scope_known": targets is not None,
                    "filtered_positive_references": cast(list[JsonValue], filtered_references),
                    "excluded_files": task_metadata.get("excluded")
                    if isinstance(task_metadata, dict)
                    else None,
                    "undecided": row["undecided"],
                    "excluded_reference_rows": row["excluded_reference_rows"],
                    **_mask_unknown(count.metrics(), known),
                }
            )
            job_seconds = 0.0
            has_seconds = False
            for attempt in objects(job.get("attempts", [])):
                attempts_count += 1
                records = objects(attempt.get("usage", []))
                if not records and attempt["status"] != "no_files":
                    # 失败/取消仍可能发生付费请求，未知用量不能按零算。
                    unknown_usage_attempts += 1
                    usage.append(UsageRecord("review.unknown", None, None))
                else:
                    usage.extend(decode_usage(item) for item in records)
                seconds = attempt.get("review_seconds")
                if isinstance(seconds, (int, float)):
                    job_seconds += float(seconds)
                    has_seconds = True
                repository = attempt.get("repository")
                if isinstance(repository, dict):
                    preparation_seconds += float(str(repository.get("preparation_seconds", 0)))
                    lock_seconds += float(str(repository.get("lock_wait_seconds", 0)))
                    cleanup_seconds += float(str(repository.get("cleanup_seconds", 0)))
            if has_seconds:
                durations.append(job_seconds)
        summary = aggregate(by_repo)
        all_known = all(known_repo.values())
        _mask_unknown(cast(JsonObject, summary["micro"]), all_known)
        cast(JsonObject, summary["micro"]).update(
            {
                "G_known": generated_known,
                "G_unknown_jobs": unknown_jobs,
                "precision_is_complete": all_known and undecided == 0,
                "M_lower": sum(count.matched for count in by_repo.values()),
                "M_upper": upper_matched,
            }
        )
        for repo, metrics in cast(dict[str, JsonObject], summary["repos"]).items():
            _mask_unknown(metrics, known_repo[repo])
        if not all_known:
            cast(JsonObject, summary["macro"]).update({"precision": None, "f1": None})
        repeat_values = {
            str(key): _mask_unknown(value.metrics(), repeated_known[key])
            for key, value in repeated.items()
        }
        f1s = [
            float(str(value["f1"])) for value in repeat_values.values() if value["f1"] is not None
        ]
        summary.update(
            json_object(
                {
                    "repetitions": repeat_values,
                    "repeat_mean_f1": mean(f1s) if len(f1s) == len(repeated) and f1s else None,
                    "repeat_std_f1": pstdev(f1s)
                    if len(f1s) == len(repeated) and len(f1s) > 1
                    else None,
                    "recall_by_side": {
                        key: {
                            "matched": value[0],
                            "references": value[1],
                            "recall": value[0] / value[1] if value[1] else None,
                        }
                        for key, value in side.items()
                    },
                    "common_success": common.metrics(),
                    "undecided": undecided,
                    "matched_upper": upper_matched,
                    "efficiency": {
                        **cost_metrics(
                            usage, durations, sum(count.matched for count in by_repo.values())
                        ),
                        "attempts": attempts_count,
                        "unknown_usage_attempts": unknown_usage_attempts,
                        "preparation_seconds": preparation_seconds,
                        "lock_wait_seconds": lock_seconds,
                        "cleanup_seconds": cleanup_seconds,
                    },
                }
            )
        )
        systems[reviewer] = summary
    complete = all(row["completed"] and row["undecided"] == 0 for row in rows)
    left = cast(JsonObject, cast(JsonObject, systems["claude"])["micro"]).get("f1")
    right = cast(JsonObject, cast(JsonObject, systems["project"])["micro"]).get("f1")
    comparable = complete and not score["flow_test_only"] and left is not None and right is not None
    report = json_object(
        {
            "schema_version": 2,
            "score_id": score["score_id"],
            "measurement_status": "complete" if comparable else "incomplete",
            "flow_test_only": score["flow_test_only"],
            "scope": "explicit subset"
            if plan.get("repo") or plan.get("limit")
            else "frozen repository selection",
            "identity": identity,
            "systems": systems,
            "per_pr": per_pr,
            "project_minus_claude_f1_percentage_points": 100
            * (float(str(right)) - float(str(left)))
            if comparable
            else None,
            "judge_efficiency": cost_metrics(
                [decode_usage(row) for row in objects(score["judge_usage"])], [], 0
            ),
            "metric_definition": "M: strict path/side/interval + anonymous semantic maximum matching; G: all final comments including duplicates and unknown positions; N: valid label=1 rows; failed jobs M=0, N retained, G unknown. Precision is reference coverage precision, not actual defect correctness.",
        }
    )
    store.write(f"reports/{score['score_id']}.json", report)
    directory = store.paths.directory(f"reports/benchmarks/{store.session_id}")
    lines = [
        "# 本项目与 Claude Code 评测",
        "",
        f"状态：{report['measurement_status']}；流程模拟：{score['flow_test_only']}",
        "",
        "| Reviewer | M | G | N | Precision | Recall | F1 | 完成率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in systems.items():
        micro = cast(JsonObject, cast(JsonObject, value)["micro"])
        numbers: list[JsonValue] = [
            micro[key]
            for key in (
                "matched",
                "generated",
                "references",
                "precision",
                "recall",
                "f1",
                "completion_rate",
            )
        ]
        lines.append(
            "| "
            + name
            + " | "
            + " | ".join("未知" if item is None else str(item) for item in numbers)
            + " |"
        )
    lines += [
        "",
        f"Project − Claude F1 百分点：{report['project_minus_claude_f1_percentage_points']}",
        "",
        str(report["metric_definition"]),
        "",
        "准备/锁等待/清理时间单独统计；Judge 与评审用量分开；未匹配评论不等于真实误报。",
    ]
    (directory / f"{score['score_id']}.md").write_text("\n".join(lines) + "\n")
    return report
