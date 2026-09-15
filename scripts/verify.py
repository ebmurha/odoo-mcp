"""Verify a clean disposable clone without changing the working tree."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ARCHIVE_PARTS = {
    ".env",
    ".env.local",
    "AGENTS.md",
    "CLAUDE.md",
}


def _run(command: list[str], *, cwd: Path) -> None:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    completed = subprocess.run(command, cwd=cwd, check=False, env=environment)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _git_status(path: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        return archive.getnames()


def _inspect_artifacts(dist: Path) -> None:
    artifacts = sorted(
        item for item in dist.iterdir() if item.suffix == ".whl" or item.name.endswith(".tar.gz")
    )
    if len(artifacts) != 2 or {item.suffix for item in artifacts} != {".whl", ".gz"}:
        raise SystemExit("Expected exactly one wheel and one source distribution")
    for artifact in artifacts:
        for name in _archive_names(artifact):
            normalized = name.replace("\\", "/")
            parts = set(normalized.split("/"))
            if parts & FORBIDDEN_ARCHIVE_PARTS:
                raise SystemExit(f"Forbidden file in package artifact: {normalized}")


def main() -> None:
    if not (ROOT / ".git").is_dir():
        raise SystemExit("Verification requires the scaffolded Git repository")
    before = _git_status(ROOT)
    if before:
        raise SystemExit("Verification requires a clean working tree")
    with tempfile.TemporaryDirectory(
        prefix="odoo-mcp-verify-",
        dir=ROOT.parent,
    ) as temp_name:
        temp = Path(temp_name)
        clone = temp / "clone"
        _run(
            ["git", "clone", "--quiet", "--no-hardlinks", os.fspath(ROOT), os.fspath(clone)],
            cwd=temp,
        )
        _run(["uv", "sync", "--python", "3.11", "--locked", "--all-extras"], cwd=clone)
        _run(["uv", "lock", "--check"], cwd=clone)
        _run(["uv", "run", "ruff", "check", "."], cwd=clone)
        _run(["uv", "run", "ruff", "format", "--check", "."], cwd=clone)
        _run(["uv", "run", "mypy"], cwd=clone)
        _run(["uv", "run", "pytest", "-q"], cwd=clone)
        dist = temp / "dist"
        _run(["uv", "build", "--out-dir", os.fspath(dist)], cwd=clone)
        _inspect_artifacts(dist)
        _run(["uv", "lock", "--check"], cwd=clone)
        if _git_status(clone):
            raise SystemExit("Verification dirtied the disposable clone")
    if _git_status(ROOT) != before:
        raise SystemExit("Verification dirtied the source working tree")
    print("Verification passed in a clean disposable clone.")


if __name__ == "__main__":
    main()
