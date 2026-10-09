"""稳定扫描分批；每个入选文件恰好属于一个批次。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from ..contracts import ReviewUnit, stable_digest


def scan_units(paths: tuple[str, ...], strategy: str, batch_size: int) -> tuple[ReviewUnit, ...]:
    if batch_size < 1 or strategy not in {"none", "by-language", "by-directory"}:
        raise ValueError("Invalid scan batching configuration")
    groups: dict[str, list[str]] = defaultdict(list)
    for path in sorted(paths):
        if strategy == "none":
            key = path
        elif strategy == "by-language":
            key = PurePosixPath(path).suffix.lower() or "<no-ext>"
        else:
            key = path.split("/", 1)[0] if "/" in path else "<root>"
        groups[key].append(path)
    units: list[ReviewUnit] = []
    for key in sorted(groups):
        for index in range(0, len(groups[key]), batch_size):
            batch = tuple(groups[key][index : index + batch_size])
            units.append(ReviewUnit(stable_digest(list(batch)), batch, strategy))
    return tuple(units)
