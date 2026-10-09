"""配对仓库 cluster bootstrap；Judge 重跑不是独立生成。"""

from __future__ import annotations

import random

from ..contracts import JsonObject
from .metrics import Counts, ratio


def paired_bootstrap(
    left_reviewer: dict[str, Counts],
    right_reviewer: dict[str, Counts],
    *,
    seed: int = 42,
    samples: int = 2000,
) -> JsonObject:
    repos = sorted(left_reviewer)
    if set(repos) != set(right_reviewer) or not repos or samples < 1:
        raise ValueError("Bootstrap requires the same nonempty repository clusters")

    def estimate(selected: list[str]) -> tuple[float, float]:
        left = right = Counts()
        for repo in selected:
            left += left_reviewer[repo]
            right += right_reviewer[repo]
        return float(str(left.metrics()["f1"])), float(str(right.metrics()["f1"]))

    initial, improved = estimate(repos)
    rng = random.Random(seed)
    differences = sorted(
        (lambda pair: pair[1] - pair[0])(estimate(rng.choices(repos, k=len(repos))))
        for _ in range(samples)
    )
    lower = differences[int(0.025 * (samples - 1))]
    upper = differences[int(0.975 * (samples - 1))]
    return {
        "seed": seed,
        "bootstrap_samples": samples,
        "repo_clusters": len(repos),
        "left_reviewer_f1": initial,
        "right_reviewer_f1": improved,
        "difference": improved - initial,
        "percentage_point_change": 100 * (improved - initial),
        "relative_change": ratio(improved - initial, initial),
        "difference_ci95": [lower, upper],
        "scope": "selected repositories only; paired cluster resampling",
    }
