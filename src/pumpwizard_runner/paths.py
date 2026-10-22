"""Stable locations for local application state."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    """Return the branded data directory, retaining existing local data on upgrade."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return _choose_data_dir(base / "SolanaMarketWizard", base / "PumpWizard")
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        return _choose_data_dir(base / "SolanaMarket Wizard", base / "PumpWizard")
    root = os.environ.get("XDG_DATA_HOME")
    base = Path(root) if root else Path.home() / ".local" / "share"
    return _choose_data_dir(base / "solanamarket-wizard", base / "pumpwizard")


def _choose_data_dir(current: Path, legacy: Path) -> Path:
    """Prefer an existing legacy directory once, so a rebrand never hides state."""
    return legacy if legacy.exists() and not current.exists() else current
