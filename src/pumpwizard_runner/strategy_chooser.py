"""One entry point for marketplace browsing, import and local strategy drafts."""

import tkinter as tk
from tkinter import ttk


def show_strategy_chooser(root, on_browse, on_import, on_create):
    window = tk.Toplevel(root)
    window.title('Strategies · PumpWizard Runner')
    window.configure(background='#34415f')
    window.transient(root)
    window.resizable(False, False)
    body = ttk.Frame(window, style='Panel.TFrame', padding=24)
    body.pack(fill='both', expand=True, padx=1, pady=1)
    ttk.Label(body, text='YOUR NEXT SESSION', style='PanelAccent.TLabel',
              font=('TkDefaultFont', 9, 'bold')).pack(anchor='w')
    ttk.Label(body, text='Choose your strategy', style='Hero.TLabel').pack(anchor='w', pady=(6, 8))
    ttk.Label(body, text='Explore the marketplace, import a configuration or create a local draft.',
              style='Muted.TLabel').pack(anchor='w')
    cards = ttk.Frame(body, style='Panel.TFrame')
    cards.pack(fill='x', pady=22)
    previous_focus = root.focus_get()

    def close():
        window.grab_release()
        window.destroy()
        if previous_focus and previous_focus.winfo_exists():
            previous_focus.focus_set()

    def choose(callback):
        close()
        callback()

    choices = (
        ('EXPLORE', 'Strategy marketplace',
         'Browse PumpWizard strategies. Compare simulation results, inspect rules and check local compatibility.',
         'Browse marketplace', on_browse),
        ('BRING YOUR OWN', 'Import configuration',
         'Open a strategy JSON file from PumpWizard or a saved export. Review its rules and compatibility before starting a session.',
         'Choose config file', on_import),
    )
    for column, (eyebrow, title, description, action, callback) in enumerate(choices):
        card = ttk.Frame(cards, style='Card.TFrame', padding=20)
        card.grid(row=0, column=column, sticky='nsew', padx=(0, 12) if column == 0 else 0)
        cards.columnconfigure(column, weight=1, uniform='choices')
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        ttk.Label(card, text=eyebrow, style='MetricCaption.TLabel',
                  foreground='#b5a8ff', font=('TkDefaultFont', 8, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(card, text=title, style='Card.TLabel', font=('TkDefaultFont', 14, 'bold')).grid(
            row=1, column=0, sticky='w', pady=(10, 10))
        ttk.Label(card, text=description, style='Card.TLabel', wraplength=280, justify='left').grid(
            row=2, column=0, sticky='nw', pady=(0, 22))
        button = ttk.Button(card, text=action, style='Accent.TButton' if column == 0 else 'Subtle.TButton',
                            command=lambda callback=callback: choose(callback))
        button.grid(row=3, column=0, sticky='ew')
        if column == 0:
            first_button = button
    creator = ttk.Frame(body, style='Card.TFrame', padding=20)
    creator.pack(fill='x', pady=(0, 22))
    creator.columnconfigure(0, weight=1)
    ttk.Label(creator, text='CREATE YOUR OWN', style='MetricCaption.TLabel', foreground='#b5a8ff',
              font=('TkDefaultFont', 8, 'bold')).grid(row=0, column=0, sticky='w')
    ttk.Label(creator, text='Create a local strategy', style='Card.TLabel', font=('TkDefaultFont', 14, 'bold')).grid(
        row=1, column=0, sticky='w', pady=(10, 6))
    ttk.Label(creator, text=('Start with a paper-only local template, set its basic rules and review the '
                              'existing configuration format before creating a session.'), style='Card.TLabel',
              wraplength=430, justify='left').grid(row=2, column=0, sticky='w')
    ttk.Button(creator, text='Create strategy', style='Subtle.TButton', command=lambda: choose(on_create)).grid(
        row=0, column=1, rowspan=3, padx=(20, 0), sticky='e')
    footer = ttk.Frame(body, style='Panel.TFrame')
    footer.pack(fill='x')
    ttk.Label(footer, text='Review first. Start a session when you’re ready.', style='Muted.TLabel').pack(side='left')
    ttk.Button(footer, text='Cancel', style='Subtle.TButton', command=close).pack(side='right')
    window.protocol('WM_DELETE_WINDOW', close)
    window.bind('<Escape>', lambda _: close())
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    x = max(0, root.winfo_rootx() + (root.winfo_width() - width) // 2)
    y = max(0, root.winfo_rooty() + (root.winfo_height() - height) // 2)
    window.geometry(f'+{x}+{y}')
    window.wait_visibility()
    window.grab_set()
    first_button.focus_set()
    return window
