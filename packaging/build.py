"""Create an inspectable native-folder candidate and checksummed archive."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from pumpwizard_runner.release_notes import note_for, note_url


ROOT = Path(__file__).parents[1]
BUILD_IDENTITY_SCHEMA = "pumpwizard.build-identity/v1"
SOURCE_ROOTS = (
    "src/pumpwizard_runner",
    "packaging",
    "docs",
    "web-client/src",
    "web-client/public",
)
SOURCE_FILES = (
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "launch.sh",
    "web-client/index.html",
    "web-client/package.json",
    "web-client/package-lock.json",
    "web-client/playwright.config.ts",
    "web-client/tsconfig.json",
    "web-client/tsconfig.app.json",
    "web-client/tsconfig.node.json",
    "web-client/vite.config.ts",
)
EXCLUDED_PARTS = {"__pycache__", "node_modules", "dist", "build", "test-results"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_manifest(root: Path = ROOT) -> dict[str, object]:
    root = Path(root).resolve()
    paths: set[Path] = set()
    for name in SOURCE_ROOTS:
        selected = root / name
        if selected.is_dir():
            paths.update(path for path in selected.rglob("*") if path.is_file())
    for name in SOURCE_FILES:
        selected = root / name
        if selected.is_file():
            paths.add(selected)
    entries = []
    for path in sorted(paths):
        relative = path.relative_to(root)
        if EXCLUDED_PARTS.intersection(relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        entries.append({
            "path": relative.as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        })
    checkpoint = hashlib.sha256(canonical_json(entries).encode()).hexdigest()
    return {
        "schema": "pumpwizard.source-manifest/v1",
        "source_checkpoint": checkpoint,
        "files": entries,
    }


def file_manifest(root: Path) -> list[dict[str, object]]:
    return [{
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    } for path in sorted(root.rglob("*")) if path.is_file()]


def command_version(command: list[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def dependency_identity() -> dict[str, str]:
    try:
        pyinstaller = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        pyinstaller = "not-installed"
    return {
        "python": platform.python_version(),
        "pyinstaller": pyinstaller,
        "node": command_version(["node", "--version"]),
        "npm": command_version(["npm", "--version"]),
        "platform": platform.platform(),
    }


def copy_browser_client(browser_dist: Path, bundle: Path) -> None:
    """Copy the exact browser build and its identity into a native bundle."""
    if not (browser_dist / "index.html").is_file():
        raise ValueError("Run npm run build; the isolated browser build did not produce index.html")
    shutil.copytree(browser_dist, bundle / "web-client")
    (bundle / "BROWSER-APP.txt").write_text(
        "PumpWizard Runner browser application\n\n"
        "Start the bundled background service explicitly, then open its verified Dashboard.\n"
        "The legacy native client remains available only for compatibility.\n"
        "Browser closure does not stop opted-in service work. Service restart never restores signing.\n"
        "Every funded action remains separately reviewed; production automatic execution is blocked.\n",
        encoding="utf-8",
    )


def build_frontend(destination: Path, identity: dict[str, object]) -> None:
    environment = os.environ.copy()
    environment.update({
        "PUMPWIZARD_BUILD_ID": str(identity["build_id"]),
        "PUMPWIZARD_FRONTEND_VERSION": str(identity["version"]),
        "PUMPWIZARD_SOURCE_CHECKPOINT": str(identity["source_checkpoint"]),
        "PUMPWIZARD_PACKAGED_AT": str(identity["packaged_at"]),
        "PUMPWIZARD_PACKAGE_KIND": str(identity["package_kind"]),
    })
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["npm", "run", "build", "--", "--outDir", str(destination), "--emptyOutDir"],
        check=True,
        cwd=ROOT / "web-client",
        env=environment,
    )
    write_json(destination / "build-identity.json", identity)


def version_from_source() -> str:
    version_file = (ROOT / "src/pumpwizard_runner/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', version_file, re.MULTILINE)
    if match is None:
        raise ValueError("Runner source version is missing")
    return match.group(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "release-artifacts")
    parser.add_argument(
        "--staging-root", type=Path, default=ROOT,
        help="Build/dist/frontend work root; use an isolated temporary directory for candidates",
    )
    args = parser.parse_args()
    version = version_from_source()
    post = note_for(version)  # Fail before building when the exact version has no note.
    build_name = version.split(".")[-1] if ".dev" in version else version
    staging_root = args.staging_root.resolve()
    dist = staging_root / "dist" / build_name
    build = staging_root / "build" / build_name
    frontend_dist = staging_root / "frontend" / build_name
    if dist.exists() or build.exists() or frontend_dist.exists():
        raise SystemExit(
            "Refusing to replace an existing candidate stage; choose a new --staging-root"
        )

    before = source_manifest()
    checkpoint = str(before["source_checkpoint"])
    packaged_at = datetime.now(timezone.utc).isoformat()
    system = platform.system().lower().replace("darwin", "macos")
    machine = platform.machine().lower()
    identity = {
        "schema": BUILD_IDENTITY_SCHEMA,
        "build_id": f"local-{checkpoint[:24]}",
        "version": version,
        "source_checkpoint": checkpoint,
        "packaged_at": packaged_at,
        "package_kind": f"{system}-{machine}-onedir-local-candidate",
        "dependencies": dependency_identity(),
    }
    build_frontend(frontend_dist, identity)

    build.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--collect-data", "pumpwizard_runner", "--name",
        "SolanaMarketWizard", "--distpath", str(dist), "--workpath", str(build / "work"),
        "--specpath", str(build), str(ROOT / "src" / "pumpwizard_runner" / "launcher.py"),
    ], check=True, cwd=ROOT)
    bundle = dist / "SolanaMarketWizard"
    shutil.copytree(ROOT / "docs", bundle / "docs")
    copy_browser_client(frontend_dist, bundle)
    for name in ("README.md", "CHANGELOG.md", "LICENSE"):
        shutil.copy2(ROOT / name, bundle / name)
    write_json(bundle / "BUILD-IDENTITY.json", identity)
    write_json(bundle / "SOURCE-MANIFEST.json", before)
    packaged_data = bundle / "_internal" / "pumpwizard_runner" / "data"
    if packaged_data.is_dir():
        write_json(packaged_data / "build-identity.json", identity)
    (bundle / "START-HERE.txt").write_text(
        f"PumpWizard Runner {version} — local Linux candidate\n\n"
        "Keep SolanaMarketWizard and _internal together. Use an absolute isolated data path.\n\n"
        "Inspect: ./SolanaMarketWizard --version\n"
        "Start:   ./SolanaMarketWizard --data-dir /ABSOLUTE/DATA service start --port 8845\n"
        "Open:    ./SolanaMarketWizard --data-dir /ABSOLUTE/DATA service open-dashboard --port 8845\n"
        "Status:  ./SolanaMarketWizard --data-dir /ABSOLUTE/DATA service status --port 8845\n"
        "Stop:    ./SolanaMarketWizard --data-dir /ABSOLUTE/DATA service stop --port 8845\n\n"
        "No install, enable, linger, funded action or public publication is performed by these commands.\n"
        "Read docs/PILOT-LINUX-CANDIDATE.md before migration or rollback.\n",
        encoding="utf-8",
    )
    (bundle / "PATCH-NOTES.txt").write_text(
        note_url(version)
        + "\n\nThis local candidate does not establish Community publication.\n",
        encoding="utf-8",
    )

    subprocess.run([
        sys.executable, str(ROOT / "packaging" / "linux_launcher.py"),
        "--candidate", str(bundle / "SolanaMarketWizard"),
        "--expected-version", version, "--output", str(bundle),
    ], check=True, cwd=ROOT)

    after = source_manifest()
    if after["source_checkpoint"] != before["source_checkpoint"]:
        raise SystemExit("Executable inputs changed during packaging; discard and rebuild")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact_stem = f"SolanaMarketWizard-{version}-{system}-{machine}"
    archive = output / f"{artifact_stem}.zip"
    manifest_path = output / f"{artifact_stem}-manifest.json"
    identity_path = output / f"{artifact_stem}-build-identity.json"
    source_path = output / f"{artifact_stem}-source-manifest.json"
    note_path = output / f"community-post-{version}.json"
    checksums = output / f"SHA256SUMS-{system}-{machine}.txt"
    for path in (archive, manifest_path, identity_path, source_path, note_path, checksums):
        if path.exists():
            raise SystemExit(f"Refusing to replace an existing candidate artifact: {path}")
    write_json(identity_path, identity)
    write_json(source_path, before)
    write_json(note_path, [post])
    write_json(manifest_path, {
        "schema": "pumpwizard.candidate-manifest/v1",
        "build_identity": identity,
        "bundle": file_manifest(bundle),
    })
    made = Path(shutil.make_archive(str(archive.with_suffix("")), "zip", dist))
    checksum_paths = (made, manifest_path, identity_path, source_path, note_path)
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_paths),
        encoding="utf-8",
    )
    print(made)


if __name__ == "__main__":
    main()
