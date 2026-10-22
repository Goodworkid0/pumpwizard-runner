"""Coin evidence from local snapshots and monitor decisions; no trading controls."""
import datetime as dt
import tkinter as tk
from tkinter import ttk

from .research_views import age_text, is_fresh, number, price_text


def utc(value):
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'Unknown timestamp'


class CoinPanel:
    def __init__(self, root, library, mint, ask, open_session):
        self.library, self.mint, self.ask, self.open_session = library, mint, ask, open_session
        self.closed, self.timer, self.samples, self.events = False, None, [], []
        self.window = tk.Toplevel(root)
        self.window.title('Coin details · PumpWizard Runner')
        x, y = max(0, root.winfo_rootx() + (root.winfo_width()-940)//2), max(0, root.winfo_rooty() + (root.winfo_height()-660)//2)
        self.window.geometry(f'940x660+{x}+{y}')
        self.window.transient(root)
        self.window.minsize(740, 560)
        self.window.configure(background='#121a2d')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        body = ttk.Frame(self.window, style='Panel.TFrame', padding=20)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Frame(body, style='Panel.TFrame')
        heading.grid(row=0, column=0, sticky='ew')
        heading.columnconfigure(0, weight=1)
        self.title = ttk.Label(heading, style='Hero.TLabel', wraplength=400)
        self.title.grid(row=0, column=0, sticky='w')
        ttk.Button(heading, text='Copy mint', style='Subtle.TButton', command=self.copy_mint).grid(row=0, column=1, padx=8)
        ttk.Button(heading, text='Ask Assistant…', style='Accent.TButton', command=self.ask_assistant).grid(row=0, column=2)
        heading.bind('<Configure>', lambda e: self.title.configure(wraplength=max(180, e.width-300)))
        ttk.Label(body, text=mint, style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=5)
        self.summary = ttk.Label(body, style='Card.TLabel', padding=12, wraplength=840)
        self.summary.grid(row=2, column=0, sticky='ew', pady=(8, 10))
        self.coverage = ttk.Label(body, style='Muted.TLabel', wraplength=840)
        self.coverage.grid(row=3, column=0, sticky='ew', pady=(0, 10))
        tabs = ttk.Notebook(body, style='App.TNotebook')
        tabs.grid(row=4, column=0, sticky='nsew')
        chart_page = ttk.Frame(tabs, style='Panel.TFrame')
        tabs.add(chart_page, text='Price samples')
        chart_page.columnconfigure(0, weight=1)
        chart_page.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(chart_page, background='#18223a', highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew', pady=8)
        self.canvas.bind('<Configure>', lambda _: self.draw())
        ttk.Label(chart_page, text='Recorded samples, not candles. Gaps over 90 seconds are left unconnected.',
                  style='Muted.TLabel').grid(row=1, column=0, sticky='w')
        self.history = self.table(tabs, 'Recorded history', ('time', 'price', 'cap', 'liquidity'),
                                 ('OBSERVED (UTC)', 'PRICE', 'MARKET CAP', 'LIQUIDITY'))
        self.activity = self.table(tabs, 'Session activity', ('time', 'session', 'event', 'reason'),
                                  ('RECORDED (UTC)', 'SESSION', 'DECISION', 'REASON'))
        self.activity.bind('<Double-1>', lambda _: self.open_selected_session())
        self.activity.bind('<Return>', lambda _: self.open_selected_session())
        self.status = ttk.Label(body, text='Latest 100 local decisions. Double-click activity to open its session. Monitoring records are not fills.',
                                style='Muted.TLabel', wraplength=840)
        self.status.grid(row=5, column=0, sticky='ew', pady=(12, 0))
        body.bind('<Configure>', lambda e: [label.configure(wraplength=max(200, e.width-40))
                                           for label in (self.summary, self.coverage, self.status)])
        self.reload()

    def table(self, tabs, title, columns, labels):
        page = ttk.Frame(tabs, style='Panel.TFrame')
        tabs.add(page, text=title)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        tree = ttk.Treeview(page, columns=columns, show='headings', style='App.Treeview')
        for name, label in zip(columns, labels):
            tree.heading(name, text=label)
            tree.column(name, width=210 if name == 'time' else 360 if name == 'reason' else 170)
        tree.grid(row=0, column=0, sticky='nsew')
        for orient, row, column, command, option, sticky in (
            ('vertical', 0, 1, tree.yview, 'yscrollcommand', 'ns'),
            ('horizontal', 1, 0, tree.xview, 'xscrollcommand', 'ew')):
            bar = ttk.Scrollbar(page, orient=orient, command=command)
            bar.grid(row=row, column=column, sticky=sticky)
            tree.configure(**{option: bar.set})
        return tree

    def reload(self):
        self.samples = self.library.coin_history(self.mint)
        self.events = self.library.coin_activity(self.mint)
        latest = self.samples[0] if self.samples else None
        self.symbol = latest['symbol'] if latest else self.mint[:8]
        self.title.configure(text=self.symbol + ' · coin details')
        if latest:
            self.summary.configure(text=f"Price {price_text(latest['price_usd'])}   ·   Market cap {price_text(latest['market_cap_usd'])}   ·   Liquidity {price_text(latest['liquidity_usd'])}\n"
                                   f"{'Recent sample' if is_fresh(latest['observed_at']) else 'Stale / unverified time'} · {age_text(latest['observed_at'])} · {utc(latest['observed_at'])}")
            self.coverage.configure(text=f"{len(self.samples)} retained samples shown (latest 100) · {utc(self.samples[-1]['observed_at'])} → {utc(latest['observed_at'])}\nLocal Pump market scans only. Missing periods and complete trading history are not available here.")
        else:
            self.summary.configure(text='No recorded price, market-cap or liquidity data for this mint.')
            self.coverage.configure(text='No retained snapshots. Missing data does not mean zero activity.')
        for tree in (self.history, self.activity):
            tree.delete(*tree.get_children())
        for row in self.samples:
            self.history.insert('', 'end', values=(utc(row['observed_at']), price_text(row['price_usd']), price_text(row['market_cap_usd']), price_text(row['liquidity_usd'])))
        for index, row in enumerate(self.events):
            self.activity.insert('', 'end', iid=str(index), values=(utc(row['at']), row['label'], row['kind'].replace('_', ' '), row['reason']))
        if not self.events:
            self.activity.insert('', 'end', iid='empty', values=('', 'No local session decisions', '', ''))
        self.draw()
        self.timer = self.window.after(15_000, self.reload)

    def draw(self):
        canvas = self.canvas
        canvas.delete('all')
        values = [(number(r['observed_at']), number(r['price_usd'])) for r in reversed(self.samples)]
        values = [(t, p) for t, p in values if t is not None and p is not None]
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not values:
            canvas.create_text(width/2, height/2, text='No recorded price samples', fill='#98a7c5')
            return
        lo, hi = min(p for _, p in values), max(p for _, p in values)
        start, end = values[0][0], values[-1][0]
        canvas.create_text(12, 12, anchor='nw', text=price_text(hi), fill='#edf2ff')
        canvas.create_text(12, max(40, height-42), anchor='nw', text=price_text(lo), fill='#98a7c5')
        canvas.create_text(12, height-12, anchor='sw', text=utc(start), fill='#98a7c5')
        canvas.create_text(width-12, height-12, anchor='se', text=utc(end), fill='#98a7c5')
        points = [(70 + (t-start)/max(1, end-start)*max(1,width-100),
                   25 + (1-(p-lo)/(hi-lo) if hi != lo else .5)*max(1,height-85)) for t,p in values]
        for index, (x, y) in enumerate(points):
            if index and values[index][0] - values[index-1][0] <= 90:
                canvas.create_line(*points[index-1], x, y, fill='#7c6cff', width=2)
            canvas.create_oval(x-2, y-2, x+2, y+2, fill='#6de0bd', outline='')

    def copy_mint(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.mint)
        self.status.configure(text='Mint copied.')

    def ask_assistant(self):
        if self.ask(self.mint, self.symbol):
            self.close()
        else:
            self.status.configure(text='Check the Assistant header: configure an AI profile or finish the pending request/draft first.')

    def open_selected_session(self):
        selected = self.activity.selection()
        if selected and selected[0] != 'empty':
            ident = self.events[int(selected[0])]['instance_id']
            self.close()
            self.open_session(ident)

    def close(self):
        self.closed = True
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.destroy()
