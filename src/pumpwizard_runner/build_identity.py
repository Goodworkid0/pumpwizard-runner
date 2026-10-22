"""Read the immutable identity embedded in a packaged Runner build."""
from __future__ import annotations

from importlib import resources
import json

from . import __version__


SCHEMA = "pumpwizard.build-identity/v1"
_FIELDS = {
    "schema", "build_id", "version", "source_checkpoint", "packaged_at", "package_kind",
}


def current_build_identity() -> dict[str, str]:
    """Return public, credential-free build identity for status responses.

    Source checkouts deliberately identify themselves as development builds. The
    candidate builder adds the JSON resource after PyInstaller has assembled the
    bundle, so the same code returns the exact packaged checkpoint at runtime.
    """
    fallback = {
        "schema": SCHEMA,
        "build_id": "development",
        "version": __version__,
        "source_checkpoint": "development",
        "packaged_at": "development",
        "package_kind": "source",
    }
    try:
        resource = resources.files("pumpwizard_runner").joinpath("data/build-identity.json")
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if not isinstance(value, dict) or set(value).intersection(_FIELDS) != _FIELDS:
        return fallback
    if value.get("schema") != SCHEMA or value.get("version") != __version__:
        return fallback
    public = {name: value[name] for name in _FIELDS}
    if not all(isinstance(item, str) and item for item in public.values()):
        return fallback
    return public
