"""Уведомления в Telegram — только по-русски, для человека, не программиста: без кодов ролей, id, английских слов и обрывков.
Три вида: сделка (покупка), закрытие (стоп-лосс / тейк-профит / решение трейдера), сводка раз в 6 часов + служебные сообщения.
Причины («почему купили») пишутся одной короткой фразой на роль: Gemini → запасная фраза; ответ проверяется на латиницу."""
import datetime as dt
import re
import subprocess

from . import config, proofread, telegram
from .config import COSTS, make_asset
from .llm import LLMError, RoleRouter
from .risk import RiskGate

ROLE_RU = {"market": "Технический анализ", "news": "Новости", "fundamentals": "Фундаментальный анализ", "quant": "Квант-аналитик",
           "buffett": "Баффетт", "munger": "Мангер", "druckenmiller": "Друкенмиллер", "lab": "Лаборатория стратегий", "smartmoney": "Умные деньги"}
ACC_RU = {**ROLE_RU, "bull": "Бык", "bear": "Медведь", "trader": "Трейдер"}
STANCE_RU = {"buy": "за", "sell": "против", "hold": "сомневается"}
FAMILY_RU = {"sma_cross": "пересечение средних", "rsi_revert": "возврат после перепроданности", "donchian": "пробой диапазона", "macd": "смена импульса",
             "bb_revert": "возврат от нижней границы", "bb_breakout": "выход за верхнюю границу", "adx_trend": "сильный тренд",
             "stoch_revert": "возврат после падения", "atr_breakout": "выход из канала волатильности", "roc_momentum": "устойчивый рост",
             "ema_pullback": "откат в растущем тренде"}
TECH_PHRASE = {"lab": "стратегия, прошедшая лабораторию", "trend": "растущий тренд, импульс подтверждает", "bounce": "отскок от перепроданности при целом долгом тренде",
               "support": "цена у поддержки (минимум последних дней), отскок против тренда", "revert": "цена сильно ниже средней, ожидаем возврат к ней (против тренда)"}
MINUS = "−"
LATIN = re.compile(r"[A-Za-z]")


# ---------- форматирование ----------
def num(x: float, d: int = 0) -> str:
    """15020 -> '15 020', 0.517 -> '0,517' (пробел в тысячах, запятая — десятичная)."""
    s = f"{abs(x):,.{d}f}".replace(",", "\u00a0").replace(".", ",")
    return (MINUS if x < 0 and float(f"{abs(x):.{d}f}") != 0 else "") + s


def signed(x: float, d: int = 0) -> str:
    s = num(x, d)
    return s if s.startswith(MINUS) or float(f"{abs(x):.{d}f}") == 0 else "+" + s


def usd(x: float, d: int = 0) -> str:
    return f"{num(x, d)} $"


def price(x: float) -> str:
    d = 2 if x >= 100 else 3 if x >= 0.1 else 5
    return usd(x, d)


def pct(x: float, d: int = 1) -> str:
    return f"{signed(x * 100, d)}%"


def pos_name(sym: str) -> str:
    """Символ позиции Alpaca (ONDOUSD / AAPL) -> короткое имя."""
    return sym[:-3] if sym.endswith("USD") and len(sym) > 5 else sym


def utcnow_day() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')


def name(asset: str) -> str:
    return asset.split("/")[0]


# ---------- фразы по-русски ----------
PHRASE_SYSTEM = """Ты редактор уведомлений торговой фирмы. На вход — список пунктов с исходным текстом (часто на английском, с профессиональными терминами).
Для КАЖДОГО пункта напиши ОДНУ короткую законченную фразу по-русски (до 12 слов, со строчной буквы, с запятыми по правилам русского языка, без точки в конце), понятную человеку, который не программист и не трейдер.
Строгие правила: только русские слова — никаких английских слов и латинских букв, названий индикаторов (RSI, MACD, ADX, EMA, ATR, Sharpe и т.п.) и сокращений: объясняй простыми словами
(«перекупленность», «рост выше средних значений», «высокая изменчивость цены»). Без номеров отчётов, без «#», без кодов ролей, без кавычек и многоточий. Не обрывай фразу. Не пересказывай цифры без нужды.
Пункт «debate»: фраза вида «бык победил — причина» или «медведь привёл веские доводы, но тренд сильнее» — кто сильнее в споре (бык — покупатель, медведь — продавец) и почему.
Пункты «risk» и «manager»: начинай с глагола прошедшего времени мужского рода — что сделал риск-менеджер / управляющий («уменьшил размер позиции из-за высокой изменчивости», «одобрил, риск приемлем»). Аналитические пункты начинай с сути («рост выше всех средних, тренд сильный»), без слов «аналитик считает».
Ответ — один JSON-объект: {"phrases": {"<ключ пункта>": "<фраза>", ...}} с теми же ключами, что на входе."""


