"""Private server-owned saved accounts and preferences, without signing authority."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from .transfers import public_key

DATABASE_NAME = "app-state.sqlite"
WALLET_SCHEMA = "pumpwizard.browser-wallet/v1"
MAX_WALLETS = 20
PREFERENCE_KEYS = {
    "pumpwizard.research-mode/v1",
    "pumpwizard.welcome-acknowledged-version/v1",
    "pumpwizard.wallet-session/v1",
    "pumpwizard.background-service-instance/v1",
    "pumpwizard.activity-muted",
    "pumpwizard.welcome.last-shown",
}


def validate_wallet(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema", "address", "created_at", "kdf", "cipher",
    } or value.get("schema") != WALLET_SCHEMA:
        raise ValueError("Saved wallet must contain only the supported encrypted backup fields")
    public_key(value["address"])
    if not isinstance(value["created_at"], str) or len(value["created_at"]) > 64:
        raise ValueError("Saved wallet creation time is invalid")
    try:
        datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Saved wallet creation time is invalid") from None
    kdf, cipher = value["kdf"], value["cipher"]
    if not isinstance(kdf, dict) or set(kdf) != {"name", "hash", "iterations", "salt"}:
        raise ValueError("Saved wallet key derivation is invalid")
    if (kdf["name"], kdf["hash"], kdf["iterations"]) != ("PBKDF2", "SHA-256", 310_000):
        raise ValueError("Saved wallet key derivation is unsupported")
    if not isinstance(cipher, dict) or set(cipher) != {"name", "iv", "ciphertext"}:
        raise ValueError("Saved wallet encryption is invalid")
    if cipher["name"] != "AES-GCM":
        raise ValueError("Saved wallet encryption is unsupported")
    for field, length in ((kdf["salt"], 16), (cipher["iv"], 12), (cipher["ciphertext"], 80)):
        try:
            valid = isinstance(field, str) and len(base64.b64decode(field, validate=True)) == length
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise ValueError("Saved wallet encrypted data is invalid")
    return value


def validate_preference(key: object, value: object) -> None:
    if not isinstance(key, str) or len(key) > 600:
        raise ValueError("Unsupported app preference")
    worth = key.startswith("pumpwizard.wallet-worth-history/v1:")
    portfolio_history = key.startswith("pumpwizard.portfolio-history/v1:")
    balance_history = key.startswith("pumpwizard.wallet-balance-history/v1:")
    balance_unit = key.startswith("pumpwizard.wallet-balance-unit/v1:")
    request = key.startswith(("research:", "forward-campaign:"))
    if key not in PREFERENCE_KEYS and not worth and not portfolio_history and not balance_history and not balance_unit and not request:
        raise ValueError("Unsupported app preference")
    if balance_history or balance_unit:
        parts = key.split(":")
        if len(parts) != 3 or parts[1] not in ("mainnet-beta", "devnet"):
            raise ValueError("Saved wallet balance identity is invalid")
        public_key(parts[2])
    if value is None:
        return
    if not isinstance(value, str) or len(value.encode()) > 64_000:
        raise ValueError("App preference is too large or invalid")
    if key == "pumpwizard.research-mode/v1":
        if value != "enabled":
            raise ValueError("Invalid research preference")
    elif key == "pumpwizard.activity-muted":
        if value not in ("true", "false"):
            raise ValueError("Invalid activity preference")
    elif key == "pumpwizard.welcome.last-shown":
        if not re.fullmatch(r"[0-9]{1,16}", value):
            raise ValueError("Invalid legacy welcome timestamp")
    elif key == "pumpwizard.welcome-acknowledged-version/v1":
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?", value):
            raise ValueError("Invalid acknowledged version")
    elif key == "pumpwizard.wallet-session/v1":
        record = json.loads(value)
        if not isinstance(record, dict) or set(record) != {
            "schema", "kind", "name", "address", "network", "market",
        }:
            raise ValueError("Saved wallet view may contain only public account metadata")
        public_key(record["address"])
        if (record["schema"] != "pumpwizard.wallet-session/v1"
                or record["kind"] not in ("local", "extension")
                or not isinstance(record["name"], str) or not 1 <= len(record["name"]) <= 120
                or record["network"] not in ("mainnet-beta", "devnet")
                or record["market"] not in (None, "pumpfun")):
            raise ValueError("Saved wallet view is invalid")
    elif key == "pumpwizard.background-service-instance/v1":
        record = json.loads(value)
        if (not isinstance(record, dict) or set(record) != {"id", "revision"}
                or not isinstance(record["id"], str) or len(record["id"]) > 64
                or type(record["revision"]) is not int or record["revision"] < 0):
            raise ValueError("Saved service identity is invalid")
    elif worth:
        parts = key.split(":")
        if len(parts) != 3 or parts[1] not in ("mainnet-beta", "devnet"):
            raise ValueError("Saved worth history identity is invalid")
        public_key(parts[2])
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Saved worth history is invalid")
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"at", "usd"}
                    or any(type(row[f]) not in (int, float) or not math.isfinite(row[f])
                           for f in ("at", "usd")) or row["at"] <= 0 or row["usd"] < 0):
                raise ValueError("Saved worth history is invalid")
    elif portfolio_history:
        parts = key.split(":")
        if len(parts) != 3 or parts[1] not in ("mainnet-beta", "devnet"):
            raise ValueError("Saved portfolio history identity is invalid")
        public_key(parts[2])
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > 700:
            raise ValueError("Saved portfolio history is invalid")
        previous_at = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"at", "usd", "pnl"}
                    or type(row["at"]) not in (int, float)
                    or not math.isfinite(row["at"]) or row["at"] <= previous_at
                    or any(row[field] is not None and (
                        type(row[field]) not in (int, float)
                        or not math.isfinite(row[field])) for field in ("usd", "pnl"))
                    or (row["usd"] is not None and row["usd"] < 0)):
                raise ValueError("Saved portfolio history is invalid")
            previous_at = row["at"]
    elif balance_unit:
        if value not in ("SOL", "USD"):
            raise ValueError("Saved wallet balance unit is invalid")
    elif balance_history:
        rows = json.loads(value)
        if not isinstance(rows, list) or len(rows) > 700:
            raise ValueError("Saved wallet balance history is invalid")
        previous_at = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"at", "lamports", "usd"}
                    or type(row["at"]) not in (int, float)
                    or not math.isfinite(row["at"]) or row["at"] <= previous_at
                    or not isinstance(row["lamports"], str)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,19}", row["lamports"])
                    or int(row["lamports"]) > 18_446_744_073_709_551_615
                    or row["usd"] is not None and (
                        type(row["usd"]) not in (int, float)
                        or not math.isfinite(row["usd"]) or row["usd"] < 0)):
                raise ValueError("Saved wallet balance history is invalid")
            previous_at = row["at"]
    elif request and not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise ValueError("Saved request identity is invalid")


class AppStateStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / DATABASE_NAME
        if self.path.is_symlink():
            raise ValueError("App state must be a private regular database")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS wallets (address TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def snapshot(self) -> dict:
        with closing(self._connect()) as db:
            return {
                "wallets": [json.loads(row[0]) for row in db.execute("SELECT record FROM wallets ORDER BY rowid")],
                "preferences": dict(db.execute("SELECT key, value FROM preferences")),
            }

    def migrate(self, wallets: object, preferences: object) -> dict:
        if not isinstance(wallets, list) or len(wallets) > MAX_WALLETS:
            raise ValueError("At most 20 saved wallets are supported")
        records = [validate_wallet(row) for row in wallets]
        if len({row["address"] for row in records}) != len(records):
            raise ValueError("Duplicate saved wallet address")
        if not isinstance(preferences, dict) or len(preferences) > 512:
            raise ValueError("App preferences are invalid")
        for key, value in preferences.items():
            validate_preference(key, value)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = dict(db.execute("SELECT address, record FROM wallets"))
            if len(set(existing) | {row["address"] for row in records}) > MAX_WALLETS:
                raise ValueError("This app already stores the maximum of 20 generated wallets")
            for row in records:
                serialized = json.dumps(row, sort_keys=True, allow_nan=False)
                if row["address"] in existing and existing[row["address"]] != serialized:
                    raise ValueError("An encrypted wallet with this address is already saved; existing account was kept")
                db.execute("INSERT OR IGNORE INTO wallets VALUES (?, ?)", (row["address"], serialized))
            for key, value in preferences.items():
                if value is not None:
                    db.execute("INSERT OR IGNORE INTO preferences VALUES (?, ?)", (key, value))
        return self.snapshot()

    def preference(self, key: object, value: object) -> dict:
        validate_preference(key, value)
        with closing(self._connect()) as db, db:
            if value is None:
                db.execute("DELETE FROM preferences WHERE key=?", (key,))
            else:
                if key.startswith(("research:", "forward-campaign:")):
                    db.execute("DELETE FROM preferences WHERE key IN (SELECT key FROM preferences WHERE key LIKE 'research:%' OR key LIKE 'forward-campaign:%' ORDER BY rowid DESC LIMIT -1 OFFSET 255)")
                db.execute("INSERT INTO preferences VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return {"saved": True}

    def remove_wallet(self, address: object) -> dict:
        public_key(address)
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM wallets WHERE address=?", (address,))
        return self.snapshot()


def read(data_dir: Path) -> dict:
    return AppStateStore(data_dir).snapshot()


def mutate(data_dir: Path, path: str, body: dict) -> dict:
    store = AppStateStore(data_dir)
    if path == "/api/v1/app-state/migrate" and set(body) == {"wallets", "preferences"}:
        return store.migrate(body["wallets"], body["preferences"])
    if path == "/api/v1/app-state/wallets" and set(body) == {"wallet"}:
        return store.migrate([body["wallet"]], {})
    if path == "/api/v1/app-state/wallets/remove" and set(body) == {"address"}:
        return store.remove_wallet(body["address"])
    if path == "/api/v1/app-state/preferences" and set(body) == {"key", "value"}:
        return store.preference(body["key"], body["value"])
    raise ValueError("Unsupported app state command")
