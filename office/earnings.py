"""Отчёты компаний — ПРАВИЛО РИСКА (не правило входа; в отпечаток правил входа не входит):
· не открывать позицию в акции, если её отчёт в ближайшие 2 торговых дня (включая сегодняшний);
· если позиция в акции уже открыта, а отчёт сегодня или на следующий торговый день — закрыть её заранее, пока биржа открыта (первый же проход в часы биржи накануне).
Дата отчёта — yfinance (только дата, до/после биржи неизвестно, поэтому консервативно: накануне закрываем целиком). Торговые дни — календарь Alpaca (выходные и праздники учтены), запасной — будни.
Нет данных о дате отчёта (источник недоступен) — новых входов в эту акцию нет. ETF отчётов не имеют."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import sources

NY = ZoneInfo("America/New_York")
NO_ENTRY_DAYS = 2      # вход запрещён, если до отчёта ≤ 2 торговых дней (0 = отчёт сегодня)
EXIT_DAYS = 1          # позиция закрывается, если до отчёта ≤ 1 торгового дня
_CAL: dict = {}


def today_ny(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY).date()


def trading_days(client, start: dt.date, end: dt.date) -> list[dt.date]:
    """Торговые даты (start, end] включительно по end, исключая start: календарь Alpaca, при сбое — будни."""
    key = (start, end)
    if key not in _CAL:
        try:
            rows = client._req("GET", "/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
            days = sorted(dt.date.fromisoformat(r["date"]) for r in rows)
        except Exception:  # noqa: BLE001
            days = [start + dt.timedelta(days=i) for i in range(0, (end - start).days + 1) if (start + dt.timedelta(days=i)).weekday() < 5]
        _CAL[key] = days
    return [d for d in _CAL[key] if start < d <= end]


def days_until(client, report_date: dt.date, today: dt.date | None = None) -> int | None:
    """Сколько торговых дней до отчёта: 0 — сегодня, 1 — следующий торговый день… None — отчёт уже прошёл."""
    today = today or today_ny()
    if report_date < today:
        return None
    if report_date == today:
        return 0
    return len(trading_days(client, today, report_date)) if report_date.weekday() < 5 else len(trading_days(client, today, report_date)) + 1


def next_report(symbol: str, board=None, today: dt.date | None = None) -> tuple[dt.date | None, bool]:
    """(ближайшая дата отчёта | None, данные получены). Источник — yfinance (кэш 6 ч), запасной — таблица events (отчёты, уже сохранённые прошлыми проходами)."""
    today = today or today_ny()
    cal = sources.cached(f"earn:{symbol}", 6 * 3600, lambda: __import__("yfinance").Ticker(symbol).calendar)
    if isinstance(cal, dict) and "error" not in cal:
        days = [d for d in (cal.get("Earnings Date") or []) if hasattr(d, "year")]
        fut = sorted(d for d in days if d >= today)
        return (fut[0] if fut else None), True
    if board is not None:   # источник не ответил: смотрим, что знали раньше
        rows = board.q("SELECT ts FROM events WHERE kind='earnings' AND assets=? ORDER BY ts", (symbol,))
        fut = sorted(d for d in (dt.datetime.fromisoformat(r["ts"]).astimezone(NY).date() for r in rows) if d >= today)
        if fut:
            return fut[0], True
    return None, False


def entry_block(client, board, symbol: str, today: dt.date | None = None) -> str:
    """'' — вход разрешён; иначе русская причина запрета (акции; крипта и ETF без отчётов проходят)."""
    if "/" in symbol:
        return ""
    d, ok = next_report(symbol, board, today)
    if not ok:
        return f"нет данных о дате отчёта {symbol}: новых входов в акцию нет, пока дата неизвестна"
    if d is None:
        return ""
    n = days_until(client, d, today)
    if n is not None and n <= NO_ENTRY_DAYS:
        return f"отчёт компании {symbol} {d:%d.%m} ({'сегодня' if n == 0 else 'через ' + str(n) + ' торг. дн.'}): не входим за {NO_ENTRY_DAYS} торговых дня до отчёта"
    return ""


def must_exit(client, board, symbol: str, today: dt.date | None = None) -> str:
    """'' — держать; иначе причина закрыть позицию заранее (отчёт сегодня или на следующий торговый день)."""
    if "/" in symbol:
        return ""
    d, ok = next_report(symbol, board, today)
    if not ok or d is None:
        return ""
    n = days_until(client, d, today)
    if n is not None and n <= EXIT_DAYS:
        return f"отчёт компании {symbol} {d:%d.%m} ({'сегодня' if n == 0 else 'на следующий торговый день'}): позиция закрыта заранее"
    return ""