def _valid(p) -> bool:
    return isinstance(p, str) and 3 <= len(p) <= 120 and len(p.split()) <= 16 and not LATIN.search(p) and "#" not in p and "…" not in p and "..." not in p


def phrases(items: dict[str, str]) -> dict[str, str]:
    """{ключ: исходный текст} -> {ключ: русская фраза}. Gemini → запасные модели (Groq/Cerebras/Mistral/OpenRouter). Латиница в ответе = брак.
    Ключи без пригодной фразы в результат НЕ попадают — шаблонных «оценка по графику цены» вместо мнения больше нет, вызывающий пишет прямо, что пересказ недоступен."""
    if not items:
        return {}
    payload = {"items": {k: v[:400] for k, v in items.items()}}
    out: dict[str, str] = {}
    try:
        got = RoleRouter().ask("notify", payload, system=PHRASE_SYSTEM).get("phrases", {})
    except (LLMError, AttributeError):
        got = {}
    for k, v in got.items():
        if k in items and k not in out and _valid(v):
            v = v.strip().rstrip(".").strip()
            out[k] = v[:1].lower() + v[1:]
    return out


# ---------- сообщения ----------
def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def trade_message(board, res) -> str:
    if res.status == "closed":
        return close_message(res.asset, res.order.get("pnl", 0.0), res.order.get("pnl_pct", 0.0), "signal")
    o = res.order
    entry, stop, take, qty = o["price"], o["stop"], o["take"], o["qty"]
    kind = make_asset(res.asset).kind
    notional, risk, eq = entry * qty, RiskGate.planned_risk(COSTS[kind], entry, stop, qty), o.get("equity") or 0
    reps = board.cycle_reports(res.cycle_id, res.asset)
    last = {}
    for r in reps:
        last[r["role"]] = r
    items, order = {}, []
    for role in ROLE_RU:
        r = last.get(role)
        if r and (r["data"].get("stance") in STANCE_RU):
            items[role] = r["verdict"]
            order.append(role)
    ph = phrases(items)
    dec = last.get("decision", {}).get("data", {})
    why = [f"• {ROLE_RU[r]}: {ph[r]} ({STANCE_RU[last[r]['data']['stance']]})" if r in ph else
           f"• {ROLE_RU[r]}: {STANCE_RU[last[r]['data']['stance']]} (уверенность {num(last[r]['confidence'] * 100)}%) — краткий пересказ недоступен: лимит моделей" for r in order]
    why.insert(0, "• Технический сигнал: " + TECH_PHRASE.get(o.get("tech"), "растущий тренд, импульс подтверждает"))
    if o.get("mode") == "tech_only":
        why.insert(1, "• Аналитики не одобрили вход, но запрета от совета нет — вход только по техническому сигналу")
    if o.get("half"):
        why.append("• Размер позиции уменьшен вдвое: " + "; ".join(o.get("gaps") or dec.get("gaps") or ["данные неполные"]))
    lines = [f"🟢 Покупка {name(res.asset)} (демо-счёт)", *([f"Отдел: {o['desk']}"] if o.get("desk") else []), f"Цена входа: {price(entry)}", f"Стоп-лосс: {price(stop)} ({pct(stop / entry - 1)})",
             f"Тейк-профит: {price(take)} ({pct(take / entry - 1)})",
             f"Сумма: {usd(notional)} · риск с комиссиями: {usd(risk)}" + (f" ({num(risk / eq * 100, 1)}% счёта)" if eq else ""), "", "Почему купили:"] + why
    return "\n".join(lines)


def close_message(asset: str, pnl: float, pnl_pct: float, reason: str, offline: bool = False, planned_risk: float | None = None, desk: str | None = None) -> str:
    if offline and reason in ("stop", "take"):
        head = (f"🔴 Продажа {name(asset)} — сработал стоп-лосс (пока офис был выключен)" if reason == "stop"
                else f"🟢 Продажа {name(asset)} — взят тейк-профит (пока офис был выключен)")
    elif reason == "stop":
        head = f"🔴 Продажа {name(asset)} — сработал стоп-лосс"
    elif reason == "take":
        head = f"🟢 Продажа {name(asset)} — взят тейк-профит"
    elif reason == "manual":
        head = f"{'🟢' if pnl >= 0 else '🔴'} Продажа {name(asset)} — по вашей команде"
    else:
        head = f"{'🟢' if pnl >= 0 else '🔴'} Продажа {name(asset)} — по решению трейдера"
    out = f"{head}\n" + (f"Отдел: {desk}\n" if desk and desk != "без отдела" else "") + f"Результат: {signed(pnl)} $ ({pct(pnl_pct)})"
    if reason == "stop" and planned_risk and planned_risk > 0 and pnl < 0:
        over = -pnl / planned_risk - 1
        out += f"\nЗаложенный риск с комиссиями: {usd(planned_risk)}; факт {usd(-pnl)}" + (f" — на {num(over * 100)}% больше заложенного" if over > 0.10 else " — в пределах заложенного")
    return out


