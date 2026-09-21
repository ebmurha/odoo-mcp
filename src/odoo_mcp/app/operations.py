"""Secret-safe storage operations for self-managed deployments."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from odoo_mcp.storage import Storage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odoo-mcp-admin")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="verify storage integrity and recovery state")
    verify.add_argument("--storage", type=Path, required=True)

    backup = commands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("--storage", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)

    restore = commands.add_parser("restore", help="restore into a new validated database")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--storage", type=Path, required=True)
    return parser


def _run(args: argparse.Namespace) -> str:
    if args.command == "verify":
        if not args.storage.is_file():
            raise FileNotFoundError("Storage database does not exist")
        Storage.open(args.storage).verify()
        return "Storage verification passed."
    if args.command == "backup":
        if not args.storage.is_file():
            raise FileNotFoundError("Storage database does not exist")
        storage = Storage.open(args.storage)
        storage.verify()
        storage.backup(args.destination)
        return "Storage backup completed."
    if args.command == "restore":
        Storage.restore(args.source, args.storage).verify()
        return "Storage restore validation passed."
    raise RuntimeError("Unsupported storage operation")


def main(argv: list[str] | None = None) -> None:
    """Run one bounded operation without printing paths or exception details."""

    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except Exception:
        print("Storage operation failed safely.", file=sys.stderr)
        raise SystemExit(1) from None
    print(result)


if __name__ == "__main__":
    main()
