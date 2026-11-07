"""Small standard-library checker for breaking changes to the published OpenAPI contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


def breaking_changes(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Return removed routes/operations/fields and newly required fields.

    A changed API version starts a new compatibility line. Within a version,
    compare both component schemas and inline schemas at their JSON locations.
    """
    if previous.get("info", {}).get("version") != current.get("info", {}).get("version"):
        return []
    changes: list[str] = []
    old_paths = previous.get("paths", {})
    new_paths = current.get("paths", {})
    for path, old_item in old_paths.items():
        new_item = new_paths.get(path)
        if new_item is None:
            changes.append(f"removed path {path}")
            continue
        for method, operation in old_item.items():
            if method.lower() in HTTP_METHODS and method not in new_item:
                changes.append(f"removed operation {method.upper()} {path}")

    old_schemas = _schema_nodes(previous)
    new_schemas = _schema_nodes(current)
    for location, old_schema in old_schemas.items():
        new_schema = new_schemas.get(location)
        if new_schema is None:
            continue
        old_properties = old_schema.get("properties", {})
        new_properties = new_schema.get("properties", {})
        for field in sorted(old_properties.keys() - new_properties.keys()):
            changes.append(f"removed field {location}/{field}")
        old_required = set(old_schema.get("required", []))
        new_required = set(new_schema.get("required", []))
        for field in sorted(new_required - old_required):
            if field in new_properties:
                changes.append(f"new required field {location}/{field}")
    return changes


def _schema_nodes(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    nodes: dict[str, Mapping[str, Any]] = {}

    def visit(value: Any, pointer: str) -> None:
        if isinstance(value, Mapping):
            if isinstance(value.get("properties"), Mapping):
                nodes[pointer] = value
            for key, item in value.items():
                visit(item, f"{pointer}/{_escape_pointer(str(key))}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{pointer}/{index}")

    visit(document.get("paths", {}), "#/paths")
    visit(document.get("components", {}).get("schemas", {}), "#/components/schemas")
    return nodes


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
