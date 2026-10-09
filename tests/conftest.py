"""测试产生的临时目录和缓存全部位于项目内。"""

from pathlib import Path

import pytest

from code_review_agent.project_paths import ProjectPaths


def pytest_configure() -> None:
    ProjectPaths(Path(__file__).resolve().parents[1]).configure_environment()


@pytest.fixture(autouse=True)
def isolate_test_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    # 临时目录在 .cache 内，不能把未初始化的测试目录识别为项目自身的 Git 仓库。
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(Path(__file__).resolve().parents[1]))
