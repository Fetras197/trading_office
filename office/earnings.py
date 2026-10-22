"""Отчёты компаний — ПРАВИЛО РИСКА (не правило входа; в отпечаток правил входа не входит):
· не открывать позицию в акции, если её отчёт в ближайшие 2 торговых дня (включая сегодняшний);
· если позиция в акции уже открыта, а отчёт сегодня или на следующий торговый день — закрыть её заранее, пока биржа открыта (первый же проход в часы биржи накануне).
Дата отчёта — yfinance (только дата, до/после биржи неизвестно, поэтому консервативно: накануне закрываем целиком). Торговые дни — календарь Alpaca (выходные и праздники учтены), запасной — будни.
Нет данных о дате отчёта (источник недоступен) — новых входов в эту акцию нет. ETF отчётов не имеют.
v5.8 (инцидент KLAC: гэп после отчёта конкурента LRCX, убыток 280 $ при заложенных 113 $): те же два правила действуют и для отчёта КРУПНОГО КОНКУРЕНТА из той же отрасли
(полупроводники, банки, ритейл, нефть): отчёт конкурента сегодня или в ближайшие 2 торговых дня — вход запрещён, открытая позиция закрывается до закрытия биржи.
Отрасль — по таблице крупнейших эмитентов (GROUPS), для остальных акций — по отрасли yfinance (кэш 7 суток). Нет данных об отчёте конкурента — этот конкурент пропускается (запрет только по известной дате)."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from . import sources

NY = ZoneInfo("America/New_York")
NO_ENTRY_DAYS = 2      # вход запрещён, если до отчёта ≤ 2 торговых дней (0 = отчёт сегодня)
EXIT_DAYS = 1          # позиция закрывается, если до отчёта ≤ 1 торгового дня
PEER_DAYS = 2          # отчёт крупного конкурента: вход запрещён и позиция закрывается, если до него ≤ 2 торговых дней (0 = сегодня)
# Крупные эмитенты отраслей: их отчёты двигают всю группу (гэп у соседей). Группа акции = её отрасль; конкуренты = остальные участники группы.
GROUPS = {
    "semis": ("полупроводники", ("NVDA", "AMD", "AVGO", "QCOM", "TXN", "MU", "INTC", "AMAT", "LRCX", "KLAC", "ASML", "TSM", "ADI", "MRVL", "ON", "NXPI", "MCHP", "TER", "ARM", "MPWR", "SWKS")),
    "banks": ("банки", ("JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "PNC", "TFC", "SCHW", "BK", "COF")),
    "retail": ("ритейл", ("WMT", "TGT", "COST", "HD", "LOW", "AMZN", "TJX", "ROST", "DG", "DLTR", "BBY", "KR", "BJ")),
    "oil": ("нефть и газ", ("XOM", "CVX", "COP", "OXY", "EOG", "SLB", "PSX", "MPC", "VLO", "HAL", "DVN", "FANG", "BKR")),
}
INDUSTRY_WORDS = {"semis": ("semiconductor",), "banks": ("banks -",), "retail": ("discount stores", "home improvement", "specialty retail", "department stores", "grocery stores", "apparel retail", "internet retail"),
                  "oil": ("oil & gas",)}
_CAL: dict = {}
YF_INDUSTRY = True     # определять отрасль незнакомых акций через yfinance (тесты выключают: без сети)


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
    if d is not None:
        n = days_until(client, d, today)
        if n is not None and n <= NO_ENTRY_DAYS:
            return f"отчёт компании {symbol} {d:%d.%m} ({'сегодня' if n == 0 else 'через ' + str(n) + ' торг. дн.'}): не входим за {NO_ENTRY_DAYS} торговых дня до отчёта"
    return peer_entry_block(client, board, symbol, today)


def must_exit(client, board, symbol: str, today: dt.date | None = None) -> str:
    """'' — держать; иначе причина закрыть позицию заранее (отчёт сегодня или на следующий торговый день)."""
    if "/" in symbol:
        return ""
    d, ok = next_report(symbol, board, today)
    if ok and d is not None:
        n = days_until(client, d, today)
        if n is not None and n <= EXIT_DAYS:
            return f"отчёт компании {symbol} {d:%d.%m} ({'сегодня' if n == 0 else 'на следующий торговый день'}): позиция закрыта заранее"
    return peer_must_exit(client, board, symbol, today)


# ---------- отчёты крупных конкурентов (v5.8) ----------
def group_of(symbol: str) -> str | None:
    """Ключ отрасли: по таблице GROUPS, иначе по отрасли yfinance (кэш 7 суток); не определена — None."""
    for k, (_, members) in GROUPS.items():
        if symbol in members:
            return k
    if not YF_INDUSTRY:
        return None
    ind = sources.cached(f"industry:{symbol}", 7 * 86400, lambda: (__import__("yfinance").Ticker(symbol).info or {}).get("industry") or "")
    low = ind.lower() if isinstance(ind, str) else ""
    for k, words in INDUSTRY_WORDS.items():
        if any(w in low for w in words):
            return k
    return None


def peer_reports(group: str, exclude: str, board=None, today: dt.date | None = None) -> dict[str, dt.date]:
    """{конкурент: ближайшая дата отчёта} по крупным эмитентам группы (параллельно; нет данных — конкурент пропускается)."""
    peers = [m for m in GROUPS[group][1] if m != exclude]
    def one(m):
        try:
            d, ok = next_report(m, board, today)
        except Exception:  # noqa: BLE001
            return m, None
        return m, d if ok else None
    with ThreadPoolExecutor(max_workers=8) as ex:
        return {m: d for m, d in ex.map(one, peers) if d is not None}


def peer_alert(client, board, symbol: str, today: dt.date | None = None) -> tuple[str, str, dt.date, int] | None:
    """(группа, конкурент, дата, торговых дней до отчёта) для ближайшего отчёта крупного конкурента не дальше PEER_DAYS; иначе None. ETF и крипта — None."""
    if "/" in symbol:
        return None
    g = group_of(symbol)
    if not g:
        return None
    best = None
    for m, d in peer_reports(g, symbol, board, today).items():
        n = days_until(client, d, today)
        if n is not None and n <= PEER_DAYS and (best is None or (n, m) < (best[3], best[1])):
            best = (g, m, d, n)
    return best


def _peer_text(alert, symbol: str, closing: bool) -> str:
    g, m, d, n = alert
    when = "сегодня" if n == 0 else "на следующий торговый день" if n == 1 else f"через {n} торг. дн."
    return (f"отчёт крупного конкурента {m} ({GROUPS[g][0]}) {d:%d.%m} ({when}): гэп может зацепить и {symbol}" +
            (", позиция закрыта заранее" if closing else f", не входим за {PEER_DAYS} торговых дня до отчёта конкурента"))


def peer_entry_block(client, board, symbol: str, today: dt.date | None = None) -> str:
    a = peer_alert(client, board, symbol, today)
    return _peer_text(a, symbol, False) if a else ""


def peer_must_exit(client, board, symbol: str, today: dt.date | None = None) -> str:
    a = peer_alert(client, board, symbol, today)
    return _peer_text(a, symbol, True) if a else ""
