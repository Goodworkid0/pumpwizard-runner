"""Create a local paper-only strategy through the existing handoff format."""

from __future__ import annotations

import copy
import re
import tkinter as tk
from tkinter import ttk

from .portfolio import CONFIG
from .strategies import StrategyImportError, parse_package


_IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
_ENTRY_STYLES = ('momentum', 'breakout', 'pullback_reclaim', 'slow_burn', 'persistent',
                 'higher-low', 'multi-window', 'squeeze', 'retest', 'post-grad',
                 'liquidity', 'relative', 'second-wave', 'recovery')

# These are creation-time conveniences, not additions to the strategy exchange
# format.  Each goal resolves to existing early-ladder fields before the package
# is validated and imported.
_STRATEGY_GOALS = {
    'custom': {},
    'liquidity-first': {
        'entry_style': 'liquidity', 'observation_seconds': 900,
        'minimum_samples': 8, 'minimum_price_changes': 4,
        'minimum_growth_fraction': .015, 'minimum_reserve_growth_usd': 100,
        'maximum_peak_drawdown': .15, 'max_positions': 6,
        'stop_loss_fraction': -.08, 'trailing_drawdown_fraction': .08,
        'quote_gap_exit_seconds': 600, 'max_hold_seconds': 259_200,
        'min_curve_liquidity_usd': 25_000, 'min_amm_liquidity_usd': 100_000,
        'target_multiples': ((1.15, .4), (1.35, .3), (1.7, .2)),
    },
    'swing-trader': {
        'entry_style': 'higher-low', 'observation_seconds': 1_800,
        'minimum_samples': 8, 'minimum_price_changes': 5,
        'minimum_growth_fraction': .025, 'minimum_reserve_growth_usd': 75,
        'maximum_peak_drawdown': .22, 'max_positions': 4,
        'stop_loss_fraction': -.15, 'trailing_drawdown_fraction': .12,
        'quote_gap_exit_seconds': 900, 'max_hold_seconds': 259_200,
        'min_curve_liquidity_usd': 10_000, 'min_amm_liquidity_usd': 30_000,
        'target_multiples': ((1.3, .4), (1.8, .3), (2.8, .2)),
    },
    'squeeze-watch': {
        'entry_style': 'squeeze', 'observation_seconds': 300,
        'minimum_samples': 6, 'minimum_price_changes': 3,
        'minimum_growth_fraction': .02, 'minimum_reserve_growth_usd': 25,
        'maximum_peak_drawdown': .20, 'max_positions': 5,
        'stop_loss_fraction': -.18, 'trailing_drawdown_fraction': .15,
        'quote_gap_exit_seconds': 300, 'max_hold_seconds': 21_600,
        'min_curve_liquidity_usd': 2_500, 'min_amm_liquidity_usd': 15_000,
        'target_multiples': ((1.15, .4), (1.5, .3), (2.2, .2)),
    },
    'day-trader': {
        'entry_style': 'momentum', 'observation_seconds': 60,
        'minimum_samples': 5, 'minimum_price_changes': 3,
        'minimum_growth_fraction': .0125, 'minimum_reserve_growth_usd': 25,
        'maximum_peak_drawdown': .25, 'max_positions': 8,
        'stop_loss_fraction': -.15, 'trailing_drawdown_fraction': .12,
        'quote_gap_exit_seconds': 300, 'max_hold_seconds': 14_400,
        'min_curve_liquidity_usd': 2_500, 'min_amm_liquidity_usd': 15_000,
        'target_multiples': ((1.12, .4), (1.3, .3), (1.7, .2)),
    },
    'long-hold': {
        'entry_style': 'slow_burn', 'observation_seconds': 1_800,
        'minimum_samples': 8, 'minimum_price_changes': 5,
        'minimum_growth_fraction': .03, 'minimum_reserve_growth_usd': 100,
        'maximum_peak_drawdown': .30, 'max_positions': 4,
        'stop_loss_fraction': -.25, 'trailing_drawdown_fraction': .22,
        'quote_gap_exit_seconds': 1_800, 'max_hold_seconds': 604_800,
        'min_curve_liquidity_usd': 10_000, 'min_amm_liquidity_usd': 30_000,
        'target_multiples': ((1.5, .4), (2.5, .3), (5.0, .2)),
    },
    'unicorn-hunter': {
        'entry_style': 'persistent', 'observation_seconds': 900,
        'minimum_samples': 8, 'minimum_price_changes': 5,
        'minimum_growth_fraction': .05, 'minimum_reserve_growth_usd': 50,
        'maximum_peak_drawdown': .25, 'max_positions': 5,
        'stop_loss_fraction': -.30, 'trailing_drawdown_fraction': .25,
        'quote_gap_exit_seconds': 900, 'max_hold_seconds': 864_000,
        'min_curve_liquidity_usd': 2_500, 'min_amm_liquidity_usd': 20_000,
        'target_multiples': ((2.0, .4), (5.0, .3), (10.0, .2)),
    },
}


