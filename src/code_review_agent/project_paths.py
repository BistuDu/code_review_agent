"""所有可写路径在创建前解析，避免缓存或符号链接逃逸项目目录。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve(strict=True))

    def writable(self, path: str | Path) -> Path:
        requested = Path(path)
        resolved = (requested if requested.is_absolute() else self.root / requested).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Write path outside project: {path}")
        return resolved

    def directory(self, path: str | Path) -> Path:
        resolved = self.writable(path)
        resolved.mkdir(parents=True, exist_ok=True)
        return self.writable(resolved)

    def configure_environment(self) -> None:
        """先定向 SDK 缓存；仅创建实际使用的临时目录，其余由库按需创建。"""
        cache = self.directory(".cache")
        temporary = self.directory(".cache/tmp")
        destinations = {
            "HF_HOME": cache / "huggingface",
            "HF_DATASETS_CACHE": cache / "huggingface/datasets",
            "HUGGINGFACE_HUB_CACHE": cache / "huggingface/hub",
            "XDG_CACHE_HOME": cache / "xdg",
            "MPLCONFIGDIR": cache / "matplotlib",
            "TMPDIR": temporary,
            "PYTHONPYCACHEPREFIX": cache / "pycache",
        }
        for key, value in destinations.items():
            os.environ[key] = str(self.writable(value))
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        tempfile.tempdir = str(temporary)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
