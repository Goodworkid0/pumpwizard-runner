"""Focused candidate-manifest and Linux launcher tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def load_script(name: str):
    path = ROOT / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"candidate_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_manifest_includes_untracked_inputs_and_excludes_generated_trees(tmp_path: Path):
    build = load_script("build")
    included = {
        "src/pumpwizard_runner/new_module.py": "print('included')\n",
        "packaging/tool.py": "# included\n",
        "docs/operator.md": "included\n",
        "web-client/src/App.tsx": "export default 1\n",
        "web-client/public/logo.txt": "included\n",
        "pyproject.toml": "[project]\nversion='test'\n",
    }
    for relative, content in included.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    generated = tmp_path / "web-client" / "src" / "node_modules" / "secret-token.txt"
    generated.parent.mkdir(parents=True)
    generated.write_text("must-not-enter-manifest")
    cached = tmp_path / "src" / "pumpwizard_runner" / "__pycache__" / "a.pyc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"generated")

    first = build.source_manifest(tmp_path)
    paths = {row["path"] for row in first["files"]}

    assert set(included).issubset(paths)
    assert generated.relative_to(tmp_path).as_posix() not in paths
    assert cached.relative_to(tmp_path).as_posix() not in paths
    (tmp_path / "src/pumpwizard_runner/new_module.py").write_text("print('changed')\n")
    assert build.source_manifest(tmp_path)["source_checkpoint"] != first["source_checkpoint"]


def test_linux_candidate_validation_and_desktop_entry_use_exact_executable(tmp_path: Path):
    launcher = load_script("linux_launcher")
    root = tmp_path / "candidate with spaces"
    internal = root / "_internal"
    internal.mkdir(parents=True)
    executable = root / "SolanaMarketWizard"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' 'PumpWizard Runner 9.8.7'\n")
    executable.chmod(0o755)

    assert launcher.validate_candidate(executable, "9.8.7") == "PumpWizard Runner 9.8.7"
    entry = launcher.render_desktop_entry(executable, "9.8.7")
    assert f'Exec="{executable}" service launch-dashboard' in entry
    assert "PumpWizard Runner 9.8.7 local candidate" in entry
    assert "launch.sh" not in entry

    with pytest.raises(ValueError, match="expected"):
        launcher.validate_candidate(executable, "9.8.8")
