"""Desktop workspace for local strategy agents and their position library."""
from __future__ import annotations

from pathlib import Path
import math
import queue
import sqlite3
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from . import __version__
from .agent_hub import AgentHub, AgentHubError, AgentSettings
from .ai_keys import AIKeys
from .ai_profile_panel import add_profile
from .assistant_panel import AssistantPanel
from .library import StrategyLibrary
from .market import MarketDataError, PumpPublicSource
from .ownership import OwnershipDataError
from .scheduler import MonitorScheduler
from .strategies import StrategyImportError, parse_package
from .strategy_browser import StrategyBrowser
from .strategy_onboarding import show_import
from .strategy_chooser import show_strategy_chooser
from .strategy_creator import show_strategy_creator
from .release_notes import note_url
from .research_views import session_state, filter_market, is_fresh, age_text, price_text
from .coin_panel import CoinPanel
from .strategy_support import import_comparison, support_label, support_text
from .evaluation_panel import EvaluationPanel
from .wallet_vault import LocalSolanaWallet, WalletAuthenticationError, WalletVault, WalletVaultError
from .wallet import SolanaWalletSource, WalletDataError
from .wallet_panel import WalletPanel
from .transfers import TransferJournal, TransferRpc, WithdrawalService
from .trade_panel import TradePanel, reconcile_wallet, token_text
from .trading_panel import TradingPanel
from .trading_workspace import wallet_positions


BACKGROUND = "#0b1020"
PANEL = "#121a2d"
PANEL_RAISED = "#18223a"
TEXT = "#edf2ff"
MUTED = "#98a7c5"
ACCENT = "#7c6cff"
ACCENT_HOVER = "#9287ff"
SUCCESS = "#41d19d"
WARNING = "#f2bc5e"
MARKET_WATCH_INTERVAL_MS = 15_000
WALLET_SYNC_INTERVAL_MS = 60_000
WALLET_RPC_URL = "https://api.mainnet-beta.solana.com"
MARKET_CAP_OPTIONS = {
    "Micro · $5K–$50K": (5_000.0, 50_000.0),
    "Early · $50K–$250K": (50_000.0, 250_000.0),
    "Growth · $250K–$1M": (250_000.0, 1_000_000.0),
    "Large · $1M–$10M": (1_000_000.0, 10_000_000.0),
    "All · $5K–$10M": (5_000.0, 10_000_000.0),
}
LIQUIDITY_OPTIONS = {
    "Any liquidity": 0.0,
    "$250+ liquidity": 250.0,
    "$1K+ liquidity": 1_000.0,
    "$5K+ liquidity": 5_000.0,
    "$10K+ liquidity": 10_000.0,
}


def _money(value: float | int | str | None) -> str:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        number = None
    if number is None:
        return "—"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}m"
    if number >= 1_000:
        return f"${number:,.0f}"
    return f"${number:,.2f}"


def _quantity(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.5f}" if 0 < number < .01 else f"{number:,.3f}"


def _relative_time(value: object) -> str:
    try:
        elapsed = max(0, int(time.time() - float(value)))
    except (TypeError, ValueError):
        return "never"
    if elapsed < 60:
        return f"{elapsed}s ago"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"


def _token_age(value: object) -> str:
    """Format a public token creation timestamp without implying unavailable precision."""
    try:
        created_at = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(created_at) or created_at <= 0 or created_at > time.time():
        return "—"
    elapsed = time.time() - created_at
    if elapsed < 3600:
        return f"{max(1, int(elapsed // 60))}m"
    if elapsed < 86400:
        return f"{int(elapsed // 3600)}h"
    return f"{int(elapsed // 86400)}d"


def _sparkline(values: list[float]) -> str:
    """Render a compact local trend line for a table cell."""
    if len(values) < 2 or not all(math.isfinite(value) and value > 0 for value in values):
        return ""
    low, high = min(values), max(values)
    if high == low:
        return "▅" * min(len(values), 12)
    blocks = "▁▂▃▄▅▆▇█"
    sample = values[-12:]
    return "".join(blocks[round((value - low) / (high - low) * (len(blocks) - 1))] for value in sample)


