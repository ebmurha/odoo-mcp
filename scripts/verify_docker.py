"""Build and qualify the release container without contacting Odoo."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "odoo-mcp:qualification"
DOCKER = ["docker", "--context", "default"]
QUALIFICATION = (
    "import os, odoo_mcp; "
    "assert odoo_mcp.__version__ == '0.3.0'; "
    "assert os.getuid() != 0; "
    "print('Odoo MCP Docker qualification passed.')"
)


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> None:
    _run([*DOCKER, "build", "--pull", "--tag", IMAGE, "."])
    _run(
        [
            *DOCKER,
            "run",
            "--rm",
            "--entrypoint",
            "/app/.venv/bin/python",
            IMAGE,
            "-c",
            QUALIFICATION,
        ]
    )


if __name__ == "__main__":
    main()
