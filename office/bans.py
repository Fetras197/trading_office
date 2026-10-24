"""Запреты совета на КЛАСС активов (v5.9): «Сегодня нельзя» из «Картины дня» по классу (крипта, альткоины, акции) или отрасли (технологии, чипы, …) исполняются кодом на входе.
Раньше исполнялись только запреты по символу (avoid_assets), а строка вида «Запрещено открытие новых длинных позиций в криптоактивах» оставалась текстом (24.10 после неё куплены ETH, DOT, CRV).
Совет выдаёт запреты структурированно: class_bans = [{scope: crypto|altcoins|stocks|sector, sector: <ключ>, reason, hours 1..24}]. Для картин без поля (и для строк `forbidden`) запрет выводится из текста строки
(слова «запрещено / не открывать / избегать покупок …» + класс; строки про плечо, игнорирование, усреднение, «крупные позиции» — не запреты входа). Срок текстового запрета — 12 часов от времени картины.
Запрет живёт по сроку каждой картины, а не по самой свежей: пока какая-нибудь картина за последние 24 часа действует по сроку, класс закрыт. Открытые позиции не затрагиваются — только новые входы.
Это исполнение решений совета, не правило входа: в отпечаток правил (rules.snapshot) не входит."""
import datetime as dt
import re

from . import entry

SCOPES = ("crypto", "altcoins", "stocks", "sector")
TEXT_HOURS = 12
SECTOR_WORDS = {   # ключ отрасли -> корни русских слов
    "semiconductors": ("чип", "полупроводник"), "technology": ("технолог", "информационн", "айти"), "real_estate": ("недвижимост",), "industrials": ("промышлен",),
    "consumer": ("потребител",), "financial": ("финанс", "банк"), "energy": ("энергет", "нефт"), "healthcare": ("здравоохран", "медицин", "фарма"),
    "materials": ("сырьев", "материал"), "utilities": ("коммунальн",), "communication": ("коммуникац",)}
LABEL = {"crypto": "криптоактивы", "altcoins": "альткоины", "stocks": "акции", "semiconductors": "чипы и полупроводники", "technology": "технологии", "real_estate": "недвижимость",
         "industrials": "промышленность", "consumer": "потребительский сектор", "financial": "финансы", "energy": "энергетика", "healthcare": "здравоохранение",
         "materials": "сырьё и материалы", "utilities": "коммунальные", "communication": "связь и медиа"}
ETF_SECTOR = {"XLK": "technology", "XLE": "energy", "XLF": "financial", "XLV": "healthcare", "XLI": "industrials", "XLY": "consumer", "XLP": "consumer", "XLU": "utilities",
              "XLB": "materials", "XLRE": "real_estate", "XLC": "communication", "SMH": "semiconductors", "SOXX": "semiconductors"}
YF_SECTOR = {"technology": "technology", "healthcare": "healthcare", "financial services": "financial", "consumer cyclical": "consumer", "consumer defensive": "consumer",
             "industrials": "industrials", "energy": "energy", "basic materials": "materials", "utilities": "utilities", "real estate": "real_estate", "communication services": "communication"}
NOT_ENTRY = re.compile(r"плеч|игнорир|усредн|стоп-лосс|защитн\w* стоп|крупн\w* позиц|превыш|лимит")
ENTRY = re.compile(r"открыв|открыт|покуп|вход|наращив|экспозиц|длинн|лонг")


def _key(b: dict) -> tuple:
    return (b["scope"], b.get("sector") or "")


def label(b: dict) -> str:
    return LABEL.get(b.get("sector") or b["scope"], b.get("sector") or b["scope"])


def parse_text(line: str) -> list[dict]:
    """Строка «Сегодня нельзя» → запреты на класс/отрасль (пусто, если строка не про вход в класс). Срок и причина — вызывающий."""
    low = str(line).lower()
    if not ENTRY.search(low) or NOT_ENTRY.search(low):
        return []
    out = []
    for key, roots in SECTOR_WORDS.items():
        if any(r in low for r in roots):
            out.append({"scope": "sector", "sector": key})
    for t in re.findall(r"\bXL[A-Z]{1,2}\b|\bSMH\b|\bSOXX\b", str(line)):
        if t in ETF_SECTOR and not any(b["sector"] == ETF_SECTOR[t] for b in out):
            out.append({"scope": "sector", "sector": ETF_SECTOR[t]})
    if out:
        return out
    if "альт" in low or "мем" in low:
        return [{"scope": "altcoins"}]
    if "крипт" in low:
        return [{"scope": "crypto"}]
    if re.search(r"акци", low) and not re.search(r"акци\w*\s+с\s", low):
        return [{"scope": "stocks"}]
    return []


