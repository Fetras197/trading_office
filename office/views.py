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


def when_text(ts: str, now: dt.datetime | None = None) -> str:
    """Срок с датой, если не сегодня: «до 10:21» при обновлении в 10:21 читалось как нулевой запрет, хотя срок — сутки (до 10:21 завтра)."""
    t, n = local(ts), (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(config.TZ_NAME))
    days = (t.date() - n.date()).days
    return f"{t:%H:%M}" if days == 0 else f"завтра {t:%H:%M}" if days == 1 else f"{t:%d.%m %H:%M}"


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
    bans = events.picture_bans(d)
    if d.get("forbidden") or bans:
        lines.append("Сегодня нельзя:")
        lines += [f"• {f}" for f in d.get("forbidden", [])]
        lines += [f"• не покупать {name(a)} до {when_text(e['until'])} — {e['reason']}" for a, e in bans.items()]
    sm = d.get("size_mult", 1.0)
    lines.append("Новые покупки запрещены" if d.get("no_new_entries") else f"Размер новых позиций: {num(sm * 100)}% от обычного" if sm < 1 else "Новые покупки разрешены в обычном размере")
    if d.get("data_sources"):
        lines.append("Данные и источники: " + d["data_sources"])
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


STAGE_RU = {"signal": "нет сигнала", "votes": "аналитики", "limits": "лимит риска", "regime": "режим рынка", "event": "событие", "cooldown": "остывание после стопа",
            "halt": "пауза", "alts": "лимит альткоинов", "riskgate": "риск-гейт", "desk": "отделы", "economics": "экономика сделки", "spread": "спред", "broker": "брокер", "data": "нет данных"}


def desk_lines(rows: list[dict], title: str) -> list[str]:
    """Отчёт по отделам: бюджет, позиции, сделки, прибыль после комиссий и комиссии — по каждому отделу отдельно."""
    if not rows:
        return []
    out = [title]
    tot_pnl = tot_fees = tot_tr = tot_pt = 0
    for r in rows:
        tot_pnl += r["pnl"]
        tot_fees += r["fees"]
        tot_tr += r["trades"]
        tot_pt += r.get("partials", 0)
        if r["status"] != "active":
            out.append(f"• {r['name']}: закрыт" + (f" — {r['trades']} сделок за период, {signed(r['pnl'])} $" if r["trades"] else ""))
            continue
        b = f"{num(r['share'] * 100)}%" + (f" ({usd(r['budget'])})" if r.get("budget") else "")
        win = (f", в плюс {r['wins']}" if r["trades"] else "") + (f"; частичных фиксаций {r['partials']}" if r.get("partials") else "")
        un = f", не закрыто {signed(r['unrealized'])} $" if r["open"] else ""
        out.append(f"• {r['name']}: бюджет {b}; открыто {r['open']} из 3; закрыто {r['trades']} {plural(r['trades'], 'сделка', 'сделки', 'сделок')}{win}; "
                   f"прибыль после комиссий {signed(r['pnl'])} $ (комиссии {num(r['fees'], 2)} $){un}")
    out.append(f"Итого по отделам: закрыто {tot_tr} {plural(tot_tr, 'сделка', 'сделки', 'сделок')}" + (f", частичных фиксаций {tot_pt}" if tot_pt else "")
               + f"; результат {signed(tot_pnl)} $ после комиссий {num(tot_fees, 2)} $ (резерв 20% не торгуется)")
    return out


def nogo_lines(rows: list[dict], stock_open: bool | None = None) -> list[str]:
    """«Почему не вошли»: 3 ближайших к входу кандидата за последние 24 часа и что их заблокировало (rows = Journal.nogo_top). Крипта торгуется круглосуточно — сканер и оценка работают всегда."""
    out = []
    if not rows:
        out.append("🔎 Почему не вошли: за последние 24 часа оценённых кандидатов нет (проходов офиса ещё не было — первые появятся в ближайшие минуты).")
    else:
        out.append("🔎 Почему не вошли (ближайшие кандидаты за последние 24 часа):")
        for i, r in enumerate(rows, 1):
            out.append(f"{i}) {name(r['asset'])} — {STAGE_RU.get(r['stage'], r['stage'])}: {r['reason']}")
    if stock_open is False:
        out.append("Акции и ETF сейчас не оцениваются: биржа США закрыта (отдел «Акции США» работает в её часы); крипта оценивается круглосуточно.")
    return out


def evening_text(view, bench, trades_today: list[dict], opened_today: int, shadow_rows, lessons: list[str], evs, labst, nogo: list[dict] | None = None, rules_line: str = "", desk_rows: list[dict] | None = None, stock_open: bool | None = None) -> str:
    day = view["equity"] / view["last_equity"] - 1 if view["last_equity"] else 0.0
    lines = ["🌙 Вечерний отчёт", f"Счёт: {usd(view['equity'])} ({pct(day, 2)} за день)", f"S&P 500 {pct(bench.get('SPY', 0))}, биткоин {pct(bench.get('BTC', 0))} за день",
             f"Покупок за день: {opened_today}, закрыто сделок: {sum(1 for t in trades_today if t['exit_reason'] != 'partial')}"
             + (f", частичных фиксаций: {sum(1 for t in trades_today if t['exit_reason'] == 'partial')}" if any(t['exit_reason'] == 'partial' for t in trades_today) else ""),
             f"Реализованный результат за день: {signed(sum(t['pnl'] for t in trades_today))} $ (закрытые сделки и частичные фиксации, после комиссий)"]
    for t in trades_today[:8]:
        why = {"stop": "по стопу", "take": "по цели", "partial": "частичная фиксация"}.get(t["exit_reason"], "по решению офиса")
        lines.append(f"• {name(t['asset'])}: {signed(t['pnl'])} $ ({why})")
    if desk_rows:
        lines += [""] + desk_lines(desk_rows, "🏢 Отделы за день:")
    if nogo is not None:
        lines += [""] + nogo_lines(nogo, stock_open)
    lines += ["", shadow_text(shadow_rows)]
    if lessons:
        lines += ["", "Уроки дня (только запись в журнал, правила от них не меняются):"] + [f"• {l}" for l in lessons[:3]]
    if rules_line:
        lines += ["", rules_line]
    if evs:
        lines += ["", "Завтра в календаре:"] + events_lines(evs, 4)
    if labst.get("trial") or labst.get("live"):
        lines += ["", f"Лаборатория: на испытании {labst.get('trial', 0)}, подтверждено вживую {labst.get('live', 0)}"]
    return "\n".join(lines)


def weekly_text(week_rows, trades: list[dict], lessons: list[str], acc: list[dict], props: list[dict], labst, insight: str = "", real: str = "") -> str:
    lines = ["📅 Еженедельное собрание офиса"]
    if insight:
        lines += [insight, ""]
    lines += [shadow_text(week_rows) if week_rows else "Теневые портфели ещё накапливают данные."]
    closed = [t for t in trades if t.get("exit_reason") != "partial"]
    parts = [t for t in trades if t.get("exit_reason") == "partial"]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    lines += ["", f"Сделок закрыто за неделю: {len(closed)}" + (f", прибыльных {wins}" if closed else "") + (f"; частичных фиксаций: {len(parts)}" if parts else "")
              + (f". Реализованный результат (закрытые сделки и частичные фиксации, после комиссий): {signed(sum(t['pnl'] for t in trades))} $" if trades else "")]
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
