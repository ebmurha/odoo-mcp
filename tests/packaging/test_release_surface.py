from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_metadata_is_consistent_and_installable() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    package = registry["packages"][0]

    assert project["project"]["version"] == "0.2.0"
    assert project["project"]["name"] == "odoo-erp-mcp"
    assert registry["name"] == "io.github.ebmurha/odoo-mcp"
    assert registry["version"] == project["project"]["version"]
    assert "remotes" not in registry
    assert registry["icons"] == [
        {
            "src": (
                "https://raw.githubusercontent.com/ebmurha/odoo-mcp/main/assets/odoo-mcp-logo.png"
            ),
            "mimeType": "image/png",
            "sizes": ["256x256"],
        }
    ]
    assert (ROOT / "assets" / "odoo-mcp-logo.png").is_file()
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "odoo-erp-mcp"
    assert package["identifier"] == project["project"]["name"]
    assert package["version"] == project["project"]["version"]
    assert package["transport"] == {"type": "stdio"}
    assert project["project"]["scripts"] == {
        "odoo-mcp": "odoo_mcp.app.main:main",
        "odoo-mcp-admin": "odoo_mcp.app.operations:main",
        "odoo-erp-mcp": "odoo_mcp.app.main:main",
    }
    assert (
        project["project"]["scripts"][package["identifier"]]
        == (project["project"]["scripts"]["odoo-mcp"])
    )


def test_release_documentation_and_deployment_templates_are_present() -> None:
    required = (
        "SECURITY.md",
        "SUPPORT.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "docs/deployment.md",
        "docs/demo.md",
        "docs/operations.md",
        "Dockerfile",
        "docker-compose.yml",
        ".dockerignore",
        "deploy/odoo-mcp.service",
        "deploy/shared-compose.yml",
        "deploy/reverse-proxy/nginx.conf",
        "scripts/verify_docker.py",
        "scripts/verify_live_invoicing_odoo19.py",
        "scripts/verify_shared.py",
    )
    assert all((ROOT / path).is_file() for path in required)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    docker_verifier = (ROOT / "scripts/verify_docker.py").read_text(encoding="utf-8")
    assert "mcp-name: io.github.ebmurha/odoo-mcp" in readme
    assert "pipx install odoo-erp-mcp" in readme
    assert "`odoo-erp-mcp` as a compatibility launcher" in readme
    assert "USER odoo-mcp" in dockerfile
    assert "FROM python:3.11.16-slim@sha256:" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "pip install" not in dockerfile
    assert "uv.lock pyproject.toml README.md LICENSE NOTICE" in dockerfile
    assert "COPY .env" not in dockerfile
    assert "127.0.0.1:8000:8000" in compose
    assert "ODOO_MCP_ODOO_API_KEY" in compose
    assert '"/app/.venv/bin/python"' in docker_verifier


def test_pypi_publish_workflow_builds_tags_from_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

    assert '      - "v*"' in workflow
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in workflow
    assert "python -m build" in workflow
    assert "python -m twine check dist/*" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "secrets.PYPI_API_TOKEN" in workflow

    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'uvx --from "$WHEEL" odoo-erp-mcp --help' in ci_workflow


def test_live_invoicing_qualifier_requires_explicit_write_flag() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_live_invoicing_odoo19.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "Live Odoo 19 invoicing qualification refused: explicit write flag required.\n"
    )
