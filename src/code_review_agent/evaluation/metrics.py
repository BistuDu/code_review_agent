"""可重算计数与显式分母；未匹配参考不能用于宣称实际误报。"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from statistics import median

from ..contracts import JsonObject, UsageRecord


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class Counts:
    matched: int = 0
    generated: int = 0
    references: int = 0
    planned_prs: int = 0
    completed_prs: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(
            self.matched + other.matched,
            self.generated + other.generated,
            self.references + other.references,
            self.planned_prs + other.planned_prs,
            self.completed_prs + other.completed_prs,
        )

    def metrics(self) -> JsonObject:
        return {
            **asdict(self),
            "precision": ratio(self.matched, self.generated),
            "recall": ratio(self.matched, self.references),
            "f1": 2 * self.matched / (self.generated + self.references)
            if self.generated + self.references
            else 0.0,
            "completion_rate": ratio(self.completed_prs, self.planned_prs),
        }


def aggregate(by_repo: dict[str, Counts]) -> JsonObject:
    total = Counts()
    for count in by_repo.values():
        total += count
    metrics = [count.metrics() for count in by_repo.values()]
    macro: JsonObject = {}
    for name in ("precision", "recall", "f1", "completion_rate"):
        known = [float(str(value[name])) for value in metrics if value[name] is not None]
        macro[name] = sum(known) / len(known) if known else None
        macro[name + "_repo_count"] = len(known)
    return {
        "micro": total.metrics(),
        "macro": macro,
        "repos": {repo: count.metrics() for repo, count in by_repo.items()},
    }


def cost_metrics(usage: list[UsageRecord], pr_durations: list[float], hits: int) -> JsonObject:
    stages: dict[str, list[UsageRecord]] = defaultdict(list)
    for record in usage:
        stages[record.stage].append(record)
    summary: JsonObject = {}
    for name, records in stages.items():
        unknown = sum(item.input_tokens is None or item.output_tokens is None for item in records)
        input_known = sum(item.input_tokens or 0 for item in records)
        output_known = sum(item.output_tokens or 0 for item in records)
        summary[name] = {
            "calls": len(records),
            "count_unit": "CLI aggregate" if name == "review.claude" else "model request record",
            "unknown_usage_calls": unknown,
            "usage_complete": not unknown,
            "input_tokens": None if unknown else input_known,
            "output_tokens": None if unknown else output_known,
            "known_token_subtotal": input_known + output_known,
            "cached_input_tokens_known": sum(item.cached_tokens or 0 for item in records),
            "request_seconds": sum(item.duration_seconds for item in records),
        }
    review_records = [item for item in usage if not item.stage.startswith(("judge", "prepare"))]
    complete = all(
        item.input_tokens is not None and item.output_tokens is not None for item in review_records
    )
    total = sum((item.input_tokens or 0) + (item.output_tokens or 0) for item in review_records)
    ordered = sorted(pr_durations)
    return {
        "stages": summary,
        "review_usage_complete": complete,
        "review_tokens": total if complete else None,
        "tokens_per_hit": ratio(total, hits) if complete else None,
        "pr_median_seconds": median(ordered) if ordered else None,
        "pr_p95_seconds": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None,
        "cost_scope": "input + output; cached input already included; Judge and preparation separate",
    }
