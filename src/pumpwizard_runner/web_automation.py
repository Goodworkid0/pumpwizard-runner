"""Owner-authenticated browser facade for the separate Automation Wallet."""
import json
from pathlib import Path
import sqlite3
import threading
import time

from .strategy_support import reviewed_eligible
from .strategies import parse_package
from .transfers import TransferError, parse_sol
from .wallet_vault import WalletVault, WalletVaultError, VAULT_SCHEMA

LOCK = threading.RLock()
GATE = 'Automatic activation remains blocked until funded lifecycle and strategy-owned acceptance are recorded.'
SOL_FIELDS = (
    'budget_sol', 'max_order_sol', 'daily_buy_sol', 'daily_loss_sol',
    'reserve_sol', 'portfolio_drawdown_sol',
)
COUNT_FIELDS = {'max_buys_per_day': (1, 20), 'max_trades_per_day': (1, 40), 'max_positions': (1, 5),
                'slippage_bps': (1, 500), 'duration_minutes': (1, 60), 'daily_loss_bps': (1, 10000),
                'trade_loss_bps': (1, 10000)}


def _open(directory):
    root = Path(directory) / 'automation-wallet'
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'settings.sqlite', timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return db, WalletVault(root)


def _get(db, key):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set(db, key, value):
    with db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))


def _owned(db, sender):
    owner = _get(db, 'owner')
    if owner and owner != sender:
        raise TransferError('Sign in with the wallet that created this Automation Wallet')


def state(directory, sender, network='mainnet-beta', *, library=None, controller=None):
    with LOCK:
        db, vault = _open(directory)
        try:
            _owned(db, sender)
            address = vault._read_record()['address'] if vault.exists() else None
            plan = _get(db, 'plan')
            runtime = controller.status(library, sender, network, address, plan) if controller and library else {}
            authorizations = runtime.get('authorizations', [])
            active = next((row for row in authorizations if row['status'] in ('authorized', 'running', 'paused')), None)
            return dict(
                address=address,
                owner=_get(db, 'owner'),
                locked=not bool(active and controller.manager.available(active['id'])),
                active=bool(runtime.get('runtime', {}).get('running')),
                gate=GATE,
                plan=plan,
                stopped_at=_get(db, 'stopped_at'),
                backup_verified_at=_get(db, 'backup_verified_at'),
                **runtime,
            )
        finally:
            db.close()


