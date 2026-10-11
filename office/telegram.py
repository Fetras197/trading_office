"""Telegram: отчёты и кнопки Одобрить/Отклонить (Bot API). Токен/чат: TG_TOKEN/TG_CHAT из .env или конфиг tgnotify.
Рекомендуется отдельный бот для одобрений (TG_TOKEN): getUpdates нельзя делать параллельно с другим процессом на том же токене —
в этом случае ответа не будет и сделка безопасно отклонится по таймауту."""
import os
import time
import uuid
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


def ask_approval(asset: str, ticket: dict, reason: str, timeout_s: int = 600, poll=_api):
    """True/False по кнопке; None — нет ответа за timeout (трактуется как отклонение)."""
    tok, chat = _creds()
    if not tok:
        return None
    rid = uuid.uuid4().hex[:8]
    text = (f"🟢 Сделка на одобрение: BUY {asset}\nqty {ticket['qty']} @ ~{ticket['entry']:.4g}\n"
            f"stop {ticket['stop']:.4g}  take {ticket['take']:.4g}\nнотионал ${ticket['notional']}\n{reason}")
    kb = {"inline_keyboard": [[{"text": "✅ Одобрить", "callback_data": f"ok:{rid}"},
                               {"text": "❌ Отклонить", "callback_data": f"no:{rid}"}]]}
    try:
        m = poll(tok, "sendMessage", chat_id=chat, text=text, reply_markup=kb)
        offset, end = None, time.time() + timeout_s
        while time.time() < end:
            upd = poll(tok, "getUpdates", timeout=20, offset=offset, allowed_updates=["callback_query"])
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if cq and cq.get("data", "").endswith(rid) and str(cq["message"]["chat"]["id"]) == str(chat):
                    ok = cq["data"].startswith("ok:")
                    poll(tok, "answerCallbackQuery", callback_query_id=cq["id"], text="Одобрено" if ok else "Отклонено")
                    poll(tok, "editMessageReplyMarkup", chat_id=chat, message_id=m["result"]["message_id"], reply_markup={"inline_keyboard": []})
                    return ok
    except (requests.RequestException, KeyError):
        return None
    return None
