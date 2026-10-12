"""Сообщения для Telegram (этап 4): статус, позиции, картина дня, брифинг, вечерний и недельный отчёты. Всё по-русски, без кодов и id;
итоговый текст дополнительно проходит вычитку (report.notify -> proofread). Цифры счёта — только из Alpaca (view = AlpacaClient.account_view())."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import config, events, regime, shadow, weights
from .report import ROLE_RU, name, num, pct, plural, pos_name, price, signed, usd

REGIME_RU = {"risk_on": "риск включён (деньги идут в рискованные активы)", "risk_off": "риск выключен (рынок осторожничает)", "neutral": "нейтральный"}
ROLES_ALL = {**ROLE_RU, "bull": "Бык", "bear": "Медведь", "trader": "Трейдер", "risk_manager": "Риск-менеджер", "portfolio_manager": "Управляющий"}


def local(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts)
    t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(ZoneInfo(config.TZ_NAME))


def _tz_hhmm(ts: str) -> str:
    return f"{local(ts):%H:%M}"


def events_lines(evs: list[dict], limit: int = 8) -> list[str]:
    out = []
    for e in evs[:limit]:
        star = "‼️" if e["impact"] == "High" else "•"
        out.append(f"{star} {local(e['ts']):%d.%m %H:%M} — {events.title_ru(e)}")
    return out


def picture_text(pic: dict | None, reg: dict | None = None) -> str:
    if not pic:
        return "Свежей «Картины дня» пока нет — исследовательский отдел её готовит."
    d = pic["data"]
    lines = [f"🗺 Картина дня (обновлена в {_tz_hhmm(pic['ts'])})", f"Режим рынка: {REGIME_RU.get(d.get('regime'), 'нейтральный')}"]
    if reg:
        parts = [f"{t} — {regime.RU[reg[k]['regime']]}" for k, t in (("crypto", "крипта"), ("stock", "акции")) if reg.get(k)]
        if parts:
            lines.append("По графикам: " + ", ".join(parts))
    lines.append(d.get("summary", ""))
    if d.get("main_risks"):
        lines += ["Главные риски:"] + [f"• {r}" for r in d["main_risks"]]
    if d.get("priority_assets"):
        lines.append("В приоритете: " + ", ".join(name(a) for a in d["priority_assets"]))
    if d.get("forbidden") or d.get("avoid_assets"):
        lines.append("Сегодня нельзя:")
        lines += [f"• {f}" for f in d.get("forbidden", [])]
        if d.get("avoid_assets"):
            lines.append("• покупать: " + ", ".join(name(a) for a in d["avoid_assets"]))
    sm = d.get("size_mult", 1.0)
    lines.append("Новые покупки запрещены" if d.get("no_new_entries") else f"Размер новых позиций: {num(sm * 100)}% от обычного" if sm < 1 else "Новые покупки разрешены в обычном размере")
    if d.get("mechanical"):
        lines.append("Картина составлена по правилам: аналитики были недоступны.")
    return "\n".join(l for l in lines if l)


def positions_text(view: dict, jpos: dict) -> str:
    if not view["positions"]:
        return "Открытых позиций нет."
    lines = ["📌 Открытые позиции"]
    for p in view["positions"]:
        n = pos_name(p["symbol"])
        full = next((k for k in jpos if name(k) == n), None)
        j = jpos.get(full, {})
        lines.append(f"• {n}: вход {price(p['avg'])}, сейчас {price(p['price'])}, результат {pct(p['plpc'])} ({signed(p['pl'])} $)"
                     + (f", стоп {price(j['stop'])}" if j.get("stop") else "") + (f", цель {price(j['take'])}" if j.get("take") else ""))
    return "\n".join(lines)


def status_text(view: dict, paused: bool, reg: dict | None, pic: dict | None, labst: dict, trades_today: int, bench: dict, real: str = "") -> str:
    day = view["equity"] / view["last_equity"] - 1 if view["last_equity"] else 0.0
    parts = [f"{t} — {regime.RU[reg[k]['regime']]}" for k, t in (("crypto", "крипта"), ("stock", "акции")) if reg and reg.get(k)]
    lines = ["📋 Состояние офиса", "Работа: на паузе (новые покупки запрещены, позиции под защитой)" if paused else "Работа: идёт, торговля разрешена",
             f"Счёт: {usd(view['equity'])} ({pct(day, 2)} за день), свободно {usd(view['cash'])}",
             f"Для сравнения за день: S&P 500 {pct(bench.get('SPY', 0))}, биткоин {pct(bench.get('BTC', 0))}",
             f"Открытых позиций: {len(view['positions'])}, покупок за сутки: {trades_today}"]
    if parts:
        lines.append("Режим рынка: " + ", ".join(parts))
    if pic:
        lines.append(f"Картина дня: {REGIME_RU.get(pic['data'].get('regime'), 'нейтральный')} (обновлена в {_tz_hhmm(pic['ts'])})")
    lines.append(f"Лаборатория: на испытании {labst.get('trial', 0)}, подтверждено вживую {labst.get('live', 0)}")
    if real:
        lines.append(real)
    return "\n".join(lines)


def compare_line(rows: list[dict]) -> str:
    """«С начала наблюдения: офис +1,2%, S&P 500 +0,5%, биткоин −2,0%» по теневым портфелям."""
    by = {r["name"]: r for r in rows}
    if "office" not in by:
        return ""
    bits = [f"офис {pct(by['office']['ret'], 2)}"] + [f"{t} {pct(by[k]['ret'], 2)}" for k, t in (("spy", "S&P 500"), ("btc", "биткоин")) if k in by]
    return "С начала наблюдения: " + ", ".join(bits)


def shadow_text(rows: list[dict]) -> str:
    if not rows:
        return "Теневые портфели ещё не начали считаться."
    return "Теневые портфели (с начала наблюдения):\n" + shadow.table_text(rows)


def briefing_text(pic, reg, evs, view, jpos, cands) -> str:
    lines = ["☀️ Утренний брифинг", picture_text(pic, reg), ""]
    lines += ["События на ближайшие сутки:"] + (events_lines(evs) or ["• значимых событий нет"])
    lines += ["", "План на день:", f"• открытых позиций: {len(view['positions'])}, свободных денег {usd(view['cash'])}"]
    if cands:
        lines.append("• присматриваемся к: " + ", ".join(name(c) for c in cands[:5]))
    lines.append("• сопровождение позиций — стоп подтягивается автоматически")
    return "\n".join(lines)


def evening_text(view, bench, trades_today: list[dict], opened_today: int, shadow_rows, lessons: list[str], evs, labst) -> str:
    day = view["equity"] / view["last_equity"] - 1 if view["last_equity"] else 0.0
    lines = ["🌙 Вечерний отчёт", f"Счёт: {usd(view['equity'])} ({pct(day, 2)} за день)", f"S&P 500 {pct(bench.get('SPY', 0))}, биткоин {pct(bench.get('BTC', 0))} за день",
             f"Покупок за день: {opened_today}, закрыто сделок: {len(trades_today)}"]
    for t in trades_today[:5]:
        lines.append(f"• {name(t['asset'])}: {signed(t['pnl'])} $ ({'по стопу' if t['exit_reason'] == 'stop' else 'по цели' if t['exit_reason'] == 'take' else 'по решению офиса'})")
    lines += ["", shadow_text(shadow_rows)]
    if lessons:
        lines += ["", "Уроки дня:"] + [f"• {l}" for l in lessons[:3]]
    if evs:
        lines += ["", "Завтра в календаре:"] + events_lines(evs, 4)
    lines += ["", f"Лаборатория: на испытании {labst.get('trial', 0)}, подтверждено вживую {labst.get('live', 0)}"]
    return "\n".join(lines)


def weekly_text(week_rows, trades: list[dict], lessons: list[str], acc: list[dict], props: list[dict], labst, insight: str = "", real: str = "") -> str:
    lines = ["📅 Еженедельное собрание офиса"]
    if insight:
        lines += [insight, ""]
    lines += [shadow_text(week_rows) if week_rows else "Теневые портфели ещё накапливают данные."]
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    lines += ["", f"Сделок закрыто за неделю: {n}" + (f", прибыльных {wins}, итог {signed(sum(t['pnl'] for t in trades))} $" if n else "")]
    if lessons:
        lines += ["Уроки недели:"] + [f"• {l}" for l in lessons[:5]]
    ok = [a for a in acc if a["role"] in ROLES_ALL and a["n"] >= weights.MIN_N]
    if ok:
        lines += ["", "Точность ролей (мнения, подтвердившиеся ходом цены):"] + [f"• {ROLES_ALL[a['role']]}: {num(a['accuracy'] * 100)}% на {a['n']} {plural(a['n'], 'оценке', 'оценках', 'оценках')}" for a in ok]
    lines.append(f"Лаборатория: на испытании {labst.get('trial', 0)}, подтверждено вживую {labst.get('live', 0)}, не прошли испытание {labst.get('failed', 0)}")
    if real:
        lines.append(real)
    if props:
        lines += ["", "Предложения (меняются только с вашего согласия):"] + [f"{i}) {p['text']}" for i, p in enumerate(props, 1)]
        lines.append("Ответьте «да» — применить все, «да 1» — только первое, «нет» — отклонить.")
    else:
        lines += ["", "Предложений по изменениям на этой неделе нет."]
    return "\n".join(lines)
