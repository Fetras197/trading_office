"""Заморозка правил входа: пока не накоплено FREEZE_MIN_TRADES закрытых сделок по текущим правилам, правила не меняются.
«Уроки дня» (review.py → memory) только ПИШУТСЯ в журнал и показываются в отчётах — ни один модуль не меняет по ним пороги, веса или шаблоны (автоподстройки в облаке нет:
weights.propose/apply и лаборатория выключены).
Отпечаток правил считается ТОЛЬКО от параметров правил входа, размера и блокировок (числа и списки: пороги шаблонов, уровни стопа/тейка, экономика, лимиты отделов и риска,
окна событий, спред, лимит альткоинов, комиссии) — не от исходного кода: рефакторинг, комментарии и обычные коммиты счёт не сбрасывают. Если параметр изменился,
счёт сделок начинается заново, а владелец получает сообщение. Тест test_v42.py закрепляет отпечаток: менять параметры можно только осознанно."""
import hashlib
import json

from . import config, desks, entry, events, sources

FREEZE_MIN_TRADES = 20
VERSION = "v5.2"
SCHEME = "p1:"                                   # схема отпечатка: «только параметры»
V5_START = "2026-10-18T21:37:43+00:00"           # начало счёта 20 сделок (запуск корпорации); при переходе на схему p1 счёт не сбрасывается


def _consts(mod) -> dict:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(mod).items()
            if k.isupper() and (isinstance(v, (int, float, bool)) or (isinstance(v, tuple) and all(isinstance(x, (str, int, float)) for x in v)))}


def snapshot() -> dict:
    """Параметры, определяющие вход, размер, выход и блокировки (без исходного кода)."""
    return {"entry": _consts(entry), "desks": _consts(desks), "desk_specs": {k: [list(v["patterns"]), v["assets"]] for k, v in desks.BUILTIN.items()},
            "events": [events.PRE_MIN, events.POST_MIN, sources.KEY_US_EVENT.pattern], "limits": vars(config.Limits()), "turnover": dict(config.MIN_TURNOVER),
            "costs": {k: vars(v) for k, v in config.COSTS.items()}}


def fingerprint(snap: dict | None = None) -> str:
    return SCHEME + hashlib.sha1(json.dumps(snap or snapshot(), sort_keys=True, default=str).encode()).hexdigest()[:12]


def register(j, notify=lambda t: None, now_iso: str = "") -> bool:
    """Вызывать на каждом проходе. Первый запуск запоминает отпечаток и дату начала счёта; переход со старой схемы (по коду) — молча, счёт с V5_START;
    смена параметров — новый счёт + сообщение. True, если правила изменились."""
    from .hall import utcnow
    now_iso = now_iso or utcnow()
    fp, old = fingerprint(), j.kv_get("rules_fp")
    if fp == old:
        return False
    j.kv_set("rules_fp", fp)
    if old is not None and not str(old).startswith(SCHEME):   # миграция схемы: отпечаток по коду → по параметрам; счёт 20 сделок остаётся с запуска корпорации
        j.kv_set("rules_since", V5_START)
        return False
    j.kv_set("rules_since", now_iso)
    if old is not None:
        notify(f"⚠️ Параметры правил входа изменились (версия {VERSION}, отпечаток {fp}). Счёт «{FREEZE_MIN_TRADES} сделок без изменений правил» начат заново.")
    return old is not None


def status(j) -> dict:
    since = j.kv_get("rules_since") or ""
    n = j.q("SELECT COUNT(*) n FROM trades WHERE exit_reason!='partial' AND exit_ts>=?", (since,))[0]["n"] if since else 0
    return {"version": VERSION, "fp": j.kv_get("rules_fp"), "since": since, "closed": n, "need": FREEZE_MIN_TRADES, "frozen": n < FREEZE_MIN_TRADES}


def line(st: dict) -> str:
    if st["frozen"]:
        return (f"Правила входа зафиксированы ({st['version']}): закрыто {st['closed']} из {st['need']} сделок. До {st['need']} сделок правила не меняются, "
                "уроки дня только записываются в журнал.")
    return f"Правила входа ({st['version']}): накоплено {st['closed']} закрытых сделок — можно разбирать результаты и решать об изменениях вручную."
