from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_metadata_is_consistent_and_installable() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    package = registry["packages"][0]

    assert project["project"]["version"] == "0.1.0"
    assert registry["name"] == "io.github.ebmurha/odoo-mcp"
    assert registry["version"] == project["project"]["version"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == project["project"]["name"]
    assert package["version"] == project["project"]["version"]
    assert package["transport"] == {"type": "stdio"}
    assert project["project"]["scripts"]["odoo-mcp-admin"] == "odoo_mcp.app.operations:main"


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
    )
    assert all((ROOT / path).is_file() for path in required)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mcp-name: io.github.ebmurha/odoo-mcp" in readme
    assert "USER odoo-mcp" in dockerfile
    assert "COPY .env" not in dockerfile
    assert "127.0.0.1:8000:8000" in compose
    assert "ODOO_MCP_ODOO_API_KEY" in compose
