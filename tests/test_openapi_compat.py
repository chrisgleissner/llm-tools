"""API-011: the initial published v1 contract remains backward compatible."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_tools.openapi_compat import breaking_changes

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "docs/distributed" / name).read_text(encoding="utf-8"))


def test_current_openapi_contract_has_no_breaking_changes_from_v1_baseline() -> None:
    changes = breaking_changes(_load("openapi-v1-baseline.json"), _load("openapi.json"))
    assert changes == [], "breaking OpenAPI v1 changes:\n" + "\n".join(changes)


def test_breaking_change_checker_detects_removed_routes_operations_and_fields() -> None:
    previous = {
        "info": {"version": "v1"},
        "paths": {"/items": {"get": {"responses": {}}, "post": {"responses": {}}}},
        "components": {"schemas": {"Item": {"type": "object", "properties": {"id": {}, "name": {}}}}},
    }
    current = {
        "info": {"version": "v1"},
        "paths": {"/items": {"get": {"responses": {}}}},
        "components": {"schemas": {"Item": {"type": "object", "properties": {"id": {}}}}},
    }
    changes = breaking_changes(previous, current)
    assert any(item == "removed operation POST /items" for item in changes)
    assert any(item.endswith("removed field #/components/schemas/Item/name") for item in changes)


def test_breaking_change_checker_rejects_new_required_fields_and_allows_new_version() -> None:
    previous = {
        "info": {"version": "v1"}, "paths": {},
        "components": {"schemas": {"Item": {"properties": {"id": {}}, "required": ["id"]}}},
    }
    current = {
        "info": {"version": "v1"}, "paths": {},
        "components": {"schemas": {"Item": {"properties": {"id": {}, "name": {}}, "required": ["id", "name"]}}},
    }
    assert breaking_changes(previous, current) == ["new required field #/components/schemas/Item/name"]
    current["info"]["version"] = "v2"
    assert breaking_changes(previous, current) == []
