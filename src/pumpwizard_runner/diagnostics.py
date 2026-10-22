"""Local support report with aggregate health; no addresses, paths or secrets."""
import platform
import sqlite3
import time

from . import __version__


def report(library, background_service=None):
    journals = {}
    path = library.path.parent / 'wallet-transfers.sqlite'
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)
        try:
            for table in ('trades', 'transfers', 'agent_orders', 'automation_authorizations'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    journals[table] = dict(db.execute(f'SELECT status,COUNT(*) FROM {table} GROUP BY status'))
            journals['integrity'] = 'ok' if db.execute('PRAGMA quick_check(1)').fetchone()[0] == 'ok' else 'needs attention'
        finally:
            db.close()
    lease = library.monitor_lease()
    service = None
    if background_service is not None:
        current = background_service.status()
        service = {
            "service": current.get("service"),
            "jobs": current.get("jobs", []),
            "trading": current.get("trading"),
        }
    return dict(schema='pumpwizard.diagnostics/v1', generated_at=time.time(), version=__version__,
                platform=platform.system(), architecture=platform.machine(), python=platform.python_version(),
                monitor=dict(lease_active=bool(lease and lease['expires_at']>time.time()),
                             sessions=len(library.instances())), journals=journals,
                background_service=service, automatic_signing=False,
                funded_acceptance='pending', canonical_onchain_history=False)
