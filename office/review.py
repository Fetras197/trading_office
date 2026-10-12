"""Разбор закрытых сделок (2.2). Для каждой сделки без разбора: Claude получает тезис входа (отчёты цикла), картину дня на тот момент и ход цены
(максимальный плюс/минус за время сделки) и отвечает: кто был прав/неправ, момент входа, доля случайности. Урок пишется в память офиса (memory kind='lesson');
в похожей ситуации зал показывает трейдеру и спорящим прошлые уроки по этому активу и типу актива."""
import json

from . import memory
from .llm import LLMError, LLMUnavailable

OUTCOMES = ("right", "wrong", "luck_win", "bad_luck")
SYSTEM = """You are the post-trade reviewer of a paper-trading firm. Judge a CLOSED long trade fairly: separate decision quality from luck.
Input JSON: trade (entry, exit, pnl, r_multiple, exit_reason, holding hours), price_path (max favorable and max adverse excursion vs entry), entry_reports (what each role said at entry, with role names), picture_at_entry.
outcome: "right" (good decision, good result), "wrong" (bad decision, bad result), "luck_win" (bad decision but profit), "bad_luck" (good decision, loss from randomness).
right_roles / wrong_roles: only role names that appear in entry_reports. entry: "good|early|late" relative to the price path. luck: how much randomness explains the result.
Text fields in Russian, plain language. The lesson must be actionable for a similar future situation.
Schema: {"thesis": "<=30 words", "outcome": "right|wrong|luck_win|bad_luck", "right_roles": [..], "wrong_roles": [..], "entry": "good|early|late", "luck": "low|medium|high", "lesson": "<=30 words", "tags": ["<=3 short words"]}
Output exactly ONE JSON object."""


def _validate(raw, roles):
    o = str(raw["outcome"]).lower()
    if o not in OUTCOMES:
        raise ValueError(f"outcome {o!r}")
    return {"thesis": str(raw["thesis"])[:300], "outcome": o, "right_roles": [r for r in raw.get("right_roles", []) if r in roles], "wrong_roles": [r for r in raw.get("wrong_roles", []) if r in roles],
            "entry": str(raw.get("entry", "good")).lower(), "luck": str(raw.get("luck", "medium")).lower(), "lesson": str(raw["lesson"])[:300], "tags": [str(t)[:20] for t in raw.get("tags", [])][:3]}


def price_path(df, entry_ts, exit_ts, entry) -> dict:
    try:
        w = df[(df.index >= entry_ts) & (df.index <= exit_ts)]
        return {"max_favorable_pct": round(float(w["high"].max() / entry - 1) * 100, 2), "max_adverse_pct": round(float(w["low"].min() / entry - 1) * 100, 2)} if len(w) else {}
    except Exception:  # noqa: BLE001
        return {}


def review_trade(board, journal, router, t: dict, candles_fn=None) -> int:
    run = journal.q("SELECT mode FROM runs WHERE id=?", (t["run_id"],))
    cid = run[0]["mode"] if run else None
    reps = board.cycle_reports(cid, t["asset"]) if cid else []
    ent = [{"role": r["role"], "stance": r["data"].get("stance") or r["data"].get("action") or r["data"].get("verdict") or r["data"].get("decision"), "confidence": r["confidence"], "text": r["verdict"][:300]} for r in reps]
    pic = board.q("SELECT data FROM reports WHERE role='picture' AND ts<=? ORDER BY id DESC LIMIT 1", (t["entry_ts"],))
    path = {}
    if candles_fn:
        try:
            path = price_path(candles_fn(t["asset"]), t["entry_ts"], t["exit_ts"], t["entry"])
        except Exception:  # noqa: BLE001
            pass
    hours = None
    try:
        import datetime as dt
        hours = round((dt.datetime.fromisoformat(t["exit_ts"]) - dt.datetime.fromisoformat(t["entry_ts"])).total_seconds() / 3600, 1)
    except ValueError:
        pass
    payload = {"trade": {"asset": t["asset"], "entry": t["entry"], "exit": t["exit"], "pnl_usd": round(t["pnl"], 2), "r_multiple": t["r_mult"], "exit_reason": t["exit_reason"], "holding_hours": hours},
               "price_path": path, "entry_reports": ent, "picture_at_entry": json.loads(pic[0]["data"]).get("summary") if pic else None}
    roles = {e["role"] for e in ent}
    last = None
    for _ in range(2):
        try:
            out = _validate(router.ask("postmortem", payload, system=SYSTEM), roles)
            break
        except LLMUnavailable:
            raise
        except (LLMError, ValueError, KeyError, TypeError) as e:
            last = e
    else:
        raise LLMError(f"postmortem: невалидный ответ ({last})")
    kind = "crypto" if "/" in t["asset"] else "stock"
    rid = board.publish(f"R-post-{t['id']}", "postmortem", t["asset"], f"{out['outcome']}: {out['lesson']}", 0.7, data={**out, "trade_id": t["id"], "pnl": t["pnl"], "path": path})
    memory.remember(board, "lesson", out["lesson"], asset=t["asset"], tags=(kind, out["outcome"], *out["tags"]), data={"trade_id": t["id"], "pnl": t["pnl"], "thesis": out["thesis"]})
    return rid


def run(board, journal, router, candles_fn=None, max_n: int = 3) -> list[int]:
    """Разобрать ещё не разобранные закрытые сделки (не более max_n за проход)."""
    done = []
    for t in journal.q("SELECT * FROM trades ORDER BY id"):
        if journal.kv_get(f"reviewed:{t['id']}") or t["exit_reason"] == "partial":   # частичные фиксации — часть одной сделки
            continue
        if len(done) >= max_n:
            break
        done.append(review_trade(board, journal, router, t, candles_fn))
        journal.kv_set(f"reviewed:{t['id']}", 1)
    return done


def lessons_for(board, asset: str, kind: str, limit: int = 4) -> list[str]:
    """Уроки для зала: по этому активу + по типу актива (без дублей)."""
    out = memory.brief(board, "lesson", asset=asset, days=180, limit=limit)
    for x in memory.brief(board, "lesson", tag=kind, days=90, limit=limit):
        if x not in out:
            out.append(x)
    return out[-limit:]
