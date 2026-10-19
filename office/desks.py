"""Корпорация: отделы со своими бюджетами, позициями и прибылью.
Отделы: «Тренд» (шаблоны trend/bounce), «Отскок и возврат к средней» (support/revert), «Акции США» (акции и ETF, часы биржи, комиссия 0), «Крупная крипта» (топ по обороту).
Бюджет — доля счёта (по 20% на отдел, 20% резерв). Лимиты НА ОТДЕЛ: 3 позиции, 5 входов в сутки; риск 2% счёта на сделку и дневной стоп −5% счёта — общие и неизменные.
Маршрут сигнала — первый подходящий отдел со свободным лимитом: Акции США → Крупная крипта → Тренд → Отскок. Лаборатория-отделы (под стратегию со статусом live) идут первыми."""
import hashlib

from . import entry
from .config import CRYPTO_CORE, Limits

BASE_SHARE, RESERVE, MIN_SHARE, MAX_SHARE, STEP = 0.20, 0.20, 0.05, 0.40, 0.05
MAX_POSITIONS, MAX_ENTRIES_DAY = 3, 5
CLOSE_AFTER_TRADES = 20
POSITION_CAP = 0.5            # потолок одной позиции — доля бюджета отдела
ALL = ("trend", "bounce", "support", "revert")
BUILTIN = {
    "stocks": {"name": "Акции США", "patterns": ALL, "assets": "stock"},
    "majors": {"name": "Крупная крипта", "patterns": ALL, "assets": "major"},
    "trend": {"name": "Тренд", "patterns": ("trend", "bounce"), "assets": "any"},
    "reversal": {"name": "Отскок и возврат к средней", "patterns": ("support", "revert"), "assets": "any"},
}
ORDER = ("stocks", "majors", "trend", "reversal")


def seed(j, now_iso: str) -> None:
    """Завести встроенные отделы (один раз)."""
    for did in ORDER:
        if not j.q("SELECT 1 FROM desks WHERE id=?", (did,)):
            j._x("INSERT INTO desks(id,name,kind,share,status,opened) VALUES(?,?,?,?,?,?)", (did, BUILTIN[did]["name"], "builtin", BASE_SHARE, "active", now_iso))
            j._x("INSERT INTO desk_hist(ts,desk,event,share,note) VALUES(?,?,?,?,?)", (now_iso, did, "open", BASE_SHARE, "создан"))
    adopt_legacy(j)


def adopt_legacy(j) -> int:
    """Позиции, открытые до введения отделов (desk IS NULL), закрепляются за отделом по активу: акции → «Акции США», крупная крипта → «Крупная крипта», остальное → «Тренд»."""
    n = 0
    for p in j.q("SELECT asset FROM positions WHERE desk IS NULL"):
        a = p["asset"]
        d = "stocks" if "/" not in a else "majors" if a in majors(j) else "trend"
        j._x("UPDATE positions SET desk=? WHERE asset=?", (d, a))
        n += 1
    return n


def all_desks(j) -> list[dict]:
    rows = {r["id"]: r for r in j.q("SELECT * FROM desks")}
    return [rows[d] for d in ORDER if d in rows] + [r for k, r in rows.items() if k not in ORDER]


def active(j) -> list[dict]:
    lab = [d for d in all_desks(j) if d["kind"] == "lab" and d["status"] == "active"]
    return lab + [d for d in all_desks(j) if d["kind"] == "builtin" and d["status"] == "active"]


def name_of(j, desk_id: str | None) -> str:
    if not desk_id:
        return "без отдела"
    r = j.q("SELECT name FROM desks WHERE id=?", (desk_id,))
    return r[0]["name"] if r else desk_id


def majors(j) -> list[str]:
    return j.kv_get("major_crypto") or list(CRYPTO_CORE)


def eligible(j, desk: dict, symbol: str, pattern: str, stock_open: bool) -> bool:
    """Подходит ли отделу сигнал по активу."""
    crypto = "/" in symbol
    if desk["kind"] == "lab":
        return pattern == "lab" and desk.get("strategy") is not None and symbol in (j.kv_get(f"labdesk:{desk['id']}") or [])
    spec = BUILTIN.get(desk["id"])
    if not spec or pattern not in spec["patterns"]:
        return False
    if not crypto and not stock_open:
        return False                                  # акции вне часов биржи не торгуем
    return {"stock": not crypto, "major": crypto and symbol in majors(j), "any": True}[spec["assets"]]


def alt_positions(j) -> list[str]:
    """Открытые позиции в альткоинах по ВСЕМ отделам (BTC и ETH альткоинами не считаются)."""
    return [a for a in j.positions() if "/" in a and a not in entry.ALT_EXEMPT]


