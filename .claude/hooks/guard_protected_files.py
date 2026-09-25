#!/usr/bin/env python3
"""PreToolUse hook: tolak edit ke file yang dilindungi.

- .env dan varian .env.* (kecuali .env.example): berisi secret.
- Migrasi Alembic yang sudah ada di commit HEAD: riwayat migrasi tidak boleh diubah,
  buat migrasi baru. Migrasi yang belum di-commit masih boleh diedit.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path("backend/alembic/versions")


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def _is_env_file(path: Path) -> bool:
    name = path.name
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def _committed_in_head(project_dir: Path, rel_path: Path) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel_path.as_posix()}"],
        cwd=project_dir,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


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

    if _is_env_file(path):
        _deny(
            f"{path.name} berisi secret dan tidak boleh diedit Claude. "
            "Ubah .env.example kalau perlu menambah variabel, lalu minta user mengisi .env."
        )

    try:
        rel_path = path.relative_to(project_dir)
    except ValueError:
        return

    if (
        rel_path.parent == MIGRATIONS_DIR
        and rel_path.suffix == ".py"
        and _committed_in_head(project_dir, rel_path)
    ):
        _deny(
            f"{rel_path} sudah ter-commit. Migrasi lama tidak boleh diubah. "
            "Buat migrasi baru: cd backend && uv run alembic revision --autogenerate -m '...'"
        )


if __name__ == "__main__":
    main()
