#!/usr/bin/env python3
"""PostToolUse hook: format file yang baru diedit Claude.

- .py → ruff format (versi sama dengan .pre-commit-config.yaml)
- .ts/.tsx di frontend/ → prettier dari frontend/node_modules

Kalau formatter gagal (misal syntax error), pesannya dikirim ke Claude lewat exit code 2.
Kalau formatter belum ter-install, hook dilewati tanpa error.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RUFF_VERSION = "0.16.9"


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"{' '.join(cmd[:3])} gagal:\n{result.stderr or result.stdout}", file=sys.stderr)
        sys.exit(2)


def _project_dir() -> Path:
    # CLAUDE_PROJECT_DIR selalu root repo, walaupun cwd session sedang di subfolder.
    root = os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]
    return Path(root).resolve()


def main() -> None:
    payload = json.load(sys.stdin)
    raw_path = (payload.get("tool_input") or {}).get("file_path")
    if not raw_path:
        return

    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or ".") / path
    path = path.resolve()
    project_dir = _project_dir()
    if not path.is_file():
        return

    try:
        rel_path = path.relative_to(project_dir)
    except ValueError:
        return  # file di luar project, jangan disentuh

    if path.suffix == ".py":
        if shutil.which("uvx") is None:
            return
        _run(["uvx", f"ruff@{RUFF_VERSION}", "format", str(path)], project_dir)
        return

    if path.suffix in {".ts", ".tsx"} and rel_path.parts[0] == "frontend":
        frontend = project_dir / "frontend"
        prettier = frontend / "node_modules" / ".bin" / "prettier"
        if not prettier.exists():
            return
        _run([str(prettier), "--write", "--log-level=warn", str(path)], frontend)


if __name__ == "__main__":
    main()
