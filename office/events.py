"""Календарь событий (1.2): макро (Faireconomy), отчёты компаний, экспирация опционов. Таблица events в board.db.
Гейт `blocked`: за 60 мин до и 60 мин после события высокой важности новые входы по связанным активам запрещены (до вызова LLM).
Связанные активы: USD-события высокой важности — все активы; отчёт компании — только её акция (весь день отчёта США + до 10:00 следующего дня:
время выхода неизвестно); квартальная экспирация Deribit — вся крипта."""
import datetime as dt
import hashlib
import re

from . import sources

PRE_MIN, POST_MIN = 60, 60   # новые входы запрещены за 60 минут до и 60 минут после важного события (инфляция, ставка ФРС, занятость)
FULL_RU = [  # точные названия показателей (Faireconomy) -> полностью по-русски; проверяются раньше общих шаблонов
    (r"^CB Consumer Confidence", "Индекс доверия потребителей (Conference Board)"), (r"^CB Leading Index", "Опережающий индекс (Conference Board)"),
    (r"Michigan Consumer Sentiment|UoM Consumer Sentiment", "Настроения потребителей (Мичиганский университет)"), (r"Michigan Inflation Expectations|UoM Inflation Expectations", "Инфляционные ожидания (Мичиганский университет)"),
    (r"ADP Weekly Employment", "недельная занятость по данным ADP"), (r"ADP Non-Farm", "занятость в частном секторе по данным ADP"), (r"API Weekly Statistical", "недельный отчёт API по запасам нефти"),
    (r"Core Durable Goods", "заказы на товары длительного пользования без транспорта"), (r"Durable Goods", "заказы на товары длительного пользования"), (r"Advance GDP Price Index", "дефлятор ВВП (первая оценка)"),
    (r"Advance GDP", "ВВП (первая оценка)"), (r"Prelim GDP|Flash GDP", "ВВП (предварительная оценка)"), (r"Chicago PMI", "деловая активность в Чикаго"), (r"Crude Oil Inventories", "запасы нефти в США"),
    (r"Natural Gas Storage", "запасы природного газа в США"), (r"Employment Cost Index", "индекс стоимости рабочей силы"), (r"Goods Trade Balance", "торговый баланс товаров"),
    (r"S&P/CS Composite-20 HPI", "цены на жильё в 20 городах (Кейс-Шиллер)"), (r"\bHPI\b", "цены на жильё"), (r"New Home Sales", "продажи новых домов"), (r"Existing Home Sales", "продажи домов на вторичном рынке"),
    (r"Pending Home Sales", "отложенные продажи домов"), (r"Housing Starts", "начатое строительство жилья"), (r"Building Permits", "разрешения на строительство"), (r"Personal Income", "личные доходы"), (r"Personal Spending", "личные расходы"),
    (r"Wholesale Inventories", "запасы оптовых торговцев"), (r"Richmond Manufacturing", "промышленный индекс ФРБ Ричмонда"), (r"Philly Fed|Philadelphia Fed", "промышленный индекс ФРБ Филадельфии"),
    (r"Empire State", "промышленный индекс Нью-Йорка"), (r"JOLTS", "число вакансий (JOLTS)"), (r"Import Prices", "цены на импорт"), (r"Industrial Production", "промышленное производство"),
    (r"Mortgage Approvals", "одобренные ипотечные кредиты"), (r"Net Lending to Individuals", "чистое кредитование населения"), (r"Nationwide HPI", "цены на жильё (Nationwide)"), (r"BRC Shop Price", "цены в магазинах (BRC)"),
    (r"CBI Realized Sales", "фактические продажи (CBI)"), (r"M[34] Money Supply", "денежная масса"), (r"Annual Budget Release", "годовой бюджет"), (r"Autumn Forecast Statement", "осенний прогноз бюджета"),
    (r"Private Sector Credit", "кредит частному сектору"), (r"Private Loans", "кредиты частному сектору"), (r"Overnight Rate|Main Refinancing Rate|Official Cash Rate|Policy Rate|Cash Rate", "решение по ключевой ставке"),
    (r"Monetary Policy Statement|Rate Statement", "заявление по денежной политике"), (r"Monetary Policy Report", "доклад по денежной политике"), (r"Press Conference", "пресс-конференция центробанка"),
    (r"Outlook Report", "доклад центробанка о перспективах"), (r"Speaks$|Speech$|Testifies$", "выступление представителя центробанка"), (r"Bond Auction", "аукцион облигаций"), (r"Bank Holiday", "выходной день банков"), (r"KOF Economic Barometer", "экономический барометр KOF"),
    (r"UBS Economic Expectations", "экономические ожидания (UBS)"), (r"GfK Consumer Climate", "потребительский климат (GfK)"), (r"ifo Business Climate", "деловой климат (ifo)"), (r"Consumer Spending", "потребительские расходы"),
    (r"Private Payrolls", "занятость в частном секторе"), (r"Unemployment Change", "изменение числа безработных"), (r"ANZ Business Confidence", "деловая уверенность (ANZ)"), (r"Business Confidence", "деловая уверенность"),
    (r"Consumer Confidence", "доверие потребителей"), (r"SPPI", "цены производителей услуг"), (r"Manufacturing PMI", "деловая активность в промышленности (PMI)"), (r"Non-Manufacturing PMI|Services PMI", "деловая активность в услугах (PMI)")]

