"""Telegram (Bot API): отправка отчётов. Токен/чат: TG_TOKEN/TG_CHAT из .env или конфиг tgnotify."""
import os
from pathlib import Path

import requests

CONF_PATHS = [Path.home() / ".config/tgnotify/conf", Path("/data/data/com.termux/files/home/.config/tgnotify/conf")]


def _creds():
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tok and chat:
        return tok, chat
    for p in CONF_PATHS:
        if p.is_file():
            kv = dict(l.strip().split("=", 1) for l in p.read_text().splitlines() if "=" in l and not l.lstrip().startswith("#"))
            tok, chat = kv.get("TOKEN", "").strip("\"'"), kv.get("CHAT", "").strip("\"'")
            if tok and chat:
                return tok, chat
    return None, None


def _api(tok, method, **data):
    r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", json=data, timeout=40)
    return r.json()


def configured() -> bool:
    return all(_creds())


def notify(text: str) -> bool:
    tok, chat = _creds()
    if not tok:
        return False
    try:
        return bool(_api(tok, "sendMessage", chat_id=chat, text=text[:4000]).get("ok"))
    except requests.RequestException:
        return False
