"""Календарь событий (1.2): макро (Faireconomy), отчёты компаний, экспирация опционов. Таблица events в board.db.
Гейт `blocked`: за 30 мин до и 30 мин после события высокой важности новые входы по связанным активам запрещены (до вызова LLM).
Связанные активы: USD-события высокой важности — все активы; отчёт компании — только её акция (весь день отчёта США + до 10:00 следующего дня:
время выхода неизвестно); квартальная экспирация Deribit — вся крипта."""
import datetime as dt
import hashlib
import re

from . import sources

PRE_MIN, POST_MIN = 30, 30
TITLES_RU = [  # порядок важен: более длинные шаблоны раньше
    (r"FOMC Press Conference", "пресс-конференция ФРС"), (r"FOMC Statement", "решение ФРС по ставке"), (r"Federal Funds Rate", "решение ФРС по ставке"),
    (r"FOMC Meeting Minutes", "протокол заседания ФРС"), (r"Core CPI", "базовая инфляция (CPI)"), (r"\bCPI\b", "инфляция (CPI)"), (r"Core PPI", "базовые цены производителей"),
    (r"\bPPI\b", "цены производителей"), (r"Non-Farm Employment Change", "занятость в США (Non-Farm)"), (r"Unemployment Rate", "уровень безработицы"),
    (r"Unemployment Claims", "заявки на пособие по безработице"), (r"Core Retail Sales|Retail Sales", "розничные продажи"), (r"GDP", "ВВП"), (r"ISM", "деловая активность (ISM)"),
    (r"Core PCE", "базовая инфляция (PCE)"), (r"PCE", "инфляция (PCE)"), (r"Powell", "выступление главы ФРС"), (r"FOMC Member", "выступление члена ФРС"),
    (r"Fed Chair", "выступление главы ФРС"), (r"Treasury Currency Report", "отчёт Минфина США")]


def title_ru(ev: dict) -> str:
    if ev.get("kind") != "macro":
        return ev["title"]
    for pat, ru in TITLES_RU:
        if re.search(pat, ev["title"], re.I):
            return ru
    return f"событие ({ev['ccy']})"


def _key(e: dict) -> str:
    return hashlib.md5(f"{e['ts']}|{e['title']}|{e.get('assets', '')}".encode()).hexdigest()[:16]


def _dt(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def refresh(board, symbols=()) -> int:
    """Обновить таблицу событий из источников; возвращает число событий в таблице на ближайшие 3 дня."""
    evs = sources.faireconomy_events() + sources.deribit_expiry() + sources.earnings_events([s for s in symbols if "/" not in s])
    for e in evs:
        board.x("INSERT INTO events(key,ts,until,title,ccy,impact,kind,assets,src,data) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET impact=excluded.impact, until=excluded.until",
                (_key(e), e["ts"], e.get("until"), e["title"], e.get("ccy"), e["impact"], e["kind"], e.get("assets", ""), e["src"],
                 str({k: e.get(k) for k in ("forecast", "previous")})))
    board.x("DELETE FROM events WHERE ts<?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(timespec="minutes"),))
    return len(board.q("SELECT 1 FROM events"))


def upcoming(board, hours: float = 24, now: dt.datetime | None = None, high_only: bool = False) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    lo, hi = (now - dt.timedelta(minutes=POST_MIN)).isoformat(timespec="minutes"), (now + dt.timedelta(hours=hours)).isoformat(timespec="minutes")
    rows = board.q("SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts", (lo, hi))
    return [r for r in rows if r["impact"] == "High"] if high_only else rows


def blocked(board, asset: str, kind: str, now: dt.datetime | None = None) -> str:
    """'' если вход разрешён; иначе короткая русская причина запрета."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for r in board.q("SELECT * FROM events WHERE impact='High' AND ts<=? AND COALESCE(until,ts)>=?",
                     ((now + dt.timedelta(minutes=PRE_MIN)).isoformat(timespec="minutes"), (now - dt.timedelta(minutes=POST_MIN)).isoformat(timespec="minutes"))):
        a = r["assets"]
        if a == "all" or a == kind or a == asset:
            if r["until"]:
                if _dt(r["ts"]) - dt.timedelta(minutes=PRE_MIN) <= now <= _dt(r["until"]) + dt.timedelta(minutes=POST_MIN):
                    return f"{title_ru(r)}"
            elif _dt(r["ts"]) - dt.timedelta(minutes=PRE_MIN) <= now <= _dt(r["ts"]) + dt.timedelta(minutes=POST_MIN):
                return f"{title_ru(r)}"
    return ""
