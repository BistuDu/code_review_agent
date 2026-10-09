"""可重跑的环境、依赖方向、工具白名单和边界证据；不会调用模型。"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from code_review_agent.project_paths import ProjectPaths  # noqa: E402

paths = ProjectPaths(PROJECT)
paths.configure_environment()
from packaging.specifiers import SpecifierSet  # noqa: E402

from code_review_agent.config import load_settings  # noqa: E402
from code_review_agent.contracts import InputMode, ReviewInput, stable_digest  # noqa: E402
from code_review_agent.reflection.tools import FILTER_TOOLS  # noqa: E402
from code_review_agent.tools.context import ContextTools  # noqa: E402
from code_review_agent.tools.findings import FindingCollector  # noqa: E402
from code_review_agent.tools.registry import ToolRegistry  # noqa: E402


def source_graph() -> tuple[dict[str, list[str]], list[list[str]]]:
    root = PROJECT / "src"
    modules: dict[str, Path] = {}
    for file in (root / "code_review_agent").rglob("*.py"):
        parts = list(file.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = file
    edges: dict[str, list[str]] = {}
    for name, file in modules.items():
        targets: set[str] = set()
        container = name.split(".") if file.name == "__init__.py" else name.split(".")[:-1]
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom):
                prefix = container[: len(container) - node.level + 1] if node.level else []
                base = ".".join(prefix + (node.module or "").split(".")).strip(".")
                if base in modules:
                    targets.add(base)
                for alias in node.names:
                    submodule = base + "." + alias.name
                    if submodule in modules:
                        targets.add(submodule)
            elif isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names if alias.name in modules)
        edges[name] = sorted(targets)
    cycles: list[list[str]] = []
    visited: set[str] = set()

    def traverse(name: str, active: list[str]) -> None:
        if name in active:
            cycles.append(active[active.index(name) :] + [name])
            return
        if name in visited:
            return
        for target in edges[name]:
            traverse(target, active + [name])
        visited.add(name)

    for name in edges:
        traverse(name, [])
    return edges, cycles


def main() -> None:
    graph, cycles = source_graph()
    assert not cycles, cycles
    assert Path(sys.prefix).resolve() == paths.writable(".venv")
    frozen = ReviewInput(str(PROJECT), InputMode.SCAN, None, None, (), (), "verification")
    context = ContextTools(frozen)
    tools = {
        stage: sorted(
            ToolRegistry(
                context,
                FindingCollector(frozen, (), "verification") if stage == "review" else None,
                stage=stage,
            ).definitions
        )
        for stage in ("review", "plan", "reflection", "relocation", "grouping")
    }
    # Reflection 是单次模型请求的结果工具，不在主循环 Toolkit 中执行。
    tools["reflection"] = sorted(str(definition["function"]["name"]) for definition in FILTER_TOOLS)
    distributions = sorted(
        importlib.metadata.distributions(), key=lambda item: str(item.metadata["Name"]).lower()
    )
    versions = {str(item.metadata["Name"]): item.version for item in distributions}
    settings = load_settings(
        paths, "config.local.json" if paths.writable("config.local.json").exists() else None
    )
    configured = {
        role: bool(
            getattr(settings, role).base_url
            and getattr(settings, role).model
            and getattr(settings, role).api_key
        )
        for role in ("reviewer", "reflection", "judge")
    }
    python311_incompatible = [
        str(item.metadata["Name"])
        for item in distributions
        if item.metadata.get("Requires-Python")
        and not SpecifierSet(str(item.metadata["Requires-Python"])).contains("3.11.0")
    ]
    report = {
        "verification_scope": "offline functionality and implementation boundaries, not effect metrics",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "venv": sys.prefix,
        "agentscope": importlib.metadata.version("agentscope"),
        "installed_versions": versions,
        "python311_metadata_incompatible": python311_incompatible,
        "python311_runtime_tested": False,
        "source_file_count": len(graph),
        "module_edges": graph,
        "cycles": cycles,
        "stage_tools": tools,
        "configured_model_roles": configured,
        "writable_runtime_root": str(PROJECT),
        "runtime_directories": [".cache", ".state", "reports"],
        "self_contained_runtime": True,
        "evaluation_reviewers": ["production project review", "native Claude Code"],
        "evaluation_checks": [
            "direct SOURCE/TARGET",
            "shared effective rules",
            "isolated Judge",
            "planned failures retained",
        ],
        "resources_digest": stable_digest(
            json.loads(
                paths.writable(
                    "src/code_review_agent/resources/resource_manifest.json"
                ).read_text()
            )
        ),
        "real_model_evaluation": "tracked separately in benchmark reports; this verifier does not call models",
        "human_review_completed": 0,
    }
    destination = paths.writable("reports/verification/project.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert not python311_incompatible, python311_incompatible
    print(
        json.dumps(
            {
                "source_file_count": len(graph),
                "cycles": cycles,
                "agentscope": versions["agentscope"],
                "python311_metadata_incompatible": python311_incompatible,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