def usage(j, desk_id: str) -> dict:
    pos = [p for p in j.positions().values() if p.get("desk") == desk_id]
    return {"open": len(pos), "used": sum(p["qty"] * p["avg"] for p in pos)}


def capacity(j, desk: dict, equity: float, day: str) -> dict:
    """Остаток лимитов отдела: позиции, входы за сутки, свободный бюджет."""
    u = usage(j, desk["id"])
    entries = j.entries_on(day, desk["id"])
    budget = desk["share"] * equity
    free = budget - u["used"]
    why = ""
    if u["open"] >= MAX_POSITIONS:
        why = f"лимит {MAX_POSITIONS} позиций отдела"
    elif entries >= MAX_ENTRIES_DAY:
        why = f"лимит {MAX_ENTRIES_DAY} входов в сутки отдела"
    elif free < 10:
        why = "бюджет отдела исчерпан"
    return {"ok": not why, "why": why, "open": u["open"], "entries": entries, "budget": budget, "free": free}


def route(j, symbol: str, pattern: str, equity: float, day: str, stock_open: bool) -> tuple[dict | None, str]:
    """(отдел, причина отказа). Перебор по приоритету; если подходящие отделы есть, но все полные — причина называет их."""
    full = []
    for d in active(j):
        if not eligible(j, d, symbol, pattern, stock_open):
            continue
        cap = capacity(j, d, equity, day)
        if cap["ok"]:
            return {**d, **{"cap": cap}}, ""
        full.append(f"«{d['name']}»: {cap['why']}")
    return None, ("нет свободного отдела — " + "; ".join(full)) if full else "ни один отдел не берёт такой сигнал (актив/шаблон/часы биржи)"


def limits_for(desk: dict, equity: float) -> Limits:
    """Лимиты RiskGate для сделки отдела. Риск 2% счёта на сделку и дневной стоп −5% счёта — те же; позиций/входов — на отдел; потолок позиции — половина бюджета отдела."""
    return Limits(risk_per_trade=0.02, max_notional=POSITION_CAP * desk["share"], daily_stop=-0.05, max_trades_day=MAX_ENTRIES_DAY, max_positions=MAX_POSITIONS)


# ---------- статистика для отчётов ----------
def stats(j, since: str = "", view: dict | None = None) -> list[dict]:
    """По каждому отделу: бюджет, открытые позиции (и нереализованный результат по данным брокера), закрытые сделки за период, прибыль после комиссий, комиссии."""
    live = {}
    for p in (view or {}).get("positions", []):
        live[p["symbol"]] = p
    out = []
    eq = (view or {}).get("equity")
    for d in all_desks(j):
        t = j.q("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(fees),0) fees, COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) wins "
                "FROM trades WHERE desk=? AND exit_reason!='partial' AND exit_ts>=?", (d["id"], since))[0]
        part = j.q("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(fees),0) fees FROM trades WHERE desk=? AND exit_reason='partial' AND exit_ts>=?", (d["id"], since))[0]
        pos = [p for p in j.positions().values() if p.get("desk") == d["id"]]
        unreal = sum(live.get(a["asset"].replace("/", ""), {}).get("pl", 0.0) for a in pos)
        opened_fees = sum(p["fees"] for p in pos)
        out.append({"id": d["id"], "name": d["name"], "status": d["status"], "share": d["share"], "budget": d["share"] * eq if eq else None, "open": len(pos), "unrealized": unreal,
                    "trades": t["n"], "wins": t["wins"], "partials": part["n"], "pnl": t["pnl"] + part["pnl"],   # прибыль отдела = закрытые сделки + частичные фиксации
                    "fees": t["fees"] + part["fees"] + opened_fees})
    return out


def lifetime(j, desk_id: str) -> dict:
    """Результат отдела за всё время жизни (для совета): закрытые сделки (без частичных) и суммарная прибыль после комиссий."""
    d = j.q("SELECT opened FROM desks WHERE id=?", (desk_id,))[0]
    t = j.q("SELECT COUNT(*) n FROM trades WHERE desk=? AND exit_reason!='partial' AND exit_ts>=?", (desk_id, d["opened"]))[0]
    p = j.q("SELECT COALESCE(SUM(pnl),0) pnl FROM trades WHERE desk=? AND exit_ts>=?", (desk_id, d["opened"]))[0]
    return {"trades": t["n"], "pnl": p["pnl"]}


