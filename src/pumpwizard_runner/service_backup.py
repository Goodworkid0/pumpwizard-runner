"""Offline, integrity-checked backup and restore for local service state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

from . import __version__
from .background_service import ServiceOwnerLock


BACKUP_SCHEMA = "pumpwizard.service-backup/v1"
MANIFEST = "manifest.json"
EXCLUDED = (
    "background-service-access-token",
    "background-service.lock",
    "background-service.log",
    "solanamarket-wallet.json",
    "automation-wallet/",
    "ai-credentials/",
    "solanamarket-agent-settings.json",
)


def create_backup(data_dir: Path, destination: Path) -> dict[str, object]:
    """Create a new offline backup directory without copying launch authority."""
    source = Path(data_dir).resolve()
    target = Path(destination).resolve()
    if not source.is_dir():
        raise ValueError("Service data directory does not exist")
    _require_new_target(target, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=target.name + ".backup-", dir=target.parent))
    os.chmod(stage, 0o700)
    owner = ServiceOwnerLock(source)
    owner.acquire()
    try:
        entries: list[dict[str, object]] = []
        databases = sorted(source.glob("*.sqlite"))
        for database in databases:
            _regular_private_source(database, "SQLite database")
            relative = database.name
            output = stage / relative
            _backup_database(database, output)
            entries.append(_entry(stage, relative, "sqlite"))

        vault_root = source / "service-wallets"
        if vault_root.exists():
            details = vault_root.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise ValueError("Service wallet vault must be a private directory")
            for record in sorted(vault_root.glob("*.json")):
                _regular_private_source(record, "Service wallet ciphertext")
                relative = f"service-wallets/{record.name}"
                output = stage / relative
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _copy_regular(record, output, 0o600)
                entries.append(_entry(stage, relative, "vault-ciphertext"))

        if not entries:
            raise ValueError("Service data directory has no durable databases or vault ciphertext")
        manifest = {
            "schema": BACKUP_SCHEMA,
            "runner_version": __version__,
            "created_at": time.time(),
            "data_directory_id": hashlib.sha256(str(source).encode()).hexdigest(),
            "entries": entries,
            "excluded": list(EXCLUDED),
            "restore_policy": "new-directory-only; service starts locked",
        }
        _write_new(stage / MANIFEST, _json(manifest), 0o600)
        os.replace(stage, target)
        stage = None
        return {**manifest, "destination": str(target)}
    finally:
        owner.release()
        if stage is not None:
            shutil.rmtree(stage)


def restore_backup(backup_dir: Path, destination: Path) -> dict[str, object]:
    """Restore a verified same-version backup into a new data directory."""
    backup = Path(backup_dir).resolve()
    target = Path(destination).resolve()
    if not backup.is_dir():
        raise ValueError("Service backup directory does not exist")
    _require_new_target(target, backup)
    manifest = _read_manifest(backup)
    if manifest.get("runner_version") != __version__:
        raise ValueError(
            "Restore requires the same Runner version; use the pre-upgrade backup for downgrade"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Service backup manifest has no entries")
    expected_paths = {MANIFEST}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Service backup manifest entry is invalid")
        relative = _relative(entry.get("path"))
        expected_paths.add(relative)
        source = backup / relative
        _regular_private_source(source, "Backup entry")
        if source.stat().st_size != entry.get("size") or _sha256(source) != entry.get("sha256"):
            raise ValueError(f"Service backup entry failed integrity verification: {relative}")
    actual_paths = {
        str(path.relative_to(backup))
        for path in backup.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise ValueError("Service backup contains unmanifested or missing files")

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=target.name + ".restore-", dir=target.parent))
    os.chmod(stage, 0o700)
    try:
        for entry in entries:
            relative = _relative(entry["path"])
            source = backup / relative
            output = stage / relative
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_regular(source, output, 0o600)
            if entry.get("kind") == "sqlite":
                _quick_check(output)
        os.replace(stage, target)
        stage = None
        return {
            "schema": BACKUP_SCHEMA,
            "runner_version": __version__,
            "restored": True,
            "destination": str(target),
            "entries": len(entries),
            "signing_state": "locked-on-start",
            "notice": "Launch tokens, logs and lock files were not restored.",
        }
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def _read_manifest(root: Path) -> dict[str, object]:
    path = root / MANIFEST
    _regular_private_source(path, "Backup manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("Service backup manifest is unreadable") from None
    if not isinstance(value, dict) or value.get("schema") != BACKUP_SCHEMA:
        raise ValueError("Unsupported service backup manifest")
    return value


def _require_new_target(target: Path, source: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ValueError("Backup and restore destinations must not already exist")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("Backup and restore destinations must be outside the source directory")


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("Service backup path is invalid")
    path = Path(value)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("Service backup path is invalid")
    normalized = str(path)
    if normalized == MANIFEST or not (
        (len(path.parts) == 1 and path.suffix == ".sqlite")
        or (len(path.parts) == 2 and path.parts[0] == "service-wallets" and path.suffix == ".json")
    ):
        raise ValueError("Service backup path is outside the allowed state set")
    return normalized


def _backup_database(source: Path, destination: Path) -> None:
    origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    copied = sqlite3.connect(destination)
    try:
        if origin.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite quick_check failed before backup: {source.name}")
        origin.backup(copied)
        copied.commit()
    finally:
        copied.close()
        origin.close()
    os.chmod(destination, 0o600)
    _quick_check(destination)


def _quick_check(path: Path) -> None:
    database = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=15
    )
    try:
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite quick_check failed: {path.name}")
    finally:
        database.close()


def _regular_private_source(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise ValueError(f"{label} is missing") from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"{label} must be a regular file")


def _copy_regular(source: Path, destination: Path, mode: int) -> None:
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    try:
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(source_fd, "rb", closefd=False) as input_file:
            with os.fdopen(destination_fd, "wb", closefd=False) as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _write_new(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _entry(root: Path, relative: str, kind: str) -> dict[str, object]:
    path = root / relative
    return {
        "path": relative,
        "kind": kind,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
