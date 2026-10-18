"""Заморозка правил входа: пока не накоплено FREEZE_MIN_TRADES закрытых сделок по текущим правилам, правила не меняются.
«Уроки дня» (review.py → memory) только ПИШУТСЯ в журнал и показываются в отчётах — ни один модуль не меняет по ним пороги, веса или шаблоны (автоподстройки в облаке нет:
weights.propose/apply и лаборатория выключены). Отпечаток правил (константы + исходный код шаблонов сигнала, уровней, размера и блокировок) запоминается в journal.kv;
если отпечаток изменился (кто-то поправил правила в коде), счётчик сделок начинается заново, а владелец получает сообщение. Тест test_rules.py привязан к текущему отпечатку:
изменить правила можно только осознанно — обновив тест и сбросив счёт."""
import hashlib
import inspect

from . import config, desks, entry, events

FREEZE_MIN_TRADES = 20
VERSION = "v5.0"


def snapshot() -> dict:
    """Всё, что определяет вход, размер, выход и блокировки."""
    return {"entry": {k: getattr(entry, k) for k in ("STOP_ATR", "TAKE_ATR", "HALF", "MIN_LIVE_FULL", "RSI_MAX", "RSI_MIN_TREND", "RSI_BOUNCE", "COOLDOWN_H", "CT_STOP_ATR", "CT_TAKE_ATR",
                                                      "CRYPTO_STOP_ATR", "CRYPTO_TAKE_ATR", "TECH_ONLY", "MIN_REWARD_RISK", "MIN_TARGET_FEES", "MAX_TAKE_ATR", "MAX_SPREAD")},
            "desks": {k: getattr(desks, k) for k in ("BASE_SHARE", "RESERVE", "MIN_SHARE", "MAX_SHARE", "STEP", "MAX_POSITIONS", "MAX_ENTRIES_DAY", "CLOSE_AFTER_TRADES", "POSITION_CAP", "ORDER")},
            "desk_specs": {k: [v["patterns"], v["assets"]] for k, v in desks.BUILTIN.items()},
            "counter": list(entry.COUNTER), "events": [events.PRE_MIN, events.POST_MIN], "limits": vars(config.Limits()), "turnover": dict(config.MIN_TURNOVER),
            "code": [inspect.getsource(f) for f in (entry.technical, entry.majority, entry.data_gaps, entry.levels, entry.trade_plan, desks.route, desks.eligible, desks.limits_for, desks.council)]}


def fingerprint(snap: dict | None = None) -> str:
    import json
    return hashlib.sha1(json.dumps(snap or snapshot(), sort_keys=True, default=str).encode()).hexdigest()[:12]


def register(j, notify=lambda t: None, now_iso: str = "") -> bool:
    """Вызывать на каждом проходе. Первый запуск запоминает отпечаток и дату начала счёта; смена отпечатка — новый счёт + сообщение. True, если правила изменились."""
    from .hall import utcnow
    now_iso = now_iso or utcnow()
    fp, old = fingerprint(), j.kv_get("rules_fp")
    if fp == old:
        return False
    j.kv_set("rules_fp", fp)
    j.kv_set("rules_since", now_iso)
    if old is not None:
        notify(f"⚠️ Правила входа изменились (версия {VERSION}, отпечаток {fp}). Счёт «{FREEZE_MIN_TRADES} сделок без изменений правил» начат заново.")
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
