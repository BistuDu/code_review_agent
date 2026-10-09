"""python main.py --参数；项目路径初始化必须早于模型库导入。"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from code_review_agent.project_paths import ProjectPaths

paths = ProjectPaths(Path(__file__).resolve().parent)
paths.configure_environment()
from code_review_agent.application.script_entry import main

if __name__ == "__main__":
    raise SystemExit(main(paths))
