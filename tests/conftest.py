"""测试产生的临时目录和缓存全部位于项目内。"""

from pathlib import Path

from code_review_agent.project_paths import ProjectPaths


def pytest_configure() -> None:
    ProjectPaths(Path(__file__).resolve().parents[1]).configure_environment()
