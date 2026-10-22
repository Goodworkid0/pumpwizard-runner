"""Validate a Linux candidate and stage its application-menu entry."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def source_version(root: Path = ROOT) -> str:
    text = (Path(root) / "src" / "pumpwizard_runner" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ValueError("Runner source version is missing")
    return match.group(1)


def validate_candidate(executable: Path, expected_version: str) -> str:
    candidate = Path(executable).absolute()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError("Candidate executable is missing or is not executable")
    if not (candidate.parent / "_internal").is_dir():
        raise ValueError("Candidate _internal directory is missing")
    version = subprocess.run(
        [str(candidate), "--version"], check=True, capture_output=True, text=True,
        timeout=30,
    ).stdout.strip()
    wanted = f"PumpWizard Runner {expected_version}"
    if version != wanted:
        raise ValueError(f"Candidate reported {version!r}; expected {wanted!r}")
    return version


def render_desktop_entry(executable: Path, version: str) -> str:
    selected = str(Path(executable).absolute())
    if any(character in selected for character in "\n\r\t"):
        raise ValueError("Candidate path must not contain tabs or newlines")
    # Desktop entry field codes use %, while Exec quoting also treats backslash,
    # quote, backtick and dollar specially.
    escaped = selected.replace("%", "%%").replace("\\", "\\\\")
    for character in ('"', '`', '$'):
        escaped = escaped.replace(character, "\\" + character)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=PumpWizard Runner\n"
        f"Comment=PumpWizard Runner {version} local candidate\n"
        f'Exec="{escaped}" service launch-dashboard\n'
        "Icon=applications-office\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
        "Keywords=Solana;Pump;Trading;Wallet;\n"
        "StartupNotify=false\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "desktop")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--expected-version", default=source_version())
    args = parser.parse_args()
    build_name = (args.expected_version.split(".")[-1]
                  if ".dev" in args.expected_version else args.expected_version)
    candidate = args.candidate or (
        ROOT / "dist" / build_name / "SolanaMarketWizard" / "SolanaMarketWizard"
    )
    try:
        validate_candidate(candidate, args.expected_version)
        entry = render_desktop_entry(candidate, args.expected_version)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / "pumpwizard-runner.desktop"
    destination.write_text(entry, encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
