from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_and_tooling_versions_are_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["dependencies"]
    development = project["dependency-groups"]["dev"]

    assert "mcp==2.2.0" in requirements
    assert all("==" in requirement for requirement in requirements)
    assert all("==" in requirement for requirement in development)


def test_environment_files_and_build_outputs_are_ignored() -> None:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns
    assert "!.env.shared.example" in patterns
    assert ".venv/" in patterns
    assert "dist/" in patterns


def test_public_files_do_not_contain_private_control_material() -> None:
    candidates = [
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / ".env.example",
        ROOT / ".env.shared.example",
        ROOT / "config" / "config.example.yaml",
        ROOT / "SECURITY.md",
        ROOT / "SUPPORT.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CHANGELOG.md",
        ROOT / "docs" / "deployment.md",
        ROOT / "docs" / "demo.md",
        ROOT / "docs" / "operations.md",
        ROOT / "Dockerfile",
        ROOT / "docker-compose.yml",
        ROOT / "deploy" / "odoo-mcp.service",
        ROOT / "deploy" / "shared-compose.yml",
        ROOT / "deploy" / "reverse-proxy" / "nginx.conf",
        ROOT / "server.json",
        *sorted((ROOT / "src").rglob("*.py")),
        *sorted((ROOT / "scripts").rglob("*.py")),
        *sorted((ROOT / "tests").rglob("*.py")),
        ROOT / ".github" / "workflows" / "ci.yml",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in candidates)

    private_factory_name = "mcp" + "-factory"
    private_plan_name = "implementation" + "-plan"
    private_drive_path = "D:" + "\\Developer"
    private_step_name = "step " + "1.1"
    assert private_factory_name not in combined
    assert private_plan_name not in combined
    assert private_drive_path not in combined
    assert private_step_name not in combined.casefold()
