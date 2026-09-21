from __future__ import annotations

from pathlib import Path

import pytest

from odoo_mcp.app import operations
from odoo_mcp.storage import Storage


def test_operations_cli_verifies_backs_up_and_restores_storage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_path = tmp_path / "source.sqlite3"
    source = Storage.open(source_path)
    source.capabilities.put("tenant-a", "connection-a", "19", {"account": True})
    backup_path = tmp_path / "backup.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"

    operations.main(["verify", "--storage", str(source_path)])
    operations.main(["backup", "--storage", str(source_path), "--destination", str(backup_path)])
    operations.main(["restore", "--source", str(backup_path), "--storage", str(restored_path)])

    restored = Storage.open(restored_path)
    assert restored.capabilities.get("tenant-a", "connection-a", "19") == {"account": True}
    assert capsys.readouterr().out.splitlines() == [
        "Storage verification passed.",
        "Storage backup completed.",
        "Storage restore validation passed.",
    ]


def test_operations_cli_fails_safely_without_overwriting_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "do-not-disclose.sqlite3"
    corrupt.write_bytes(b"not a database")
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"preserve me")

    with pytest.raises(SystemExit) as caught:
        operations.main(["restore", "--source", str(corrupt), "--storage", str(destination)])

    assert caught.value.code == 1
    assert destination.read_bytes() == b"preserve me"
    error = capsys.readouterr().err
    assert error == "Storage operation failed safely.\n"
    assert "do-not-disclose" not in error


@pytest.mark.parametrize("command", ["verify", "backup"])
def test_operations_cli_does_not_create_a_missing_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    source = tmp_path / "missing-sensitive-name.sqlite3"
    arguments = [command, "--storage", str(source)]
    if command == "backup":
        arguments.extend(["--destination", str(tmp_path / "backup.sqlite3")])

    with pytest.raises(SystemExit) as caught:
        operations.main(arguments)

    assert caught.value.code == 1
    assert not source.exists()
    error = capsys.readouterr().err
    assert error == "Storage operation failed safely.\n"
    assert "missing-sensitive-name" not in error
