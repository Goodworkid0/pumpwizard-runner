"""Regression checks for required local browser assets in native bundles."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


BUILD = Path(__file__).parents[1] / "packaging" / "build.py"
SPEC = importlib.util.spec_from_file_location("pumpwizard_packaging_build", BUILD)
assert SPEC and SPEC.loader
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def test_copy_browser_client_requires_built_index_and_copies_preview_assets(tmp_path: Path):
    browser = tmp_path / "web-client-dist"
    bundle = tmp_path / "bundle"
    browser.mkdir()
    bundle.mkdir()
    with pytest.raises(ValueError, match="npm run build"):
        build.copy_browser_client(browser, bundle)
    (browser / "index.html").write_text("<title>Browser</title>", encoding="utf-8")
    assets = browser / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export {}", encoding="utf-8")
    build.copy_browser_client(browser, bundle)
    assert (bundle / "web-client" / "index.html").read_text(encoding="utf-8") == "<title>Browser</title>"
    assert (bundle / "web-client" / "assets" / "app.js").is_file()
    instructions = (bundle / "BROWSER-APP.txt").read_text(encoding="utf-8")
    assert "legacy native client" in instructions
    assert "sign" in instructions.lower()
