"""Simple provider connection modal with encrypted optional local storage."""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from .agent_hub import AgentHubError


_PROVIDERS = {
    'OpenAI API': {
        'label': 'OpenAI', 'endpoint': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-5.6-terra',
        'models': ('gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.2'),
        'hint': 'Recommended: GPT-5.6 Terra for ongoing evidence analysis and strategy drafting. Choose Sol for harder reviews.',
    },
    'Anthropic Claude': {
        'label': 'Anthropic Claude', 'endpoint': 'https://api.anthropic.com/v1/messages',
        'model': 'claude-sonnet-5',
        'models': ('claude-sonnet-5', 'claude-opus-5-5', 'claude-haiku-4-5-20251001'),
        'hint': 'Claude uses Anthropic’s native Messages API. Sonnet is the balanced default; Opus targets harder analysis.',
    },
    'DeepSeek API': {
        'label': 'DeepSeek', 'endpoint': 'https://api.deepseek.com/chat/completions',
        'model': 'deepseek-flash',
        'models': ('deepseek-flash', 'deepseek-v4-pro'),
        'hint': 'DeepSeek uses its official Chat Completions API. Flash is the faster default; V4 Pro targets harder analysis.',
    },
    'Local Ollama': {
        'label': 'Local Ollama', 'endpoint': 'http://127.0.0.1:11434/v1/chat/completions',
        'model': 'gpt-oss-20b',
        'models': ('gpt-oss-20b', 'gpt-oss-120b'),
        'hint': 'Runs through a local OpenAI-compatible server. Enter the model installed on this computer.',
    },
    'Compatible Chat API': {
        'label': 'Compatible API', 'endpoint': '', 'model': '',
        'models': (),
        'hint': 'Use an HTTPS endpoint that implements the OpenAI Chat Completions protocol.',
    },
}