def command(library, action, body, *, controller=None):
    identity = {'sender', 'network'}
    sender = body['sender']
    if body['network'] != 'mainnet-beta':
        raise TransferError('Automation Wallet setup currently supports mainnet only')
    with LOCK:
        db, vault = _open(library.path.parent)
        try:
            _owned(db, sender)
            if action == 'create':
                if set(body) != identity | {'password'}:
                    raise TransferError('Supply a new vault password')
                # Persist ownership first; an interrupted creation cannot be claimed by another wallet.
                _set(db, 'owner', sender)
                wallet = vault.create(body['password'])
                if wallet.address == sender:
                    raise TransferError('Automation and funding wallets must differ')
                del wallet
            elif action == 'backup':
                if set(body) != identity | {'password'}:
                    raise TransferError('Authenticate before exporting an encrypted backup')
                wallet = vault.unlock(body['password'])
                del wallet
                _set(db, 'backup_verified_at', time.time())
                return dict(backup={'schema': 'pumpwizard.automation-backup/v1', 'owner': sender, 'vault': vault._read_record()})
            elif action == 'restore':
                if set(body) != identity | {'password', 'backup'} or vault.exists():
                    raise TransferError('Restore requires an empty Automation Wallet vault')
                backup = body['backup']
                if not isinstance(backup, dict) or set(backup) != {'schema', 'owner', 'vault'} or backup['schema'] != 'pumpwizard.automation-backup/v1' or backup['owner'] != sender:
                    raise TransferError('Backup does not belong to this funding wallet')
                if not isinstance(backup['vault'], dict) or backup['vault'].get('schema') != VAULT_SCHEMA:
                    raise TransferError('Unsupported encrypted vault format')
                wallet = vault._unlock_record(backup['vault'], body['password'])
                if wallet.address == sender:
                    raise TransferError('Automation and funding wallets must differ')
                del wallet
                _set(db, 'owner', sender)
                vault._write_record(backup['vault'])
            elif action == 'plan':
                fields = identity | set(SOL_FIELDS) | set(COUNT_FIELDS) | {'sessions', 'priority_fee_lamports'}
                if set(body) != fields or not vault.exists():
                    raise TransferError('Create the separate wallet and supply every authorization limit')
                values = {k: parse_sol(body[k]) for k in SOL_FIELDS}
                if not values['max_order_sol'] <= values['daily_buy_sol'] <= values['budget_sol'] <= 100_000_000:
                    raise TransferError('Require maximum order ≤ daily buys ≤ total budget ≤ 0.1 SOL')
                if values['daily_loss_sol'] > values['budget_sol']:
                    raise TransferError('Daily loss cannot exceed the total non-replenishing budget')
                if values['reserve_sol'] > 100_000_000:
                    raise TransferError('Wallet reserve cannot exceed 0.1 SOL in this first version')
                if values['portfolio_drawdown_sol'] > values['budget_sol']:
                    raise TransferError('Portfolio drawdown cannot exceed the total non-replenishing budget')
                for key, (low, high) in COUNT_FIELDS.items():
                    if type(body[key]) is not int or not low <= body[key] <= high:
                        raise TransferError(f'{key} must be an integer from {low} to {high}')
                if body['max_buys_per_day'] > body['max_trades_per_day']:
                    raise TransferError('Maximum buys cannot exceed maximum trades')
                if type(body['priority_fee_lamports']) is not int or body['priority_fee_lamports'] != 0:
                    raise TransferError('Only a zero priority-fee ceiling is currently supported')
                sessions = body['sessions']
                if not isinstance(sessions, list) or not 1 <= len(sessions) <= 5 or len(set(sessions)) != len(sessions):
                    raise TransferError('Choose one to five distinct allowed strategy sessions')
                for ident in sessions:
                    if not isinstance(ident, str) or not reviewed_eligible(parse_package(library.instance(ident)['package'])):
                        raise TransferError('Only pinned Core Frequency sessions can be authorized')
                previous = _get(db, 'plan') or {}
                revision = int(previous.get('revision', 0)) + 1
                _set(db, 'plan', {
                    **{k: body[k] for k in fields-identity},
                    'routes': ['pump_bonding_curve_sol', 'canonical_pumpswap_exit'],
                    'saved_at': time.time(), 'status': 'draft', 'revision': revision,
                })
            elif action == 'review':
                if set(body) != identity or controller is None or not vault.exists():
                    raise TransferError('Save a complete draft before reviewing authorization')
                plan = _get(db, 'plan')
                if not plan:
                    raise TransferError('Save a complete draft before reviewing authorization')
                review = controller.review(library, sender, body['network'], vault._read_record()['address'], plan)
                return dict(review=review, **state(library.path.parent, sender, body['network'], library=library, controller=controller))
            elif action == 'authorize':
                if set(body) != identity | {'review_id', 'password'} or controller is None:
                    raise TransferError('Authorize the exact reviewed limits with the vault password')
                plan = _get(db, 'plan')
                if not plan or not vault.exists():
                    raise TransferError('Create the wallet and review a complete draft first')
                ident = controller.authorize(
                    library, sender, body['network'], vault._read_record()['address'],
                    plan, body['review_id'], body['password'],
                )
                return dict(authorization_id=ident, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'start':
                if set(body) != identity | {'authorization_id'} or controller is None or not vault.exists():
                    raise TransferError('Start requires the current in-memory authorization')
                controller.start(
                    body['authorization_id'], sender, vault._read_record()['address'], body['network']
                )
            elif action == 'pause':
                if set(body) != identity | {'authorization_id', 'paused'} or type(body['paused']) is not bool or controller is None or not vault.exists():
                    raise TransferError('Pause requires an authorization ID and paused boolean')
                controller.pause_entries(
                    body['authorization_id'], sender, vault._read_record()['address'],
                    body['network'], body['paused'],
                )
            elif action == 'recovery-withdraw-review':
                if set(body) != identity | {'amount'} or not isinstance(body['amount'], str) or controller is None or not vault.exists():
                    raise TransferError('Recovery review requires an exact SOL amount')
                preview = controller.recovery_withdraw_review(
                    sender, body['network'], vault._read_record()['address'], body['amount']
                )
                return dict(recovery_preview=preview, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'recovery-withdraw-submit':
                if set(body) != identity | {'review_id', 'password'} or controller is None or not vault.exists():
                    raise TransferError('Submit the exact recovery review with the vault password')
                signature = controller.recovery_withdraw_submit(
                    sender, body['network'], vault._read_record()['address'],
                    body['review_id'], body['password'],
                )
                return dict(signature=signature, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'recovery-release-position':
                if set(body) != identity | {'position_id', 'acknowledge'} or body.get('acknowledge') != 'stop-and-release-automation-management' or controller is None or not vault.exists():
                    raise TransferError('Acknowledge that automation management and cash allocations will be released')
                released = controller.recovery_release_position(
                    sender, body['network'], vault._read_record()['address'], body['position_id']
                )
                return dict(recovery_release=released, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'recovery-sell-review':
                if set(body) != identity | {'position_id', 'slippage_bps'} or controller is None or not vault.exists():
                    raise TransferError('Recovery sell review requires a position and slippage ceiling')
                preview = controller.recovery_sell_review(
                    sender, body['network'], vault._read_record()['address'],
                    body['position_id'], body['slippage_bps'],
                )
                return dict(recovery_preview=preview, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'recovery-sell-submit':
                if set(body) != identity | {'review_id', 'password'} or controller is None or not vault.exists():
                    raise TransferError('Submit the exact recovery sell with the vault password')
                signature = controller.recovery_sell_submit(
                    sender, body['network'], vault._read_record()['address'],
                    body['review_id'], body['password'],
                )
                return dict(signature=signature, **state(
                    library.path.parent, sender, body['network'], library=library, controller=controller
                ))
            elif action == 'recovery-reconcile':
                if set(body) != identity or controller is None or not vault.exists():
                    raise TransferError('Recovery receipt check takes only the connected wallet identity')
                controller.recovery_reconcile(
                    sender, body['network'], vault._read_record()['address']
                )
            elif action == 'stop':
                if set(body) != identity:
                    raise TransferError('Emergency stop takes only the connected wallet identity')
                if controller is not None:
                    controller.stop(sender, vault._read_record()['address'] if vault.exists() else None)
                _set(db, 'stopped_at', time.time())
            else:
                raise TransferError('Unknown Automation Wallet action')
        except (WalletVaultError, KeyError, TypeError) as exc:
            raise TransferError(str(exc) if isinstance(exc, WalletVaultError) else 'Invalid Automation Wallet request') from None
        finally:
            db.close()
    return state(
        library.path.parent, sender, body['network'], library=library, controller=controller
    )
