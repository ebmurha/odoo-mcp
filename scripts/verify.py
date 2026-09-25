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
REQUIRED_SDIST_SUFFIXES = {
    "/assets/odoo-mcp-logo.png",
    "/CHANGELOG.md",
    "/CONTRIBUTING.md",
    "/Dockerfile",
    "/.env.shared.example",
    "/SECURITY.md",
    "/SUPPORT.md",
    "/deploy/odoo-mcp.service",
    "/deploy/reverse-proxy/nginx.conf",
    "/deploy/shared-compose.yml",
    "/docker-compose.yml",
    "/docs/deployment.md",
    "/docs/operations.md",
    "/scripts/verify_docker.py",
    "/scripts/verify_live_invoicing_odoo19.py",
    "/scripts/verify_shared.py",
    "/server.json",
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


def _inspect_artifacts(dist: Path) -> Path:
    artifacts = sorted(
        item for item in dist.iterdir() if item.suffix == ".whl" or item.name.endswith(".tar.gz")
    )
    if len(artifacts) != 2 or {item.suffix for item in artifacts} != {".whl", ".gz"}:
        raise SystemExit("Expected exactly one wheel and one source distribution")
    for artifact in artifacts:
        names = _archive_names(artifact)
        for name in names:
            normalized = name.replace("\\", "/")
            parts = set(normalized.split("/"))
            if parts & FORBIDDEN_ARCHIVE_PARTS:
                raise SystemExit(f"Forbidden file in package artifact: {normalized}")
        if artifact.name.endswith(".tar.gz"):
            normalized_names = {name.replace("\\", "/") for name in names}
            missing = {
                suffix
                for suffix in REQUIRED_SDIST_SUFFIXES
                if not any(name.endswith(suffix) for name in normalized_names)
            }
            if missing:
                raise SystemExit("Source distribution is missing required release assets")
        elif "odoo_mcp/app/operations.py" not in names:
            raise SystemExit("Wheel is missing the operations command")
    return next(artifact for artifact in artifacts if artifact.suffix == ".whl")


def _python_in(venv: Path) -> Path:
    windows = venv / "Scripts" / "python.exe"
    return windows if windows.exists() else venv / "bin" / "python"


def _verify_wheel_install(wheel: Path, *, temp: Path) -> None:
    venv = temp / "installed"
    _run(["uv", "venv", "--python", "3.11", os.fspath(venv)], cwd=temp)
    python = _python_in(venv)
    _run(
        ["uv", "pip", "install", "--python", os.fspath(python), os.fspath(wheel)],
        cwd=temp,
    )
    _run(
        [
            os.fspath(python),
            "-c",
            "from odoo_mcp.app.operations import main; main(['--help'])",
        ],
        cwd=temp,
    )
    _run(
        [
            os.fspath(python),
            "-c",
            (
                "import odoo_mcp; "
                "from odoo_mcp.app.main import main; "
                "assert odoo_mcp.__version__ == '0.1.0'; "
                "main(['--help'])"
            ),
        ],
        cwd=temp,
    )


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
        wheel = _inspect_artifacts(dist)
        _verify_wheel_install(wheel, temp=temp)
        _run(["uv", "lock", "--check"], cwd=clone)
        if _git_status(clone):
            raise SystemExit("Verification dirtied the disposable clone")
    if _git_status(ROOT) != before:
        raise SystemExit("Verification dirtied the source working tree")
    print("Verification passed in a clean disposable clone.")


if __name__ == "__main__":
    main()