TITLES_RU = [  # порядок важен: более длинные шаблоны раньше
    (r"FOMC Press Conference", "пресс-конференция ФРС"), (r"FOMC Statement", "решение ФРС по ставке"), (r"Federal Funds Rate", "решение ФРС по ставке"),
    (r"FOMC Meeting Minutes", "протокол заседания ФРС"), (r"Core CPI", "базовая инфляция (цены для потребителей)"), (r"\bCPI\b", "инфляция (цены для потребителей)"), (r"Core PPI", "базовые цены производителей"),
    (r"\bPPI\b", "цены производителей"), (r"Non-Farm Employment Change", "занятость в США (вне сельского хозяйства)"), (r"Unemployment Rate", "уровень безработицы"),
    (r"Unemployment Claims", "заявки на пособие по безработице"), (r"Core Retail Sales|Retail Sales", "розничные продажи"), (r"GDP", "ВВП"), (r"ISM", "деловая активность в промышленности"),
    (r"Core PCE", "базовая инфляция (расходы потребителей)"), (r"PCE", "инфляция (расходы потребителей)"), (r"Powell", "выступление главы ФРС"), (r"FOMC Member", "выступление члена ФРС"),
    (r"Fed Chair", "выступление главы ФРС"), (r"Treasury Currency Report", "отчёт Минфина США")]


BANKS_RU = [(r"BOE|Bailey|MPC", "Банк Англии"), (r"ECB|Lagarde", "ЕЦБ"), (r"BOJ|Ueda", "Банк Японии"), (r"BOC", "Банк Канады"), (r"RBNZ", "Резервный банк Новой Зеландии"), (r"RBA", "Резервный банк Австралии"), (r"SNB", "Нацбанк Швейцарии")]
EXTRA_RU = [(r"BOE|Bailey|MPC", "Банк Англии"), (r"ECB|Lagarde", "ЕЦБ"), (r"BOJ|Ueda", "Банк Японии"), (r"\bPMI\b", "деловая активность (PMI)"), (r"Retail Sales", "розничные продажи"),
            (r"Employment|Earnings|Claimant", "рынок труда"), (r"GDP", "ВВП"), (r"CPI|Inflation", "инфляция"), (r"Consumer Sentiment|Confidence", "настроения потребителей"), (r"Trade Balance", "торговый баланс")]


def title_ru(ev: dict) -> str:
    """Название события по-русски. USD-события — как в TITLES_RU; события других валют — «название (валюта)»; неизвестное — исходное название + валюта (а не безымянное «событие (GBP)»)."""
    if ev.get("kind") != "macro":
        return ev["title"]
    ccy = ev.get("ccy") or ""
    tail = f" ({ccy})" if ccy and ccy != "USD" else ""
    for pat, ru in TITLES_RU:
        if re.search(pat, ev["title"], re.I):
            return ru + tail
    for pat, ru in FULL_RU:
        if re.search(pat, ev["title"], re.I):
            bank = next((b for p2, b in BANKS_RU if re.search(p2, ev["title"])), "")   # «ECB Press Conference» -> «ЕЦБ: пресс-конференция центробанка»
            return (f"{bank}: {ru}" if bank else ru) + tail
    for pat, ru in EXTRA_RU:
        if re.search(pat, ev["title"], re.I):
            return f"{ru}: {ev['title']}{tail}"
    return f"{ev['title']}{tail}"


def _key(e: dict) -> str:
    return hashlib.md5(f"{e['ts']}|{e['title']}|{e.get('assets', '')}".encode()).hexdigest()[:16]


def _dt(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def dedup(rows: list[dict]) -> list[dict]:
    """Одно событие — одна строка: одинаковые время + валюта + русское название (CPI m/m, Median CPI, Trimmed CPI в одну минуту; Flash Manufacturing/Services PMI) схлопываются,
    остаётся запись с наибольшей важностью. Порядок по времени сохраняется."""
    rank = {"High": 2, "Medium": 1}
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["ts"], r.get("ccy"), r.get("kind"), title_ru(r) if r.get("kind") == "macro" else r["title"])
        if k not in best or rank.get(r["impact"], 0) > rank.get(best[k]["impact"], 0):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["ts"])


def refresh(board, symbols=()) -> int:
    """Обновить таблицу событий из источников; возвращает число событий в таблице на ближайшие 3 дня.
    Записи источника, которых больше нет в свежей выгрузке (событие перенесли; у отчётов компаний сменилась дата), удаляются — иначе накапливаются дубли."""
    evs = sources.faireconomy_events() + sources.deribit_expiry() + sources.earnings_events([s for s in symbols if "/" not in s])
    now_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(timespec="minutes")
    for src in {e["src"] for e in evs}:
        keep = {_key(e) for e in evs if e["src"] == src}
        for r in board.q("SELECT key FROM events WHERE src=? AND ts>=?", (src, now_iso)):   # без NOT IN (?,…): лимит переменных SQLite
            if r["key"] not in keep:
                board.x("DELETE FROM events WHERE key=?", (r["key"],))
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
    rows = dedup(board.q("SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts", (lo, hi)))
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


def picture_bans(d: dict, now: dt.datetime | None = None) -> dict[str, dict]:
    """Действующие запреты из «Картины дня»: актив -> {reason, until}. Без причины/срока (старый формат) или с истёкшим сроком запрета нет."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {}
    for e in d.get("avoid_assets") or []:
        if isinstance(e, dict) and e.get("asset") and e.get("reason") and e.get("until"):
            try:
                if _dt(e["until"]) > now:
                    out[e["asset"]] = e
            except ValueError:
                pass
    return out