# ---------- совет директоров ----------
def council(j, board, equity: float, now_iso: str, week_since: str) -> list[str]:
    """Воскресное заседание. 1) закрыть отдел с ≥20 сделками и суммарным минусом; 2) перераспределить бюджеты по результатам недели (шаг ≤5 п.п., пол 5%, потолок 40%,
    резерв ≥20% не трогаем); 3) открыть новый отдел ТОЛЬКО под стратегию лаборатории со статусом live. Возвращает строки решений по-русски."""
    out = []
    for d in active(j):
        lt = lifetime(j, d["id"])
        if lt["trades"] >= CLOSE_AFTER_TRADES and lt["pnl"] < 0:
            j._x("UPDATE desks SET status='closed', closed=?, note=? WHERE id=?", (now_iso, f"{lt['trades']} сделок, итог {lt['pnl']:.0f} $", d["id"]))
            j._x("INSERT INTO desk_hist(ts,desk,event,share,note) VALUES(?,?,?,?,?)", (now_iso, d["id"], "close", d["share"], f"{lt['trades']} сделок, итог {lt['pnl']:.2f} $"))
            out.append(f"Отдел «{d['name']}» закрыт: {lt['trades']} сделок и общий убыток {-lt['pnl']:.0f} $. Новых входов нет, открытые позиции доводятся стопами; бюджет {d['share'] * 100:.0f}% возвращён в резерв.")
    act = active(j)
    scored = []
    for d in act:
        st = next(s for s in stats(j, week_since) if s["id"] == d["id"])
        if st["trades"]:
            scored.append((st["pnl"] / max(d["share"] * equity, 1.0), d, st))
    scored.sort(key=lambda x: x[0])
    if len(scored) >= 2 and scored[-1][0] > scored[0][0]:
        (r_lo, lo, _), (r_hi, hi, _) = scored[0], scored[-1]
        if lo["share"] - STEP >= MIN_SHARE - 1e-9 and hi["share"] + STEP <= MAX_SHARE + 1e-9:
            for d, new in ((lo, lo["share"] - STEP), (hi, hi["share"] + STEP)):
                j._x("UPDATE desks SET share=? WHERE id=?", (new, d["id"]))
                j._x("INSERT INTO desk_hist(ts,desk,event,share,note) VALUES(?,?,?,?,?)", (now_iso, d["id"], "share", new, "перераспределение по результатам недели"))
            out.append(f"Бюджет перераспределён: «{hi['name']}» {hi['share'] * 100:.0f}% → {(hi['share'] + STEP) * 100:.0f}% (доходность недели на бюджет {r_hi * 100:+.2f}%), "
                       f"«{lo['name']}» {lo['share'] * 100:.0f}% → {(lo['share'] - STEP) * 100:.0f}% ({r_lo * 100:+.2f}%).")
    out += open_lab_desks(j, board, now_iso)
    if not out:
        out.append("Изменений в структуре нет: бюджеты остаются прежними, отделы работают.")
    return out


def reserve_free(j) -> float:
    """Нераспределённый бюджет сверх обязательного резерва 20%."""
    return max(0.0, 1.0 - sum(d["share"] for d in active(j)) - RESERVE)


def open_lab_desks(j, board, now_iso: str) -> list[str]:
    """Новый отдел — только под стратегию, прошедшую лабораторию (lab_trial.status='live'); по одному за заседание, из свободного бюджета."""
    out = []
    used = {d["strategy"] for d in all_desks(j) if d["kind"] == "lab"}
    for r in board.q("SELECT key, name, tf, symbol FROM lab_trial WHERE status='live' ORDER BY key"):
        sid = r["name"] + "|" + r["tf"]
        if sid in used or reserve_free(j) < MIN_SHARE - 1e-9:
            continue
        did = "lab:" + hashlib.sha1(sid.encode()).hexdigest()[:6]
        share = min(BASE_SHARE, reserve_free(j))
        syms = sorted({x["symbol"] for x in board.q("SELECT symbol FROM lab_trial WHERE status='live' AND name=? AND tf=?", (r["name"], r["tf"]))})
        j._x("INSERT INTO desks(id,name,kind,share,status,opened,strategy) VALUES(?,?,?,?,?,?,?)", (did, f"Лаборатория: {r['name']} ({r['tf']})", "lab", share, "active", now_iso, sid))
        j.kv_set(f"labdesk:{did}", syms)
        j._x("INSERT INTO desk_hist(ts,desk,event,share,note) VALUES(?,?,?,?,?)", (now_iso, did, "open", share, f"стратегия {sid} прошла лабораторию (live)"))
        out.append(f"Открыт отдел «Лаборатория: {r['name']} ({r['tf']})» с бюджетом {share * 100:.0f}%: стратегия прошла лабораторию и испытательный срок.")
        used.add(sid)
        break
    return out