def draft_package(label, identifier, entry_style, entry_cap_min, entry_cap_max, order_usd, budget_usd,
                  strategy_goal='custom'):
    """Create a valid v1 package without widening the shared strategy contract."""
    if strategy_goal not in _STRATEGY_GOALS:
        raise ValueError('Choose a supported strategy goal.')
    if entry_style not in _ENTRY_STYLES:
        raise ValueError('Choose a supported entry style.')
    if not 0 < entry_cap_min < entry_cap_max:
        raise ValueError('Market-cap minimum must be positive and lower than the maximum.')
    if order_usd <= 0 or budget_usd < order_usd:
        raise ValueError('Paper budget must be at least the positive paper order size.')
    configuration = copy.deepcopy(CONFIG)
    goal = copy.deepcopy(_STRATEGY_GOALS[strategy_goal])
    target_multiples = goal.pop('target_multiples', ((1.2, .4), (2.4, .3), (4.0, .2)))
    configuration.update(goal)
    configuration.update(entry_style=entry_style, entry_cap_min=entry_cap_min,
                         entry_cap_max=entry_cap_max, order_usd=order_usd, budget_usd=budget_usd)
    configuration['targets'] = [
        dict(market_cap_usd=entry_cap_max * multiple, initial_fraction=fraction)
        for multiple, fraction in target_multiples
    ]
    return {'schema': 'pumpwizard.strategy-handoff/v1',
            'strategy': {'id': identifier, 'label': label}, 'configuration': configuration}


def show_strategy_creator(root, on_review):
    window = tk.Toplevel(root)
    window.title('Create strategy · PumpWizard Runner')
    window.configure(background='#34415f')
    window.transient(root)
    window.resizable(False, False)
    body = ttk.Frame(window, style='Panel.TFrame', padding=24)
    body.pack(fill='both', expand=True, padx=1, pady=1)
    ttk.Label(body, text='CREATE A LOCAL DRAFT', style='PanelAccent.TLabel',
              font=('TkDefaultFont', 9, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(body, text='Create your own strategy', style='Hero.TLabel').grid(
        row=1, column=0, columnspan=2, sticky='w', pady=(6, 8))
    ttk.Label(body, text=('Start with the existing paper-only local template. You can review the exact '
                          'configuration before creating a session.'), style='Muted.TLabel', wraplength=590).grid(
        row=2, column=0, columnspan=2, sticky='w', pady=(0, 18))
    body.columnconfigure(1, weight=1)
    label = tk.StringVar(value='My research strategy')
    identifier = tk.StringVar(value='my-research-strategy')
    entry_style = tk.StringVar(value='momentum')
    minimum = tk.StringVar(value='10000')
    maximum = tk.StringVar(value='25000')
    order = tk.StringVar(value='10')
    budget = tk.StringVar(value='500')
    fields = (('Strategy name', label, None), ('Identifier', identifier, None),
              ('Entry style', entry_style, _ENTRY_STYLES), ('Minimum market cap (USD)', minimum, None),
              ('Maximum market cap (USD)', maximum, None), ('Paper order size (USD)', order, None),
              ('Paper budget (USD)', budget, None))
    for row, (caption, variable, choices) in enumerate(fields, start=3):
        ttk.Label(body, text=caption, style='Muted.TLabel').grid(row=row, column=0, sticky='w', padx=(0, 14), pady=4)
        if choices:
            control = ttk.Combobox(body, textvariable=variable, values=choices, state='readonly',
                                   style='Agent.TCombobox')
        else:
            control = ttk.Entry(body, textvariable=variable)
        control.grid(row=row, column=1, sticky='ew', pady=4)
    status = ttk.Label(body, text='This creates a local research configuration. It does not authorize trading.',
                       style='Muted.TLabel', wraplength=590)
    status.grid(row=10, column=0, columnspan=2, sticky='w', pady=(16, 0))
    actions = ttk.Frame(body, style='Panel.TFrame')
    actions.grid(row=11, column=0, columnspan=2, sticky='ew', pady=(16, 0))

    def create():
        name, ident = label.get().strip(), identifier.get().strip()
        if not name or not _IDENTIFIER.fullmatch(ident):
            status.configure(text='Enter a name and a lowercase identifier using letters, numbers and hyphens.', foreground='#ffb547')
            return
        try:
            package = draft_package(name, ident, entry_style.get(), *(float(variable.get()) for variable in
                (minimum, maximum, order, budget)))
            parse_package(package)
        except (ValueError, StrategyImportError) as exc:
            status.configure(text='Check the values: ' + str(exc), foreground='#ffb547')
            return
        window.destroy()
        on_review(package)

    ttk.Button(actions, text='Cancel', style='Subtle.TButton', command=window.destroy).pack(side='right')
    ttk.Button(actions, text='Review configuration', style='Accent.TButton', command=create).pack(side='right', padx=8)
    window.protocol('WM_DELETE_WINDOW', window.destroy)
    window.bind('<Escape>', lambda _: window.destroy())
    window.update_idletasks()
    x = max(0, root.winfo_rootx() + (root.winfo_width() - window.winfo_reqwidth()) // 2)
    y = max(0, root.winfo_rooty() + (root.winfo_height() - window.winfo_reqheight()) // 2)
    window.geometry(f'+{x}+{y}')
    return window