def add_profile(root, hub, settings, keys, on_saved):
    """Connect or update the one active assistant without exposing profile plumbing."""
    window = tk.Toplevel(root)
    active = next((agent for agent in settings.agents if agent.id == settings.active_agent_id), None)
    window.title('Connect Strategy Assistant')
    window.geometry('620x460')
    window.transient(root)
    window.resizable(False, False)
    body = ttk.Frame(window, style='Panel.TFrame', padding=22)
    body.pack(fill='both', expand=True)
    body.columnconfigure(1, weight=1)
    ttk.Label(body, text='Connect Strategy Assistant', style='Hero.TLabel').grid(
        row=0, column=0, columnspan=2, sticky='w', pady=(0, 6))
    ttk.Label(body, text='Choose a compatible provider, add its API key, then chat from the header.',
              style='Muted.TLabel', wraplength=570).grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 18))
    provider = tk.StringVar(value='Compatible Chat API')
    if active:
        provider.set(next((name for name, preset in _PROVIDERS.items() if preset['endpoint'] == active.endpoint),
                          'Compatible Chat API'))
    model = tk.StringVar(value=active.model if active else _PROVIDERS[provider.get()]['model'])
    endpoint = tk.StringVar(value=active.endpoint if active and provider.get() == 'Compatible Chat API' else '')
    ttk.Label(body, text='Provider', style='Muted.TLabel').grid(row=2, column=0, sticky='w', padx=(0, 14), pady=8)
    provider_choice = ttk.Combobox(body, textvariable=provider, values=tuple(_PROVIDERS), state='readonly',
                                    style='Agent.TCombobox')
    provider_choice.grid(row=2, column=1, sticky='ew', pady=8)
    ttk.Label(body, text='Model', style='Muted.TLabel').grid(row=3, column=0, sticky='w', padx=(0, 14), pady=8)
    model_choice = ttk.Combobox(body, textvariable=model, state='normal', style='Agent.TCombobox')
    model_choice.grid(row=3, column=1, sticky='ew', pady=8)
    endpoint_label = ttk.Label(body, text='Endpoint', style='Muted.TLabel')
    endpoint_input = ttk.Entry(body, textvariable=endpoint)
    endpoint_label.grid(row=4, column=0, sticky='w', padx=(0, 14), pady=8)
    endpoint_input.grid(row=4, column=1, sticky='ew', pady=8)
    ttk.Label(body, text='API key', style='Muted.TLabel').grid(row=5, column=0, sticky='w', padx=(0, 14), pady=8)
    api_key = tk.StringVar()
    ttk.Entry(body, textvariable=api_key, show='•').grid(row=5, column=1, sticky='ew', pady=8)
    remember = tk.BooleanVar(value=False)
    ttk.Checkbutton(body, text='Remember API key encrypted on this device', variable=remember,
                     style='Panel.TCheckbutton').grid(row=6, column=0, columnspan=2, sticky='w', pady=(8, 4))
    hint = ttk.Label(body, style='Muted.TLabel', wraplength=570)
    hint.grid(row=7, column=0, columnspan=2, sticky='w', pady=(6, 10))
    status = ttk.Label(body, style='Muted.TLabel', wraplength=570)
    status.grid(row=8, column=0, columnspan=2, sticky='ew')

    def update_provider(_event=None):
        preset = _PROVIDERS[provider.get()]
        custom = provider.get() == 'Compatible Chat API'
        model_choice.configure(values=preset['models'])
        if not custom:
            endpoint.set(preset['endpoint'])
            if _event is not None or not model.get():
                model.set(preset['model'])
            endpoint_label.grid_remove()
            endpoint_input.grid_remove()
        else:
            endpoint_label.grid()
            endpoint_input.grid()
            if not endpoint.get() and active:
                endpoint.set(active.endpoint)
        hint.configure(text=preset['hint'] + ' Saving this screen sends no request. API keys never reach wallet code.')

    provider_choice.bind('<<ComboboxSelected>>', update_provider)
    update_provider()

    def save():
        try:
            preset = _PROVIDERS[provider.get()]
            selected_endpoint = endpoint.get().strip() if provider.get() == 'Compatible Chat API' else preset['endpoint']
            selected_label = preset['label']
            token = api_key.get()
            same_endpoint = active and active.endpoint == selected_endpoint
            if provider.get() != 'Local Ollama' and not token and not (same_endpoint and (keys.available(active) or keys.exists(active))):
                raise AgentHubError('Enter an API key for this provider')
            updated = hub.replace_active(settings, label=selected_label, endpoint=selected_endpoint,
                                         model=model.get(), api_key_env='')
            profile = next(agent for agent in updated.agents if agent.id == updated.active_agent_id)
            password = None
            if token and remember.get():
                password = simpledialog.askstring('Encrypt AI key', 'Choose a separate AI-key password (12+ characters):', show='•', parent=window)
                if password is None:
                    return
                confirm = simpledialog.askstring('Confirm AI-key password', 'Repeat the AI-key password:', show='•', parent=window)
                if confirm != password:
                    raise AgentHubError('AI-key passwords did not match')
            if token:
                keys.set(profile, token, password)
            hub.save(updated)
            api_key.set('')
            on_saved(updated)
            window.destroy()
        except (AgentHubError, OSError, ValueError) as exc:
            status.configure(text=str(exc))

    actions = ttk.Frame(body, style='Panel.TFrame')
    actions.grid(row=9, column=0, columnspan=2, sticky='ew', pady=(12, 0))
    ttk.Button(actions, text='Cancel', style='Subtle.TButton', command=window.destroy).pack(side='right')
    ttk.Button(actions, text='Save connection' if active else 'Connect assistant', style='Accent.TButton', command=save).pack(
        side='right', padx=8)
    return window


def set_key(root, profile, keys):
    if profile is None:
        messagebox.showinfo('AI profile', 'Add or select a profile first.', parent=root)
        return
    token = simpledialog.askstring('API key', f'API key for {profile.label}:', show='•', parent=root)
    if not token:
        return
    remember = messagebox.askyesnocancel('Remember AI key', 'Save encrypted on this device?\n\nNo keeps the key only until the app closes.', parent=root)
    if remember is None:
        return
    password = None
    if remember:
        password = simpledialog.askstring('Encrypt AI key', 'Choose an AI-key password (12+ characters):', show='•', parent=root)
        if password is None:
            return
        confirmation = simpledialog.askstring('Confirm AI-key password', 'Repeat the AI-key password:', show='•', parent=root)
        if confirmation != password:
            messagebox.showerror('AI key', 'AI-key passwords did not match.', parent=root)
            return
    try:
        keys.set(profile, token, password)
    except (AgentHubError, OSError, ValueError) as exc:
        messagebox.showerror('AI key', str(exc), parent=root)
