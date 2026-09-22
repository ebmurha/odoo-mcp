from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_qualifier() -> ModuleType:
    path = ROOT / "scripts" / "verify_live_invoicing_odoo19.py"
    spec = importlib.util.spec_from_file_location("live_invoicing_qualifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manual_route_rejects_external_methods() -> None:
    qualifier = _load_qualifier()
    external = {
        "material_effects": {
            "valid_choices": [
                {
                    "journal": {"id": 10, "name": "Synthetic"},
                    "payment_method_line": {"id": 20, "name": "Synthetic"},
                    "payment_method_code": "electronic",
                }
            ]
        }
    }
    manual = {
        "material_effects": {
            "valid_choices": [
                {
                    "journal": {"id": 11, "name": "Synthetic"},
                    "payment_method_line": {"id": 21, "name": "Synthetic"},
                    "payment_method_code": "manual",
                }
            ]
        }
    }

    assert qualifier._manual_route(external) is None
    assert qualifier._manual_route(manual) == (11, 21)


def test_qualifier_suppresses_dependency_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    qualifier = _load_qualifier()
    logger = logging.getLogger("httpx")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    logger.addHandler(handler)

    async def synthetic_qualification() -> None:
        logger.info("configured-endpoint.invalid")

    monkeypatch.setattr(qualifier, "_qualify", synthetic_qualification)
    try:
        qualifier.main(["--execute-authorized-writes"])
    finally:
        logger.removeHandler(handler)
        logging.disable(logging.NOTSET)

    captured = capsys.readouterr()
    assert captured.out == f"{qualifier.SUCCESS}\n"
    assert captured.err == ""