def plural(n: int, one: str, few: str, many: str) -> str:
    return one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def lab_line(st: dict, appr=None) -> str:
    n = st["crypto"]["n"] + st["stock"]["n"]
    if not n:
        return "Лаборатория: за сутки новых проверок нет"
    ok = st.get("strict", st["crypto"].get("ok", 0) + st["stock"].get("ok", 0))   # строгая проверка = approved + поправка на множественное тестирование
    return (f"Лаборатория: проверено {n} {plural(n, 'стратегия', 'стратегии', 'стратегий')} (крипта {st['crypto']['n']}, акции {st['stock']['n']}); "
            f"прошли строгую проверку: {ok}; на испытании: {st.get('trial', 0)}; подтверждены вживую: {st.get('live', 0)}")


def summary_message(view: dict, bench: dict, trades_today: int, lab_text: str, now=None, title: str = "Сводка офиса", day_line: str | None = None) -> str:
    """Все цифры счёта — из Alpaca (view = AlpacaClient.account_view()): equity, cash, last_equity, unrealized_pl по позициям."""
    now = now or config.localnow()
    eq, last = view["equity"], view["last_equity"]
    pos = ", ".join(f"{pos_name(p['symbol'])} {pct(p['plpc'])} ({signed(p['pl'])} $)" for p in view["positions"]) or "нет"
    day = eq / last - 1 if last else 0.0
    return "\n".join([f"📊 {title} на {now:%H:%M}", f"Счёт: {usd(eq)} ({pct(day, 2)} за день)", f"Свободные деньги: {usd(view['cash'])}",
                      f"Для сравнения: S&P 500 {pct(bench.get('SPY', 0))}, Биткоин {pct(bench.get('BTC', 0))}",
                      f"Открытые позиции: {pos}", day_line or f"Покупок за день: {trades_today}", lab_text])


def service(kind: str, asset: str = "", detail: str = "") -> str:
    """Служебные сообщения без технических деталей."""
    a = name(asset) if asset else ""
    return {"start": "▶️ Торговый зал запущен (демо-счёт)",
            "asset_fail": f"⚠️ {a}: не удалось обработать актив в этом цикле (сбой данных или ответа аналитиков). Покупок и продаж по нему не было.",
            "task_fail": f"⚠️ Плановое задание «{detail}» завершилось с ошибкой. Офис продолжает работу, подробности в журнале.",
            "halt_daily": "⛔ Дневной стоп достигнут: новые покупки заблокированы до следующих суток."}[kind]


def manage_message(kind: str, asset: str, **k) -> str:
    n = name(asset)
    if kind == "stop":
        why = " и ".join("безубыток" if w == "безубыток" else "подтяжка за ценой" for w in k["why"] if w in ("безубыток", "подтяжка за ценой")) or "защита прибыли"
        side = "выше цены входа" if k["pct"] > 0 else "ниже цены входа"
        return f"🛡 {n}: стоп-лосс поднят до {price(k['price'])} ({pct(k['pct'])} {side}) — {why}"
    if kind == "partial":
        return f"🟢 {n}: продана половина позиции по {price(k['price'])}, зафиксировано {signed(k['pnl'])} $. Остаток под стопом {price(k['stop'])}"
    return f"{'🟢' if k['pnl'] >= 0 else '🔴'} Продажа {n} — цена стояла 48 часов\nРезультат: {signed(k['pnl'])} $ ({pct(k['pnl_pct'])})"


def urgent_message(asset: str, note: str) -> str:
    return f"⚡ Срочная новость по {name(asset)}: {note}"


def panic_message(market: str, why: list[str]) -> str:
    return f"🚨 Паника на {market}: {'; '.join(why)}. Новые покупки запрещены, пока режим не сменится."


def notify(text: str, proof: bool = True) -> None:
    """Вычитка (Gemini) -> Bot API (telegram.py читает тот же конфиг, что tgnotify); при сбое — сам tgnotify."""
    if proof:
        text = proofread.proofread(text)
    if not telegram.notify(text):
        try:   # запасной канал (Termux); в облаке утилиты нет — просто молчим, ошибка отправки не должна ронять проход
            subprocess.run(["tgnotify", text[:3800]], capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            print("telegram: сообщение не отправлено", flush=True)


def chain_text(board, cycle_id: str, asset: str) -> str:
    """Для консоли/отладки (не для Telegram): все отчёты цикла со ссылками."""
    out = []
    for r in board.cycle_reports(cycle_id, asset):
        refs = ", ".join(f"#{i}{'+' if r['stances'].get(str(i)) == 'agree' else '-'}" for i in r["refs"])
        out.append(f"#{r['id']} {r['role']} [{r['confidence']:.2f}]: {r['verdict'][:110]}" + (f"\n   -> {refs}" if refs else ""))
    return "\n".join(out)
