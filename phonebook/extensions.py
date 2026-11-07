from __future__ import annotations

from typing import Any

from .errors import UpstreamError


EXTENSIONS_PATH = "data.fetchAllExtensions.extension"
EXTENSION_KEYS = ("extension", "extensionId", "extension_id", "number", "user_extension")
NAME_KEYS = ("name", "display_name", "displayname", "description")


def _select_path(payload: Any, path: str) -> Any:
    selected = payload
    for part in path.split("."):
        if not isinstance(selected, dict) or part not in selected:
            raise UpstreamError(f"response does not contain expected path: {path}")
        selected = selected[part]
    return selected


def _looks_like_extension_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if not value:
        return True
    return all(
        isinstance(item, dict) and any(key in item for key in EXTENSION_KEYS)
        for item in value
    )


def _first_text(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_extensions(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict) and payload.get("errors"):
        raise UpstreamError("FreePBX GraphQL returned errors")
    records = _select_path(payload, EXTENSIONS_PATH)
    if payload["data"]["fetchAllExtensions"].get("status") is not True:
        raise UpstreamError("FreePBX could not fetch extensions")
    if not _looks_like_extension_list(records):
        raise UpstreamError(f"expected an extension list at {EXTENSIONS_PATH}")

    extensions: dict[str, dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        extension = _first_text(record, EXTENSION_KEYS)
        if not extension:
            continue
        name = _first_text(record, NAME_KEYS)
        if not name and isinstance(record.get("user"), dict):
            name = _first_text(record["user"], NAME_KEYS)
        if not name:
            name = extension
        extensions[extension] = {"extension": extension, "name": name}

    return sorted(
        extensions.values(),
        key=lambda item: (
            0 if item["extension"].isdigit() else 1,
            int(item["extension"]) if item["extension"].isdigit() else item["extension"].casefold(),
        ),
    )