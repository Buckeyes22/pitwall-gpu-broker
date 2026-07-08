"""Regression tests for the release OpenAPI compatibility gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "ci" / "check_openapi_compat.py"
    spec = importlib.util.spec_from_file_location("check_openapi_compat", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compatibility_gate_rejects_removed_operation_and_new_required_field() -> None:
    baseline = {
        "paths": {
            "/v1/items": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"required": ["name"]}}}
                    },
                    "responses": {"201": {}},
                },
                "get": {"responses": {"200": {}}},
            }
        }
    }
    candidate = {
        "paths": {
            "/v1/items": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"required": ["name", "region"]}}
                        }
                    },
                    "responses": {"202": {}},
                }
            }
        }
    }
    errors = _module().compare(baseline, candidate)
    assert "removed operation: GET /v1/items" in errors
    assert "removed success response: POST /v1/items 201" in errors
    assert "new required field: POST /v1/items region" in errors