def normalize(raw, now: dt.datetime | None = None) -> list[dict]:
    """Проверка class_bans от модели: допустимые класс/отрасль, причина ≥5 знаков, срок 1..24 ч; хранится с готовым `until`."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            scope, sector, why, h = str(e["scope"]).strip(), str(e.get("sector") or "").strip(), str(e["reason"]).strip()[:140], float(e["hours"])
        except (KeyError, TypeError, ValueError):
            continue
        if scope not in SCOPES or (scope == "sector" and sector not in SECTOR_WORDS) or len(why) < 5 or not 1 <= h <= 24:
            continue
        b = {"scope": scope, "sector": sector if scope == "sector" else "", "reason": why, "until": (now + dt.timedelta(hours=h)).isoformat(timespec="minutes"), "src": "council"}
        if _key(b) not in [_key(x) for x in out]:
            out.append(b)
    return out[:8]


def from_forbidden(lines, now: dt.datetime | None = None, hours: float = TEXT_HOURS) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for ln in lines or []:
        for b in parse_text(ln):
            b = {**b, "sector": b.get("sector", ""), "reason": str(ln).strip()[:140], "until": (now + dt.timedelta(hours=hours)).isoformat(timespec="minutes"), "src": "text"}
            if _key(b) not in [_key(x) for x in out]:
                out.append(b)
    return out


def _dt(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def active(board, now: dt.datetime | None = None) -> list[dict]:
    """Действующие запреты на класс: из всех картин за 24 часа (структурированные — по их сроку; у старых картин — выведенные из текста, 12 ч от времени картины)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    got: dict[tuple, dict] = {}
    for r in board.q("SELECT ts, data FROM reports WHERE role='picture' AND ts>=? ORDER BY id", (since,)):
        import json
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        cands = d.get("class_bans") if d.get("class_bans") is not None else from_forbidden(d.get("forbidden"), _dt(r["ts"]))
        for b in cands:
            try:
                if _dt(b["until"]) > now and (_key(b) not in got or _dt(b["until"]) > _dt(got[_key(b)]["until"])):
                    got[_key(b)] = b
            except (KeyError, ValueError):
                continue
    return list(got.values())


def sectors_of(symbol: str) -> set[str]:
    """Отрасли акции/ETF: таблица ETF, иначе yfinance (кэш 7 суток; в тестах выключен флагом earnings.YF_INDUSTRY)."""
    if symbol in ETF_SECTOR:
        return {ETF_SECTOR[symbol]}
    from . import earnings, sources
    if not earnings.YF_INDUSTRY:
        return set()
    info = sources.cached(f"sector:{symbol}", 7 * 86400, lambda: (lambda i: {"sector": i.get("sector") or "", "industry": i.get("industry") or ""})(__import__("yfinance").Ticker(symbol).info or {}))
    if not isinstance(info, dict) or "error" in info:
        return set()
    out = set()
    if YF_SECTOR.get(str(info.get("sector", "")).lower()):
        out.add(YF_SECTOR[str(info["sector"]).lower()])
    if "semiconductor" in str(info.get("industry", "")).lower():
        out.add("semiconductors")
    return out


def blocking(board, asset, now: dt.datetime | None = None) -> dict | None:
    """Запрет, под который попадает вход в asset (config.Asset), либо None. Приоритет: класс, потом отрасль."""
    crypto = asset.kind == "crypto"
    for b in active(board, now):
        s = b["scope"]
        if (s == "crypto" and crypto) or (s == "altcoins" and crypto and asset.name not in entry.ALT_EXEMPT) or (s == "stocks" and not crypto):
            return b
        if s == "sector" and not crypto and b["sector"] in sectors_of(asset.name):
            return b
    return None