class RunnerWindow:
    """A local-first agent dashboard; monitoring remains separate from execution."""

    def __init__(self, root: tk.Tk, data_dir: Path):
        self.root = root
        self.data_dir = data_dir
        self.selected_agent_id: str | None = None
        self.selected_position_id: str | None = None
        self._scheduler_after: str | None = None
        self._closing = False
        self.active_section = "workspace"
        self._wallet_sync_thread: threading.Thread | None = None
        self._wallet_sync_result: tuple[bool, float | str, float | None] | None = None
        self._wallet_sync_after: str | None = None
        self.wallet_panel = None
        self.trade_panel = None
        self.trading_panel = None
        self.evaluation_panel = None
        self._wallet_position_cache = []
        self._trade_recovery_thread = None
        self._trade_recovery_after = None
        self._market_watch_thread: threading.Thread | None = None
        self._market_watch_result: tuple[bool, object] | None = None
        self._market_watch_after: str | None = None
        self._market_scan_error = ''
        self._market_last_success = None
        self._ui_refreshed_at = 0.0
        self._scheduler_warning = ''
        self.coin_panel = None

        self.root.title(f"PumpWizard Runner {__version__}")
        self.root.geometry("1360x820")
        self.root.minsize(1100, 680)
        self.root.configure(background=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self.wallet = self._unlock_local_wallet()
        self.library = StrategyLibrary(data_dir)
        self.scheduler = MonitorScheduler(data_dir)
        self.agent_hub = AgentHub(data_dir)
        self.ai_keys = AIKeys(data_dir)
        try:
            self.agent_settings = self.agent_hub.load()
        except AgentHubError as exc:
            self.agent_settings = AgentSettings()
            messagebox.showerror("AI-agent settings unavailable", str(exc), parent=self.root)
        self._build_layout()
        self.refresh()
        self._render_market_watch_rows(self.library.market_watch_rows(500))
        self.root.after(500, self.refresh_market_watch)
        self._wallet_sync_after = self.root.after(750, self.sync_wallet_balance)
        self.root.after(1_000, self.recover_manual_trades)
        self.scheduler.start()
        self._scheduler_after = self.root.after(500, self._poll_scheduler)

    def _unlock_local_wallet(self) -> LocalSolanaWallet:
        vault = WalletVault(self.data_dir)
        if not vault.exists():
            create_new = messagebox.askyesnocancel(
                "Set up wallet", "Create a new local wallet?\n\nChoose No to restore an encrypted wallet backup.", parent=self.root)
            if create_new is None:
                raise WalletVaultError("Wallet setup cancelled")
            if not create_new:
                filename = filedialog.askopenfilename(title="Restore encrypted wallet backup", parent=self.root,
                                                     filetypes=[("Wallet backup", "*.json")])
                if not filename:
                    raise WalletVaultError("Wallet restore cancelled")
                password = simpledialog.askstring("Backup password", "Enter your backup password:", show="•", parent=self.root)
                if password is None:
                    raise WalletVaultError("Wallet restore cancelled")
                try:
                    return vault.restore_backup(Path(filename), password)
                except WalletVaultError as exc:
                    messagebox.showerror("Cannot restore wallet", str(exc), parent=self.root)
                    raise
            messagebox.showinfo(
                "Create local Solana wallet",
                "PumpWizard Runner will create a local Solana wallet for this device. "
                "Set a vault password to encrypt its private key. If you lose that password, "
                "the wallet cannot be recovered.",
                parent=self.root,
            )
            while True:
                password = simpledialog.askstring(
                    "Set wallet password", "Use at least 12 characters to protect this local wallet:",
                    show="•", parent=self.root,
                )
                if password is None:
                    raise WalletVaultError("Wallet creation was cancelled")
                confirmation = simpledialog.askstring(
                    "Confirm wallet password", "Enter the vault password again:", show="•", parent=self.root,
                )
                if confirmation is None:
                    raise WalletVaultError("Wallet creation was cancelled")
                if password != confirmation:
                    messagebox.showerror("Passwords do not match", "Enter the same vault password twice.", parent=self.root)
                    continue
                try:
                    wallet = vault.create(password)
                except WalletVaultError as exc:
                    messagebox.showerror("Cannot create wallet", str(exc), parent=self.root)
                    continue
                messagebox.showinfo(
                    "Wallet created",
                    f"Your local Solana deposit address is:\n\n{wallet.address}\n\n"
                    "Copy it from the workspace after this dialog. Manual trades require a separate quote review and password confirmation.",
                    parent=self.root,
                )
                return wallet
        while True:
            password = simpledialog.askstring(
                "Unlock local Solana wallet", "Enter the vault password to load your wallet:",
                show="•", parent=self.root,
            )
            if password is None:
                raise WalletVaultError("Wallet unlock was cancelled")
            try:
                return vault.unlock(password)
            except WalletAuthenticationError:
                messagebox.showerror("Could not unlock wallet", "That password did not unlock this local wallet.", parent=self.root)
            except WalletVaultError as exc:
                messagebox.showerror("Cannot open wallet", str(exc), parent=self.root)
                raise

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=TEXT)
        style.map("Panel.TCheckbutton", background=[("active", PANEL)],
                  foreground=[("disabled", MUTED), ("active", TEXT)])
        style.configure("Card.TFrame", background=PANEL_RAISED)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT, font=("TkDefaultFont", 10))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("PanelAccent.TLabel", background=PANEL, foreground=ACCENT)
        style.configure("Card.TLabel", background=PANEL_RAISED, foreground=TEXT)
        style.configure("Title.TLabel", background=BACKGROUND, foreground=TEXT, font=("TkDefaultFont", 22, "bold"))
        style.configure("Hero.TLabel", background=PANEL, foreground=TEXT, font=("TkDefaultFont", 20, "bold"))
        style.configure("Brand.TLabel", background=PANEL, foreground=TEXT, font=("TkDefaultFont", 17, "bold"))
        style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=("TkDefaultFont", 10, "bold"))
        style.configure("Metric.TLabel", background=PANEL_RAISED, foreground=TEXT, font=("TkDefaultFont", 17, "bold"))
        style.configure("MetricCaption.TLabel", background=PANEL_RAISED, foreground=MUTED, font=("TkDefaultFont", 9))
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", borderwidth=0,
                        padding=(13, 8), font=("TkDefaultFont", 10, "bold"))
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#404b67")])
        style.configure("Subtle.TButton", background=PANEL_RAISED, foreground=TEXT, borderwidth=0, padding=(11, 7))
        style.map("Subtle.TButton", background=[("active", "#24304d")])
        style.configure('Chat.TButton', background=PANEL_RAISED, foreground=TEXT,
                        borderwidth=0, padding=(6, 4), font=('TkDefaultFont', 9))
        style.map('Chat.TButton', background=[('active', '#24304d')])
        style.configure('Version.TButton', background=PANEL_RAISED, foreground='#b5a8ff',
                        borderwidth=0, padding=(7, 3), font=('TkDefaultFont', 8))
        style.map('Version.TButton', background=[('active', '#32306b')])
        style.configure('ChatSend.TButton', background=ACCENT, foreground='#ffffff',
                        borderwidth=0, padding=(8, 4), font=('TkDefaultFont', 9, 'bold'))
        style.map('ChatSend.TButton', background=[('active', ACCENT_HOVER), ('disabled', '#404b67')])
        style.configure("Danger.TButton", background="#56263a", foreground="#ffdce4", borderwidth=0, padding=(11, 7))
        style.map("Danger.TButton", background=[("active", "#713047")])
        style.configure("App.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=36, borderwidth=0, font=("TkDefaultFont", 10))
        style.map("App.Treeview", background=[("selected", "#32306b")], foreground=[("selected", "#ffffff")])
        style.configure("App.Treeview.Heading", background=PANEL_RAISED, foreground=MUTED,
                        relief="flat", font=("TkDefaultFont", 9, "bold"), padding=(9, 8))
        style.map("App.Treeview.Heading", background=[("active", "#24304d")])
        style.configure("App.TNotebook", background=PANEL, borderwidth=0)
        style.configure("App.TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 10), borderwidth=0)
        style.map("App.TNotebook.Tab", background=[("selected", PANEL_RAISED)], foreground=[("selected", TEXT)])
        style.configure("Agent.TCombobox", fieldbackground=PANEL_RAISED, background=PANEL_RAISED,
                        foreground=TEXT, arrowcolor=TEXT, padding=7)
        style.map("Agent.TCombobox", fieldbackground=[("readonly", PANEL_RAISED)], foreground=[("readonly", TEXT)])
        style.configure('TEntry', fieldbackground=PANEL_RAISED, foreground=TEXT, insertcolor=TEXT,
                        bordercolor='#34415f', lightcolor=PANEL_RAISED, darkcolor=PANEL_RAISED, padding=6)
        style.map('TEntry', fieldbackground=[('disabled', PANEL)], foreground=[('disabled', MUTED)])
        for name in ('Vertical.TScrollbar', 'Horizontal.TScrollbar'):
            style.configure(name, background=PANEL_RAISED, troughcolor=PANEL, arrowcolor=MUTED,
                            bordercolor=PANEL, lightcolor=PANEL_RAISED, darkcolor=PANEL_RAISED)
        style.configure('App.TNotebook', bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure('App.TNotebook.Tab', bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure('App.Treeview', bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure('Session.App.Treeview', rowheight=30)
        style.configure('Agent.TCombobox', bordercolor='#2b3852', lightcolor=PANEL, darkcolor=PANEL, arrowsize=13)
        style.map('Agent.TCombobox', selectbackground=[('readonly', PANEL_RAISED)],
                  selectforeground=[('readonly', TEXT)])
        style.configure('TSeparator', background='#29344a')
        style.configure('App.TNotebook.Tab', bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, padding=(12, 8))

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=16)
        outer.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=0, minsize=270)
        outer.columnconfigure(1, weight=1, minsize=730)
        outer.rowconfigure(1, weight=1)

        topbar = ttk.Frame(outer, style="Panel.TFrame", padding=(18, 12))
        topbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        topbar.columnconfigure(1, weight=1, minsize=360)
        brand = ttk.Frame(topbar, style="Panel.TFrame")
        brand.grid(row=0, column=0, sticky="w", padx=(0, 18), pady=(0, 12))
        ttk.Label(brand, text="PumpWizard Runner", style="Brand.TLabel",
                  font=('TkDefaultFont', 16, 'bold')).grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="Solana Markets Trading Bot", style="Muted.TLabel",
                  font=('TkDefaultFont', 9)).grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.version_badge = ttk.Button(brand, text=f'v{__version__} ↗', style='Version.TButton',
                                        cursor='hand2', command=self.show_patch_notes)
        self.version_badge.grid(row=2, column=0, sticky='w', pady=(9, 0))
        self.nav_buttons: dict[str, ttk.Button] = {}
        nav = ttk.Frame(topbar, style='Panel.TFrame')
        nav.grid(row=1, column=0, sticky='w')
        for label, section, command in (('Market', 'workspace', self._show_welcome),
                                         ('Strategies', 'strategies', self.open_strategies)):
            button = ttk.Button(nav, text=label, width=10, style='Subtle.TButton', command=command)
            button.pack(side='left', padx=(0, 6))
            self.nav_buttons[section] = button
        wallet_controls = ttk.Frame(topbar, style="Panel.TFrame")
        wallet_controls.grid(row=0, column=2, rowspan=2, sticky="e", padx=(18, 0))
        self._build_wallet_navigation_card(wallet_controls).pack(anchor="e")
        wallet_actions = ttk.Frame(wallet_controls, style="Panel.TFrame")
        wallet_actions.pack(anchor="e", pady=(5, 0))
        ttk.Button(wallet_actions, text="Trade", width=7, style="Subtle.TButton", command=self.open_manual_trades).pack(side="left")
        ttk.Button(wallet_actions, text="Withdraw", width=8, style="Subtle.TButton", command=self.withdraw_wallet).pack(side="left", padx=5)
        settings_button = ttk.Button(wallet_actions, text="Settings", width=8, style="Subtle.TButton",
                                     command=lambda: self.show_section("settings"))
        settings_button.pack(side="left")
        self.nav_buttons["settings"] = settings_button

        self.sidebar = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        self.sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(2, weight=2)
        self.sidebar.rowconfigure(5, weight=1)
        session_heading = ttk.Frame(self.sidebar, style='Panel.TFrame')
        session_heading.grid(row=0, column=0, sticky='ew')
        session_heading.columnconfigure(0, weight=1)
        ttk.Label(session_heading, text="ACTIVE SESSIONS", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(session_heading, text='+', width=2, style='Accent.TButton', command=self.open_strategies).grid(
            row=0, column=1, sticky='e')
        self.session_summary = ttk.Label(self.sidebar, style='Muted.TLabel', wraplength=240, font=('TkDefaultFont', 9))
        self.session_summary.grid(row=1, column=0, sticky='w', pady=(9, 10))
        sessions = ttk.Frame(self.sidebar, style='Panel.TFrame')
        sessions.grid(row=2, column=0, sticky='nsew')
        sessions.columnconfigure(0, weight=1)
        sessions.rowconfigure(0, weight=1)
        self.agent_tree = ttk.Treeview(sessions, style="Session.App.Treeview", columns=("agent", "state"),
                                       show="headings", selectmode="browse", height=3)
        self.agent_tree.heading("agent", text="SESSION")
        self.agent_tree.heading("state", text="STATE")
        self.agent_tree.column("agent", width=130, minwidth=90, anchor="w")
        self.agent_tree.column("state", width=98, minwidth=95, anchor="center")
        self.agent_tree.grid(row=0, column=0, sticky="nsew")
        session_scroll = ttk.Scrollbar(sessions, orient='vertical', command=self.agent_tree.yview)
        session_scroll.grid(row=0, column=1, sticky='ns')
        self.agent_tree.configure(yscrollcommand=session_scroll.set)
        self.agent_tree.bind("<<TreeviewSelect>>", self._on_agent_selected)
        for tag, color in (('warning', WARNING), ('success', SUCCESS), ('muted', MUTED)):
            self.agent_tree.tag_configure(tag, foreground=color)
        self.session_empty = ttk.Label(sessions, text='No sessions yet.\nChoose Strategies to browse or import a configuration.',
                                       style='Muted.TLabel', wraplength=220, justify='center')
        self.session_empty.grid(row=0, column=0, sticky='n', pady=(45, 0))

        ttk.Separator(self.sidebar).grid(row=3, column=0, sticky="ew", pady=16)
        ttk.Label(self.sidebar, text="OPEN POSITIONS", style="Section.TLabel").grid(row=4, column=0, sticky="w")
        positions = ttk.Frame(self.sidebar, style='Panel.TFrame')
        positions.grid(row=5, column=0, sticky='nsew', pady=(9, 0))
        positions.columnconfigure(0, weight=1)
        positions.rowconfigure(0, weight=1)
        self.position_rail = ttk.Treeview(positions, style="App.Treeview", columns=("token", "agent"),
                                          show="headings", selectmode="browse", height=1)
        self.position_rail.heading("token", text="TOKEN")
        self.position_rail.heading("agent", text="SESSION")
        self.position_rail.column("token", width=125, anchor="w")
        self.position_rail.column("agent", width=100, anchor="w")
        self.position_rail.grid(row=0, column=0, sticky="nsew")
        position_scroll = ttk.Scrollbar(positions, orient='vertical', command=self.position_rail.yview)
        position_scroll.grid(row=0, column=1, sticky='ns')
        self.position_rail.configure(yscrollcommand=position_scroll.set)
        self.position_rail.bind("<<TreeviewSelect>>", self._on_rail_position_selected)

        self.workspace = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        self.workspace.grid(row=1, column=1, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self.welcome = self._build_welcome()
        self.agent_workspace = self._build_agent_workspace()
        self.settings = self._build_settings()
        self.strategy_browser = StrategyBrowser(self.workspace, self.library, self.data_dir,
            self.refresh, self._show_agent, self._show_welcome)
        self.assistant = AssistantPanel(topbar, self.data_dir, self.library, self.active_ai_profile,
                                        self.ai_keys, self.open_ai_settings, compact=True)
        self.assistant.grid(row=0, column=1, rowspan=2, sticky='nsew')
        self.show_section("workspace")

    def _build_welcome(self) -> ttk.Frame:
        frame = ttk.Frame(self.workspace, style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        heading = ttk.Frame(frame, style='Panel.TFrame')
        heading.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        ttk.Label(heading, text='Market', style='Hero.TLabel').pack(side='left')
        self.market_refresh_button = ttk.Button(heading, text='Refresh market', style='Subtle.TButton', command=self.refresh_market_watch)
        self.market_refresh_button.pack(side='right')
        ttk.Button(heading, text='Test setup', style='Subtle.TButton', command=self.setup_test_agent).pack(side='right', padx=8)
        search = ttk.Frame(frame, style='Panel.TFrame')
        search.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text='Coin / mint', style='Muted.TLabel').grid(row=0, column=0, padx=(0, 8))
        self.market_query = tk.StringVar()
        ttk.Entry(search, textvariable=self.market_query).grid(row=0, column=1, sticky='ew')
        self.market_sort = tk.StringVar(value='Newest first')
        sorter = ttk.Combobox(search, textvariable=self.market_sort, values=('Newest first', 'Highest liquidity', 'Highest market cap'),
                              width=18, state='readonly', style='Agent.TCombobox')
        sorter.grid(row=0, column=2, padx=8)
        sorter.bind('<<ComboboxSelected>>', self._on_market_filter_changed)
        ttk.Button(search, text='Reset filters', style='Subtle.TButton', command=self.reset_market_filters).grid(row=0, column=3)
        filters = ttk.Frame(frame, style='Panel.TFrame')
        filters.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        self.market_cap_filter = tk.StringVar(value='All · $5K–$10M')
        self.liquidity_filter = tk.StringVar(value='$1K+ liquidity')
        for label, variable, values, width in (('Cap', self.market_cap_filter, tuple(MARKET_CAP_OPTIONS), 18),
                                               ('Liquidity', self.liquidity_filter, tuple(LIQUIDITY_OPTIONS), 15)):
            ttk.Label(filters, text=label, style='Muted.TLabel').pack(side='left', padx=(0, 5))
            selector = ttk.Combobox(filters, textvariable=variable, values=values, width=width,
                                    state='readonly', style='Agent.TCombobox')
            selector.pack(side='left', padx=(0, 12))
            selector.bind('<<ComboboxSelected>>', self._on_market_filter_changed)
        self.market_fresh_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filters, text='Fresh only (90s)', variable=self.market_fresh_only,
                         command=self._on_market_filter_changed, style='Panel.TCheckbutton').pack(side='left')
        self.market_watch_status = ttk.Label(frame, text='Loading retained samples…', style='Muted.TLabel', wraplength=850)
        self.market_watch_status.grid(row=3, column=0, sticky='ew', pady=(0, 8))
        table = ttk.Frame(frame, style='Panel.TFrame')
        table.grid(row=4, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ('token', 'price', 'market_cap', 'liquidity', 'freshness', 'trend', 'age', 'watch')
        self.market_watch_tree = ttk.Treeview(table, style='App.Treeview', columns=columns, show='headings', selectmode='browse', height=4)
        for name, title, width in (('token', 'COIN / MINT', 180), ('price', 'PRICE', 110), ('market_cap', 'CAP', 100),
                                    ('liquidity', 'LIQUIDITY', 110), ('freshness', 'OBSERVED', 135),
                                    ('trend', 'RECORDED CAP Δ', 180), ('age', 'COIN AGE', 90), ('watch', 'SESSION EVIDENCE', 190)):
            self.market_watch_tree.heading(name, text=title)
            self.market_watch_tree.column(name, width=width, minwidth=60, stretch=name == 'token')
        self.market_watch_tree.grid(row=0, column=0, sticky='nsew')
        self.market_watch_tree.tag_configure('stale', foreground=WARNING)
        self.market_watch_tree.tag_configure('alternate', background=PANEL_RAISED)
        for orient, row, col, command, option, sticky in (
            ('vertical', 0, 1, self.market_watch_tree.yview, 'yscrollcommand', 'ns'),
            ('horizontal', 1, 0, self.market_watch_tree.xview, 'xscrollcommand', 'ew')):
            bar = ttk.Scrollbar(table, orient=orient, command=command)
            bar.grid(row=row, column=col, sticky=sticky)
            self.market_watch_tree.configure(**{option: bar.set})
        self.market_empty = ttk.Label(table, style='Muted.TLabel', wraplength=500, justify='center')
        self.market_empty.grid(row=0, column=0, pady=(48, 0))
        actions = ttk.Frame(frame, style='Panel.TFrame')
        actions.grid(row=5, column=0, sticky='ew', pady=(10, 0))
        self.coin_details_button = ttk.Button(actions, text='Coin details', style='Accent.TButton',
                                               state='disabled', command=self.open_coin_details)
        self.coin_details_button.pack(side='right')
        ttk.Label(actions, text='Double-click a coin for recorded history and session activity.', style='Muted.TLabel').pack(side='left')
        self.market_watch_tree.bind('<<TreeviewSelect>>', lambda _: self.coin_details_button.configure(
            state='normal' if self.market_watch_tree.selection() else 'disabled'))
        self.market_watch_tree.bind('<Double-1>', lambda e: self.open_coin_details() if self.market_watch_tree.identify_row(e.y) else None)
        self.market_watch_tree.bind('<Return>', lambda _: self.open_coin_details())
        self.market_query.trace_add('write', lambda *_: self._on_market_filter_changed())
        frame.bind('<Configure>', lambda e: self.market_watch_status.configure(wraplength=max(250, e.width-10)))
        return frame

    def reset_market_filters(self):
        self.market_cap_filter.set('All · $5K–$10M')
        self.liquidity_filter.set('$1K+ liquidity')
        self.market_fresh_only.set(False)
        self.market_sort.set('Newest first')
        self.market_query.set('')
        self._on_market_filter_changed()

    def open_coin_details(self):
        selected = self.market_watch_tree.selection()
        if not selected:
            return
        mint = selected[0]
        if self.coin_panel and not self.coin_panel.closed:
            if self.coin_panel.mint == mint:
                self.coin_panel.window.lift()
                return
            self.coin_panel.close()
        self.coin_panel = CoinPanel(self.root, self.library, mint, self.ask_about_coin, self._show_agent)

    def ask_about_coin(self, mint, symbol):
        ready = self.assistant.prepare_coin(mint, symbol)
        if ready:
            self.root.after_idle(self.assistant.review)
        return ready

    def refresh_market_watch(self) -> None:
        if self._market_watch_thread is not None:
            return
        if self._market_watch_after:
            self.root.after_cancel(self._market_watch_after)
            self._market_watch_after = None
        self.market_watch_status.configure(text="Scanning public Pump.fun market data…")
        self.market_refresh_button.configure(state='disabled')
        self._market_watch_result = None
        minimum_cap, maximum_cap = self._market_cap_bounds()

        def collect() -> None:
            try:
                quotes = PumpPublicSource(max_workers=8).collect(minimum_cap, maximum_cap, 60)
                self._market_watch_result = (True, quotes)
            except (MarketDataError, ValueError, TypeError) as exc:
                self._market_watch_result = (False, str(exc)[:180])

        self._market_watch_thread = threading.Thread(target=collect, name="solanamarket-market-watch", daemon=True)
        self._market_watch_thread.start()
        self._await_market_watch()

    def _await_market_watch(self) -> None:
        if self._closing:
            return
        if self._market_watch_thread and self._market_watch_thread.is_alive():
            self.root.after(200, self._await_market_watch)
            return
        self._market_watch_thread = None
        self.market_refresh_button.configure(state='normal')
        result = self._market_watch_result or (False, "Market scan ended without a result")
        if not result[0]:
            self._market_scan_error = 'Scan unavailable: ' + str(result[1])[:120] + '. Cached samples retained.'
            self._render_market_watch_rows(self.library.market_watch_rows(500))
            self._schedule_market_watch()
            return
        quotes = result[1]
        if not isinstance(quotes, list):
            self._market_scan_error = 'Scan returned invalid data; retaining cached samples.'
            self._render_market_watch_rows(self.library.market_watch_rows(500))
            self._schedule_market_watch()
            return
        for quote in quotes:
            try:
                self.library.record_market_watch_quote(quote)
            except (AttributeError, TypeError, ValueError):
                continue
        self._market_last_success = time.time()
        self._market_scan_error = '' if quotes else 'Last scan returned no quotes; cached samples retained.'
        rows = self.library.market_watch_rows(500)
        self._render_market_watch_rows(rows)
        self._schedule_market_watch()

    def _schedule_market_watch(self) -> None:
        if not self._closing:
            self._market_watch_after = self.root.after(MARKET_WATCH_INTERVAL_MS, self.refresh_market_watch)

    def _render_market_watch_rows(self, rows: list[dict[str, object]]) -> None:
        minimum_cap, maximum_cap = self._market_cap_bounds()
        selected = self.market_watch_tree.selection()
        scroll = self.market_watch_tree.yview()
        visible = filter_market(rows, self.market_query.get(), minimum_cap, maximum_cap,
                                self._minimum_liquidity(), self.market_fresh_only.get(), self.market_sort.get())
        for item in self.market_watch_tree.get_children():
            self.market_watch_tree.delete(item)
        for index, row in enumerate(visible):
            try:
                mint = str(row["mint"])
                market_cap = float(row["market_cap_usd"])
                liquidity = float(row["liquidity_usd"])
                history = list(row["market_cap_history"])
                token = f"{row['symbol']}  ·  {mint[:6]}…{mint[-4:]}"
                self.market_watch_tree.insert("", "end", iid=mint, values=(
                    token, price_text(row['price_usd']), _money(market_cap), _money(liquidity),
                    ('Fresh · ' if is_fresh(row['observed_at']) else 'Stale · ') + age_text(row['observed_at']),
                    self._market_cap_trend(history), _token_age(row['created_at']), self._market_watch_status(mint),
                ), tags=('stale' if not is_fresh(row['observed_at']) else '', 'alternate' if index % 2 else ''))
            except (KeyError, TypeError, ValueError, tk.TclError):
                continue
        if selected and self.market_watch_tree.exists(selected[0]):
            self.market_watch_tree.selection_set(selected[0])
        if scroll:
            self.market_watch_tree.yview_moveto(scroll[0])
        self.coin_details_button.configure(state='normal' if self.market_watch_tree.selection() else 'disabled')
        if visible:
            self.market_empty.grid_remove()
        else:
            self.market_empty.configure(text=('No coins match these filters.\nReset filters or include stale samples.' if rows else
                                              'No market samples yet.\nRefresh market to collect observations.'))
            self.market_empty.grid()
        newest = max((float(r['observed_at']) for r in rows), default=0)
        scan = self._market_scan_error or ('Refreshing…' if self._market_watch_thread else
                                          'Scans every 15s' if self._market_last_success else 'Retained local samples')
        self.market_watch_status.configure(text=f'{scan} · {len(visible)} shown / {len(rows)} retained (up to 500)\nNewest sample: {age_text(newest)}. Each row shows its own age.',
                                          foreground=WARNING if self._market_scan_error else MUTED)

    def _market_cap_bounds(self) -> tuple[float, float]:
        return MARKET_CAP_OPTIONS.get(self.market_cap_filter.get(), MARKET_CAP_OPTIONS["All · $5K–$10M"])

    def _minimum_liquidity(self) -> float:
        return LIQUIDITY_OPTIONS.get(self.liquidity_filter.get(), 0.0)

    def _on_market_filter_changed(self, _event: tk.Event[Any] | None = None) -> None:
        rows = self.library.market_watch_rows(500)
        self._render_market_watch_rows(rows)

    @staticmethod
    def _market_cap_trend(values: list[float]) -> str:
        if len(values) < 2 or values[0] <= 0:
            return "Baseline pending"
        change = (values[-1] / values[0] - 1) * 100
        sign = "+" if change >= 0 else ""
        return f"{sign}{change:.1f}% · {len(values)} samples"

    def _market_watch_status(self, mint: str) -> str:
        watching: list[str] = []
        for instance in self.library.instances():
            try:
                self.library.latest_observation(str(instance["id"]), mint)
            except ValueError:
                continue
            watching.append(str(instance["label"])[:20])
        return "Observed · " + ", ".join(watching[:2]) if watching else "No session samples"

    def _build_wallet_navigation_card(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.columnconfigure(1, weight=1)
        logo = tk.Canvas(card, width=32, height=28, background=PANEL_RAISED, highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky="n")
        logo.create_polygon(3, 5, 25, 5, 30, 9, 8, 9, fill="#8a6cff", outline="")
        logo.create_polygon(3, 12, 25, 12, 30, 16, 8, 16, fill="#4ee5d3", outline="")
        logo.create_polygon(3, 19, 25, 19, 30, 23, 8, 23, fill="#a978ff", outline="")
        ttk.Label(card, text="SOL WALLET", style="MetricCaption.TLabel").grid(row=0, column=1, sticky="w")
        self.wallet_sync_indicator = ttk.Label(card, text="◌", style="MetricCaption.TLabel", foreground=MUTED)
        self.wallet_sync_indicator.grid(row=0, column=2, sticky="e", padx=(9, 0))
        self.wallet_usd_value = ttk.Label(card, text="$—", style="Metric.TLabel")
        self.wallet_usd_value.grid(row=1, column=1, sticky="w", pady=(1, 0))
        self.wallet_sol_value = ttk.Label(card, text="— SOL", style="MetricCaption.TLabel")
        self.wallet_sol_value.grid(row=1, column=2, sticky="e", padx=(9, 0))
        short_address = f"{self.wallet.address[:5]}…{self.wallet.address[-5:]}"
        ttk.Label(card, text=short_address, style="Card.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(card, text="Copy", style="Subtle.TButton", command=self.copy_wallet_address).grid(
            row=2, column=2, sticky="e", padx=(9, 0), pady=(6, 0))
        return card

    def sync_wallet_balance(self) -> None:
        if self._wallet_sync_thread is not None:
            return
        self._wallet_sync_after = None
        self.wallet_sync_indicator.configure(text="◌", foreground=MUTED)
        self._wallet_sync_result = None
        address = self.wallet.address

        def collect() -> None:
            try:
                snapshot = SolanaWalletSource(WALLET_RPC_URL).snapshot(address, set())
                sol = float(snapshot["sol"])
                price = PumpPublicSource().sol_usd_price()
                self._wallet_sync_result = (True, sol, price)
            except (WalletDataError, OwnershipDataError, MarketDataError, ValueError, TypeError) as exc:
                self._wallet_sync_result = (False, str(exc)[:120], None)
            # Recover withdrawal outcomes on every launch/sync without signing or resubmitting.
            journal = TransferJournal(self.data_dir)
            try:
                networks = {row["network"] for row in journal.rows(address)
                            if row["status"] in {"pending", "unknown", "confirmed"}}
                for network in networks:
                    try:
                        WithdrawalService(TransferRpc(network), journal).reconcile(address)
                    except Exception:
                        pass  # Preserve unresolved status for the next read-only reconciliation.
            finally:
                journal.close()

        self._wallet_sync_thread = threading.Thread(target=collect, name="solanamarket-wallet-sync", daemon=True)
        self._wallet_sync_thread.start()
        self._await_wallet_balance()

    def _await_wallet_balance(self) -> None:
        if self._closing:
            return
        if self._wallet_sync_thread and self._wallet_sync_thread.is_alive():
            self.root.after(200, self._await_wallet_balance)
            return
        self._wallet_sync_thread = None
        success, value, price = self._wallet_sync_result or (False, "Sync ended without a result", None)
        if success:
            sol = float(value)
            self.wallet_usd_value.configure(text=_money(sol * float(price)))
            self.wallet_sol_value.configure(text=f"{sol:,.4f} SOL")
            self.wallet_sync_indicator.configure(text="●", foreground=SUCCESS)
        else:
            self.wallet_sync_indicator.configure(text="⚠", foreground=WARNING)
        self._schedule_wallet_sync()

    def _schedule_wallet_sync(self) -> None:
        if not self._closing:
            self._wallet_sync_after = self.root.after(WALLET_SYNC_INTERVAL_MS, self.sync_wallet_balance)

    def open_website(self) -> None:
        try:
            webbrowser.open_new_tab("https://it.pumpwizard.fun")
        except Exception as exc:
            messagebox.showerror("Website unavailable", f"Could not open it.pumpwizard.fun:\n{exc}", parent=self.root)

    def show_patch_notes(self) -> None:
        if self._closing:
            return
        url = note_url(__version__)
        try:
            if not webbrowser.open_new_tab(url):
                raise RuntimeError('No browser accepted the link')
        except Exception:
            messagebox.showerror('Cannot open patch notes', f'Open this address in your browser:\n\n{url}', parent=self.root)

    def copy_wallet_address(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.wallet.address)
        self.root.update()
        messagebox.showinfo("Wallet address copied", "Your Solana deposit address is in the clipboard.", parent=self.root)

    def open_manual_trades(self) -> None:
        if self.trade_panel and self.trade_panel.window.winfo_exists():
            self.trade_panel.window.lift()
            return
        selected = self.market_watch_tree.selection()
        self.trade_panel = TradePanel(self.root, self.data_dir, self.wallet.address, self.refresh,
                                     selected[0] if selected else '')

    def open_agent_trading(self) -> None:
        if not self.selected_agent_id:
            return
        if self.trading_panel and self.trading_panel.window.winfo_exists():
            if self.trading_panel.agent_id == self.selected_agent_id or self.trading_panel.busy:
                self.trading_panel.window.lift()
                return
            if self.trading_panel.execution_worker and self.trading_panel.execution_worker.running:
                self.trading_panel.stop_worker()
                self.root.after(150, self.open_agent_trading)
                return
            self.trading_panel.close()
        self.trading_panel = TradingPanel(self.root, self.data_dir, self.wallet.address,
                                          self.selected_agent_id, self.refresh, self.withdraw_wallet,
                                          self.open_position_manual_exit)

    def open_evaluation(self) -> None:
        if not self.selected_agent_id:
            return
        panel = self.evaluation_panel
        if panel and not panel.closed:
            if panel.instance["id"] == self.selected_agent_id:
                panel.window.lift()
                return
            panel.close()
        self.evaluation_panel = EvaluationPanel(self.root, self.data_dir,
            self.library.instance(self.selected_agent_id), self.open_evaluation_shadow)

    def open_evaluation_shadow(self, agent_id, tab) -> None:
        self._show_agent(agent_id)
        self.open_agent_trading()
        panel = self.trading_panel
        if panel and panel.agent_id == agent_id:
            panel.tabs.select(tab)
            panel.window.lift()

    def setup_test_agent(self) -> None:
        from .trading_workspace import test_agent

        if self.trading_panel and self.trading_panel.busy:
            self.trading_panel.window.lift()
            return
        self.selected_agent_id = test_agent(self.library)
        self.refresh()
        self._show_agent(self.selected_agent_id)
        self.open_agent_trading()

    def open_position_manual_exit(self, position) -> None:
        self.open_manual_trades()
        self.trade_panel.select_position(position['id'])

    def recover_manual_trades(self) -> None:
        if self._closing:
            return
        if self._trade_recovery_thread is None or not self._trade_recovery_thread.is_alive():
            self._trade_recovery_thread = threading.Thread(
                target=reconcile_wallet, args=(self.data_dir, self.wallet.address), daemon=True,
                name='manual-trade-recovery')
            self._trade_recovery_thread.start()
            self._refresh_position_rail(self.library.instances())
        self._trade_recovery_after = self.root.after(15_000, self.recover_manual_trades)

    def withdraw_wallet(self) -> None:
        if self.wallet_panel and self.wallet_panel.window.winfo_exists():
            self.wallet_panel.window.lift()
            return
        self.wallet_panel = WalletPanel(self.root, self.data_dir, self.wallet.address, self.close)

    def _build_settings(self) -> ttk.Frame:
        frame = ttk.Frame(self.workspace, style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        heading = ttk.Frame(frame, style="Panel.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(10, 12))
        ttk.Label(heading, text="Settings", style="Hero.TLabel").pack(side="left")
        ttk.Button(heading, text="Close settings", command=lambda: self.show_section("workspace"),
                   style="Subtle.TButton").pack(side="right")
        ttk.Button(heading, text="Wallet & backup", command=self.withdraw_wallet,
                   style="Subtle.TButton").pack(side="right", padx=8)
        notebook = ttk.Notebook(frame, style="App.TNotebook")
        self.settings_tabs = notebook
        notebook.grid(row=1, column=0, sticky="nsew")
        self.marketplaces = self._build_marketplaces(notebook)
        notebook.add(self.marketplaces, text="Marketplace")
        return frame

    def open_ai_settings(self):
        self.add_ai_agent()

    def _build_marketplaces(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(0, 8, 0, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Marketplaces", style="Hero.TLabel").grid(row=0, column=0, sticky="w", pady=(10, 6))
        ttk.Label(frame, text="Choose the market data and execution adapter your agent will use.", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w")
        pump = ttk.Frame(frame, style="Card.TFrame", padding=18)
        pump.grid(row=2, column=0, sticky="ew", pady=(22, 12))
        pump.columnconfigure(1, weight=1)
        logo = tk.Canvas(pump, width=48, height=48, background=PANEL_RAISED, highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=3, padx=(0, 14))
        # Pump.fun’s capsule mark, rendered locally so the selector does not fetch remote UI assets.
        logo.create_polygon(6, 26, 26, 6, 43, 23, 23, 43, fill="#65d391", outline="#0f392e", width=3)
        logo.create_oval(23, 6, 43, 26, fill="#f5f8f7", outline="")
        logo.create_line(8, 26, 23, 41, fill="#9af2bf", width=2)
        ttk.Label(pump, text="pump.fun", style="Metric.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(pump, text="Solana launchpad · direct public-market monitoring available", style="Card.TLabel").grid(
            row=1, column=1, sticky="w", pady=(3, 0))
        self.marketplace_status = ttk.Label(pump, text="", style="MetricCaption.TLabel")
        self.marketplace_status.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.marketplace_button = ttk.Button(pump, text="Use Pump.fun", style="Accent.TButton",
                                             command=self.select_pumpfun)
        self.marketplace_button.grid(row=0, column=2, rowspan=3, padx=(16, 0), sticky="e")
        future = ttk.Frame(frame, style="Card.TFrame", padding=18)
        future.grid(row=3, column=0, sticky="ew")
        ttk.Label(future, text="MORE MARKETPLACES", style="MetricCaption.TLabel").pack(anchor="w")
        ttk.Label(future, text="Additional Solana launchpads and market adapters will appear here.", style="Card.TLabel").pack(
            anchor="w", pady=(6, 0))
        self._refresh_marketplace()
        return frame

    def _build_ai_agent(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(0, 8, 0, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="AI profile", style="Hero.TLabel").grid(row=0, column=0, sticky="w", pady=(10, 6))
        ttk.Label(frame, text="Connect a provider, choose a model, and chat in the navigation header.", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w")
        controls = ttk.Frame(frame, style="Panel.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(18, 12))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="ACTIVE AGENT", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.agent_choice = ttk.Combobox(controls, style="Agent.TCombobox", state="readonly")
        self.agent_choice.grid(row=0, column=1, sticky="ew")
        self.agent_choice.bind("<<ComboboxSelected>>", self._select_ai_agent)
        ttk.Button(controls, text="Add agent", style="Accent.TButton", command=self.add_ai_agent).grid(
            row=0, column=2, padx=(10, 0))
        ttk.Button(controls, text="Remove", style="Subtle.TButton", command=self.remove_ai_agent).grid(
            row=0, column=3, padx=(7, 0))
        detail = ttk.Frame(frame, style="Card.TFrame", padding=14)
        detail.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(detail, text="CHAT PRIVACY", style="MetricCaption.TLabel").pack(anchor="w")
        ttk.Label(detail, text="Sending shares bounded conversation history with your chosen provider. Strategy and coin data require a separate preview and approval. API keys are session-only or encrypted with a separate AI-key password. Wallet signing material is never available to chat.",
                  style="Card.TLabel", wraplength=650).pack(anchor="w", pady=(6, 0))
        self.ai_key_status = ttk.Label(detail, style='Card.TLabel')
        self.ai_key_status.pack(anchor='w', pady=12)
        ttk.Button(detail, text='Connect provider', style='Accent.TButton', command=self.add_ai_agent).pack(anchor='w')
        self._refresh_ai_agent_controls()
        return frame

    def show_section(self, section: str) -> None:
        if section not in {"workspace", "settings", "strategies"}:
            return
        self.active_section = section
        for view in (self.welcome, self.agent_workspace, self.settings, self.strategy_browser):
            view.grid_remove()
        if section == "workspace":
            (self.agent_workspace if self.selected_agent_id else self.welcome).grid(row=0, column=0, sticky="nsew")
        elif section == "strategies":
            self.selected_agent_id = None
            self.agent_tree.selection_remove(self.agent_tree.selection())
            self.strategy_browser.grid(row=0, column=0, sticky="nsew")
            self.strategy_browser.render_rows()
        else:
            self.settings.grid(row=0, column=0, sticky="nsew")
            self._refresh_marketplace()
            self._refresh_ai_agent_controls()
        for key, button in self.nav_buttons.items():
            button.configure(style="Accent.TButton" if key == section else "Subtle.TButton")

    def select_pumpfun(self) -> None:
        self.agent_settings = AgentSettings(marketplace="pump.fun", active_agent_id=self.agent_settings.active_agent_id,
                                            agents=self.agent_settings.agents)
        try:
            self.agent_hub.save(self.agent_settings)
        except AgentHubError as exc:
            messagebox.showerror("Cannot save marketplace", str(exc), parent=self.root)
            return
        self._refresh_marketplace()

    def _refresh_marketplace(self) -> None:
        selected = self.agent_settings.marketplace == "pump.fun"
        self.marketplace_status.configure(text="SELECTED · local Pump market collector enabled" if selected else "AVAILABLE")
        self.marketplace_button.configure(text="Selected" if selected else "Use Pump.fun", style="Subtle.TButton" if selected else "Accent.TButton")

    def _refresh_ai_agent_controls(self) -> None:
        if not hasattr(self, 'agent_choice'):
            if hasattr(self, 'assistant'):
                self.assistant.sync_profile()
            return
        labels = [agent.label for agent in self.agent_settings.agents]
        self.agent_choice.configure(values=labels)
        active = next((agent for agent in self.agent_settings.agents if agent.id == self.agent_settings.active_agent_id), None)
        self.agent_choice.set(active.label if active else "No agent selected")
        if hasattr(self, "assistant"):
            self.assistant.sync_profile()
        if hasattr(self, "ai_key_status"):
            state = ("Key available for this session" if active and self.ai_keys.available(active) else
                     "Encrypted key saved · unlock on first message" if active and self.ai_keys.exists(active) else
                     "Environment variable: " + active.api_key_env if active and active.api_key_env else
                     "No API key entered · local servers may not require one")
            self.ai_key_status.configure(text=state)

    def active_ai_profile(self):
        return next((agent for agent in self.agent_settings.agents if agent.id == self.agent_settings.active_agent_id), None)

    def add_ai_agent(self) -> None:
        def saved(settings):
            self.agent_settings = settings
            self._refresh_ai_agent_controls()
        add_profile(self.root, self.agent_hub, self.agent_settings, self.ai_keys, saved)

    def remove_ai_agent(self) -> None:
        active = next((agent for agent in self.agent_settings.agents if agent.id == self.agent_settings.active_agent_id), None)
        if active is None:
            return
        if not messagebox.askyesno("Remove AI agent", f"Remove {active.label} from this device?", parent=self.root):
            return
        agents = tuple(agent for agent in self.agent_settings.agents if agent.id != active.id)
        settings = AgentSettings(marketplace=self.agent_settings.marketplace,
                                 active_agent_id=agents[0].id if agents else None, agents=agents)
        try:
            self.agent_hub.save(settings)
        except AgentHubError as exc:
            messagebox.showerror("Cannot remove AI agent", str(exc), parent=self.root)
            return
        self.agent_settings = settings
        self.ai_keys.forget(active)
        self._refresh_ai_agent_controls()

    def _select_ai_agent(self, _event: tk.Event[Any]) -> None:
        label = self.agent_choice.get()
        selected = next((agent for agent in self.agent_settings.agents if agent.label == label), None)
        if selected is None:
            return
        settings = AgentSettings(marketplace=self.agent_settings.marketplace, active_agent_id=selected.id,
                                 agents=self.agent_settings.agents)
        try:
            self.agent_hub.save(settings)
        except AgentHubError as exc:
            messagebox.showerror("Cannot select AI agent", str(exc), parent=self.root)
            return
        self.agent_settings = settings
        self._refresh_ai_agent_controls()

    def _build_agent_workspace(self) -> ttk.Frame:
        frame = ttk.Frame(self.workspace, style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        top = ttk.Frame(frame, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.agent_title = ttk.Label(top, text="", style="Hero.TLabel")
        self.agent_title.grid(row=0, column=0, sticky="w")
        self.agent_subtitle = ttk.Label(top, text="", style="Muted.TLabel")
        self.agent_subtitle.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.tracking_status = ttk.Label(top, text="", style="Muted.TLabel")
        self.tracking_status.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        top.bind('<Configure>', lambda e: [label.configure(wraplength=max(250, e.width-190))
                                          for label in (self.agent_title, self.agent_subtitle, self.tracking_status)])
        self.track_button = ttk.Button(top, text="Start live tracking", style="Accent.TButton", command=self.toggle_tracking)
        self.track_button.grid(row=0, column=1, rowspan=3, sticky="e")
        ttk.Button(top, text='Agent trading', style='Subtle.TButton', command=self.open_agent_trading).grid(
            row=3, column=1, sticky='e', pady=(8,0))
        session_actions = ttk.Frame(top, style='Panel.TFrame')
        session_actions.grid(row=3, column=0, sticky='w', pady=(8, 0))
        self.recovery_button = ttk.Button(session_actions, text='View activity', style='Subtle.TButton', command=self.session_recovery)
        self.recovery_button.pack(side='left')
        ttk.Button(session_actions, text='Local evaluation', style='Subtle.TButton', command=self.open_evaluation).pack(side='left', padx=8)
        metrics = ttk.Frame(frame, style="Panel.TFrame")
        metrics.grid(row=1, column=0, sticky="ew", pady=(18, 18))
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        self.metric_values: dict[str, ttk.Label] = {}
        for column, (key, label) in enumerate((
            ("positions", "RECORDED POSITIONS"), ("marked", "RECORDED MARK"),
            ("health", "MONITOR HEALTH"), ("orders", "DRY-RUN ORDERS"),
        )):
            card = ttk.Frame(metrics, style="Card.TFrame", padding=14)
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 6 if column < 3 else 0))
            ttk.Label(card, text=label, style="MetricCaption.TLabel").pack(anchor="w")
            value = ttk.Label(card, text="—", style="Metric.TLabel")
            value.pack(anchor="w", pady=(6, 0))
            self.metric_values[key] = value
        self.notebook = ttk.Notebook(frame, style="App.TNotebook")
        self.notebook.grid(row=3, column=0, sticky="nsew")
        self.positions_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=(0, 15, 0, 0))
        self.activity_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=(0, 15, 0, 0))
        self.orders_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=(0, 15, 0, 0))
        self.notebook.add(self.positions_tab, text="Recorded positions")
        self.notebook.add(self.activity_tab, text="Activity")
        self.notebook.add(self.orders_tab, text="Practice orders")
        support_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        support_tab.columnconfigure(0, weight=1)
        support_tab.rowconfigure(0, weight=1)
        self.notebook.add(support_tab, text="Compatibility & evidence")
        self.agent_support = StrategyBrowser.textbox(self, support_tab, 0)
        self._build_positions_tab()
        self._build_activity_tab()
        self._build_orders_tab()
        return frame

    def _build_positions_tab(self) -> None:
        self.positions_tab.columnconfigure(0, weight=1)
        self.positions_tab.rowconfigure(0, weight=1)
        columns = ("token", "quantity", "cost", "latest_mark", "route", "tracking")
        self.position_tree = ttk.Treeview(self.positions_tab, style="App.Treeview", columns=columns,
                                          show="headings", selectmode="browse")
        headings = {"token": "TOKEN", "quantity": "REMAINING", "cost": "COST", "latest_mark": "LATEST MARK",
                    "route": "ROUTE", "tracking": "TRACKED"}
        widths = {"token": 150, "quantity": 115, "cost": 105, "latest_mark": 125, "route": 105, "tracking": 115}
        for column in columns:
            self.position_tree.heading(column, text=headings[column])
            self.position_tree.column(column, width=widths[column], anchor="w")
        self.position_tree.grid(row=0, column=0, sticky="nsew")
        self.position_tree.bind("<<TreeviewSelect>>", self._on_position_selected)
        buttons = ttk.Frame(self.positions_tab, style="Panel.TFrame")
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Refresh marks", style="Subtle.TButton", command=self.refresh).pack(side="left")
        ttk.Button(buttons, text="Record external close…", style="Danger.TButton", command=self.record_external_close).pack(side="right")

    def _build_activity_tab(self) -> None:
        self.activity_tab.columnconfigure(0, weight=1)
        self.activity_tab.rowconfigure(1, weight=1)
        self.health_label = ttk.Label(self.activity_tab, text="", style="Muted.TLabel")
        self.health_label.grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.activity_tree = ttk.Treeview(self.activity_tab, style="App.Treeview", columns=("time", "type", "token", "reason"), show="headings")
        for column, label, width in (("time", "TIME", 110), ("type", "EVENT", 190), ("token", "TOKEN", 130), ("reason", "DETAIL", 460)):
            self.activity_tree.heading(column, text=label)
            self.activity_tree.column(column, width=width, anchor="w")
        self.activity_tree.grid(row=1, column=0, sticky="nsew")

    def _build_orders_tab(self) -> None:
        self.orders_tab.columnconfigure(0, weight=1)
        self.orders_tab.rowconfigure(0, weight=1)
        self.orders_tree = ttk.Treeview(self.orders_tab, style="App.Treeview", columns=("time", "side", "token", "status", "limit"), show="headings")
        for column, label, width in (("time", "CREATED", 120), ("side", "SIDE", 95), ("token", "TOKEN", 180), ("status", "STATUS", 130), ("limit", "LIMIT", 190)):
            self.orders_tree.heading(column, text=label)
            self.orders_tree.column(column, width=width, anchor="w")
        self.orders_tree.grid(row=0, column=0, sticky="nsew")

    def open_strategies(self):
        existing = getattr(self, 'strategy_chooser', None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        self.strategy_chooser = show_strategy_chooser(
            self.root, lambda: self.show_section('strategies'), self.import_file, self.create_strategy)

    def create_strategy(self):
        self.strategy_creator = show_strategy_creator(self.root, self.review_strategy_package)

    def import_file(self) -> None:
        filename = filedialog.askopenfilename(title="Import strategy configuration", filetypes=[("JSON", "*.json"), ("All files", "*")])
        if not filename:
            return
        try:
            package = Path(filename).read_text(encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Cannot import configuration", str(exc), parent=self.root)
            return
        self.review_strategy_package(package)

    def review_strategy_package(self, package) -> None:
        try:
            imported = parse_package(package)
            comparison = import_comparison(self.library, imported)
            self.library.import_strategy(imported)
        except (OSError, StrategyImportError) as exc:
            messagebox.showerror("Cannot import configuration", str(exc), parent=self.root)
            return
        self.refresh()
        def open_imported(agent_id):
            self.refresh()
            self._show_agent(agent_id)

        show_import(self.root, self.library, imported, comparison, open_imported)

    def refresh(self) -> None:
        previous = self.selected_agent_id
        try:
            self._wallet_position_cache = wallet_positions(self.data_dir, self.wallet.address)
        except sqlite3.Error:
            pass  # Keep the last known positions during a transient database lock.
        instances = self.library.instances()
        instance_map = {str(row["id"]): row for row in instances}
        self.refresh_session_list(instances)
        self._refresh_position_rail(instances)
        if previous in instance_map:
            self.selected_agent_id = previous
            if self.agent_tree.selection() != (previous,):
                self.agent_tree.selection_set(previous)
            self.agent_tree.focus(previous)
            if self.active_section == "workspace":
                self._show_agent(previous)
        elif self.selected_agent_id in instance_map:
            if self.active_section == "workspace":
                self._show_agent(self.selected_agent_id)
        else:
            self.agent_tree.selection_remove(self.agent_tree.selection())
            self.agent_tree.focus("")
            self.selected_agent_id = None
            self.selected_position_id = None
            if self.active_section == "workspace":
                self._show_welcome()

    def session_presentation(self, agent_id):
        held = len(self.library.positions(agent_id, include_closed=False)) + sum(
            p['agent_id'] == agent_id for p in self._wallet_position_cache)
        return session_state(self.library.instance(agent_id), self.library.tracking_settings(agent_id),
                             self.library.monitor_health(agent_id), held)

    def refresh_session_list(self, instances):
        instance_map = {str(row['id']): row for row in instances}
        monitoring, attention = 0, 0
        for item in self.agent_tree.get_children():
            if item not in instance_map:
                self.agent_tree.delete(item)
        for row in instances:
            state = self.session_presentation(str(row['id']))
            monitoring += state['label'] == 'Monitoring'
            attention += state['tone'] == 'warning'
            values = (str(row["label"]), state['label'])
            if self.agent_tree.exists(str(row["id"])):
                self.agent_tree.item(str(row["id"]), values=values, tags=(state['tone'],))
            else:
                self.agent_tree.insert("", "end", iid=str(row["id"]), values=values, tags=(state['tone'],))
        self.session_summary.configure(text=f'{len(instances)} sessions · {monitoring} monitoring\n{attention} need attention' +
                                       ('\n' + self._scheduler_warning if self._scheduler_warning else ''),
                                       foreground=WARNING if self._scheduler_warning else MUTED)
        if instances:
            self.session_empty.grid_remove()
        else:
            self.session_empty.grid()

    def _refresh_position_rail(self, instances: list[dict[str, object]]) -> None:
        for item in self.position_rail.get_children():
            self.position_rail.delete(item)
        for instance in instances:
            agent_id = str(instance["id"])
            for position in self.library.positions(agent_id, include_closed=False):
                token = f"{position['symbol']}  {_quantity(position['quantity'])}"
                self.position_rail.insert("", "end", iid="position:" + str(position["id"]), values=(token, str(instance["label"])[:16]), tags=(agent_id,))

        try:
            self._wallet_position_cache = wallet_positions(self.data_dir, self.wallet.address)
        except sqlite3.Error:
            pass
        names = {str(r['id']):str(r['label']) for r in instances}
        for position in self._wallet_position_cache:
            token = f"{position['mint'][:8]}… {token_text(position['quantity'])}"
            owner = position['agent_id']
            self.position_rail.insert('', 'end', iid='manual:'+position['id'],
                                      values=(token, names.get(owner,'Manual')),
                                      tags=('owned',owner) if owner else ('manual',))

    def _show_welcome(self) -> None:
        self.selected_agent_id = None
        self.agent_tree.selection_remove(self.agent_tree.selection())
        self.show_section("workspace")
        self.tracking_status.configure(text="")

    def _show_agent(self, agent_id: str) -> None:
        try:
            instance = self.library.instance(agent_id)
        except ValueError:
            self._show_welcome()
            return
        self.selected_agent_id = agent_id
        self.show_section("workspace")
        strategy = parse_package(instance["package"])
        StrategyBrowser.write(self.agent_support, support_text(strategy))
        self.agent_title.configure(text=str(instance["label"]))
        self.agent_subtitle.configure(text=f"{support_label(strategy)} · Entry {instance['entry_mode']} · Exit {instance['exit_mode']} · Automatic execution disabled")
        self.update_session_status(agent_id)
        self._refresh_agent_content(agent_id)

    def update_session_status(self, agent_id):
        instance = self.library.instance(agent_id)
        state = self.session_presentation(agent_id)
        settings = self.library.tracking_settings(agent_id)
        enabled = bool(settings["enabled"] and instance["status"] == "monitoring")
        self.track_button.configure(text="Retry monitoring" if settings["fault"] else "Pause entries" if enabled else "Start tracking")
        decisions = self.library.decisions(agent_id, 1)
        latest = ('\nLatest decision ' + age_text(decisions[0]['at']) + ': ' + decisions[0]['kind'].replace('_', ' ') + ' · ' + decisions[0]['reason'][:160]) if decisions else '\nNo recorded decisions yet.'
        self.tracking_status.configure(text=state['label'] + ' · ' + state['reason'][:300] + latest,
                                      foreground=WARNING if state['tone'] == 'warning' else SUCCESS if state['tone'] == 'success' else MUTED)
        if self._scheduler_warning:
            self.tracking_status.configure(text=self._scheduler_warning + '\n' + state['reason'][:300] + latest,
                                           foreground=WARNING)
        self.recovery_button.configure(text=state['action'])

    def session_recovery(self):
        if self.selected_agent_id:
            self.notebook.select(3 if self.session_presentation(self.selected_agent_id)['action'] == 'Review setup' else self.activity_tab)

    def _refresh_agent_content(self, agent_id: str) -> None:
        positions = self.library.positions(agent_id, include_closed=False)
        health = self.library.monitor_health(agent_id)
        orders = self.library.orders(agent_id, 100)
        latest_total = 0.0
        marks: dict[str, tuple[float | None, str, str]] = {}
        for position in positions:
            try:
                quote = self.library.latest_observation(agent_id, str(position["mint"]))
                price = float(quote["price_usd"])
                latest_total += float(position["quantity"]) * price
                marks[str(position["id"])] = (price, str(quote.get("route", "—")), _relative_time(quote.get("observed_at")))
            except (ValueError, TypeError, KeyError):
                marks[str(position["id"])] = (None, "—", "not yet")
        self.metric_values["positions"].configure(text=str(len(positions)))
        self.metric_values["marked"].configure(text=_money(latest_total) if positions and latest_total else "—")
        last_run = health.get("last_run")
        self.metric_values["health"].configure(text="Never" if last_run == "never" else str(last_run.get("status", "unknown")).title())
        self.metric_values["orders"].configure(text=str(len(orders)))
        for item in self.position_tree.get_children():
            self.position_tree.delete(item)
        position_ids = {str(row["id"]) for row in positions}
        if self.selected_position_id not in position_ids:
            self.selected_position_id = None
        for position in positions:
            position_id = str(position["id"])
            price, route, tracked = marks[position_id]
            mark = _money(float(position["quantity"]) * price) if price is not None else "—"
            self.position_tree.insert("", "end", iid=position_id, values=(position["symbol"], _quantity(position["quantity"]), _money(position["remaining_cost_usd"]), mark, route, tracked))
        if self.selected_position_id:
            self.position_tree.selection_set(self.selected_position_id)
            self.position_tree.focus(self.selected_position_id)
        if last_run == "never":
            health_description = "No completed market collection yet. Start live tracking to collect independent observations."
        else:
            health_description = (f"Last monitor run: {last_run['status']} · {_relative_time(last_run['finished_at'])} · {last_run['observations']} observations · {last_run['decisions']} decisions")
            if last_run.get("detail"):
                health_description += f" · {last_run['detail']}"
        self.health_label.configure(text=health_description)
        for item in self.activity_tree.get_children():
            self.activity_tree.delete(item)
        for decision in self.library.decisions(agent_id, 100):
            self.activity_tree.insert("", "end", values=(_relative_time(decision["at"]), str(decision["kind"]).replace("_", " "), decision["mint"][:14], decision["reason"]))
        for item in self.orders_tree.get_children():
            self.orders_tree.delete(item)
        for order in orders:
            limit = order["max_input_usd"] or order["minimum_output_amount"] or "—"
            self.orders_tree.insert("", "end", values=(_relative_time(order["created_at"]), str(order["side"]).upper(), order["symbol"], str(order["status"]).replace("_", " "), _money(limit) if limit != "—" else limit))

    def _on_agent_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.agent_tree.selection()
        if selection:
            self.selected_position_id = None
            self._show_agent(selection[0])

    def _on_rail_position_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.position_rail.selection()
        if not selection:
            return
        tags = self.position_rail.item(selection[0]).get("tags", ())
        if tags and tags[0] == 'owned':
            self._show_agent(tags[1])
            self.open_agent_trading()
            return
        if tags and tags[0] == 'manual':
            self.open_manual_trades()
            return
        if tags:
            self.selected_position_id = selection[0].removeprefix("position:")
            self.agent_tree.selection_set(tags[0])
            self.agent_tree.focus(tags[0])
            self._show_agent(tags[0])

    def _on_position_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.position_tree.selection()
        self.selected_position_id = selection[0] if selection else None

    def record_external_close(self) -> None:
        if not self.selected_agent_id or not self.selected_position_id:
            messagebox.showinfo("Select a position", "Choose an open position before recording an external close.", parent=self.root)
            return
        position = next((item for item in self.library.positions(self.selected_agent_id, include_closed=False) if item["id"] == self.selected_position_id), None)
        if position is None:
            messagebox.showerror("Position unavailable", "The selected position is no longer open.", parent=self.root)
            self.refresh()
            return
        quantity = simpledialog.askfloat("Record external close", f"Quantity closed for {position['symbol']}:", initialvalue=float(position["quantity"]), minvalue=0.000000001, parent=self.root)
        if quantity is None:
            return
        proceeds = simpledialog.askfloat("Record external close", "Confirmed proceeds in USD:", minvalue=0.0, parent=self.root)
        if proceeds is None:
            return
        if not messagebox.askyesno("Confirm external fill", "Record this confirmed external fill in the local ledger?\n\nThis button does not submit an on-chain transaction.", parent=self.root):
            return
        try:
            self.library.record_position_exit(self.selected_position_id, quantity=quantity, proceeds_usd=proceeds, exited_at=time.time())
        except ValueError as exc:
            messagebox.showerror("Cannot record close", str(exc), parent=self.root)
            return
        self.selected_position_id = None
        self.refresh()

    def toggle_tracking(self) -> None:
        if not self.selected_agent_id:
            return
        if self.trading_panel and self.trading_panel.busy:
            self.trading_panel.window.lift()
            return
        settings = self.library.tracking_settings(self.selected_agent_id)
        enabled = settings["enabled"] and self.library.instance(self.selected_agent_id)["status"] == "monitoring"
        self.library.set_tracking(self.selected_agent_id, bool(settings["fault"]) or not enabled)
        self.scheduler.wake()
        self.refresh()

    def _tracking_state(self, agent_id: str) -> str:
        return self.session_presentation(agent_id)['label']

    def _poll_scheduler(self) -> None:
        if self._closing:
            return
        changed, warning = False, None
        while True:
            try:
                ident, detail = self.scheduler.events.get_nowait()
                changed = True
                if ident is None:
                    warning = detail
            except queue.Empty:
                break
        if warning:
            self._scheduler_warning = warning + (' Restart the app to retry.' if warning.startswith('Scheduler stopped:') else '')
        elif changed:
            self._scheduler_warning = ''
        if changed:
            self.refresh()
        if time.time() - self._ui_refreshed_at >= 5:
            self._ui_refreshed_at = time.time()
            self.refresh_session_list(self.library.instances())
            if self.active_section == 'workspace' and not self.selected_agent_id:
                self._render_market_watch_rows(self.library.market_watch_rows(500))
            elif self.active_section == 'workspace' and self.selected_agent_id:
                self.update_session_status(self.selected_agent_id)
        self._scheduler_after = self.root.after(500, self._poll_scheduler)

    def close(self) -> None:
        worker = getattr(self.trading_panel, 'execution_worker', None)
        if worker:
            worker.stop()
            if worker.running:
                if not getattr(self, '_worker_close_after', None):
                    def retry():
                        self._worker_close_after = None
                        self.close()
                    self._worker_close_after = self.root.after(100, retry)
                return
        if any(panel and (panel.busy or getattr(panel, 'prompting', False))
               for panel in (self.trade_panel, self.wallet_panel, self.trading_panel)):
            messagebox.showinfo('Wallet operation active', 'Wait for the current wallet operation to finish.', parent=self.root)
            return
        self._closing = True
        if self.coin_panel and not self.coin_panel.closed:
            self.coin_panel.close()
        self.assistant.close()
        if self.evaluation_panel and not self.evaluation_panel.closed:
            self.evaluation_panel.close()
        if self.trading_panel:
            self.trading_panel.sessions.lock()
        if self._trade_recovery_after:
            self.root.after_cancel(self._trade_recovery_after)
        if self._market_watch_after:
            self.root.after_cancel(self._market_watch_after)
            self._market_watch_after = None
        if self._wallet_sync_after:
            self.root.after_cancel(self._wallet_sync_after)
            self._wallet_sync_after = None
        if self._scheduler_after:
            self.root.after_cancel(self._scheduler_after)
        self.scheduler.stop()
        self.library.close()
        self.wallet = None
        self.root.destroy()


def run_desktop(data_dir: Path) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError("Desktop UI is unavailable. Use the solanamarket-wizard CLI commands instead.") from exc
    try:
        RunnerWindow(root, data_dir)
    except WalletVaultError:
        root.destroy()
        return 0
    root.mainloop()
    return 0
