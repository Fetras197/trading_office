"""Вопросы обычным текстом («почему купили ONDO?», «что думаешь про биткоин?») — ответ офиса по-русски на основе board.db (4.1).
Claude получает только данные из базы (отчёты цикла входа, последние решения, позиции, картина дня, уроки), роли — уже с русскими названиями."""
import json
import re

from . import memory, review
from .config import CRYPTO_CORE, STOCK_CORE, make_asset
from .llm import LLMError, LLMUnavailable
from .views import ROLES_ALL

ALIASES = {"биткоин": "BTC/USD", "битк": "BTC/USD", "бтц": "BTC/USD", "эфир": "ETH/USD", "эфириум": "ETH/USD", "солан": "SOL/USD", "догикоин": "DOGE/USD", "рипл": "XRP/USD",
           "эпл": "AAPL", "эппл": "AAPL", "нвидиа": "NVDA", "майкрософт": "MSFT", "тесла": "TSLA", "гугл": "GOOGL", "амазон": "AMZN"}
SYSTEM = """You are the spokesperson of a paper-trading firm answering its owner's question. The owner is not a programmer or trader: answer in plain Russian, up to 120 words.
Use ONLY the JSON context (reports of our analysts and managers, trades, positions, the Picture of the Day, lessons). If the context lacks the answer, say honestly that there is no data.
Never invent numbers. Do not use English words, role codes or report ids: use the Russian role names given in the context. Numbers: space between thousands, comma for decimals, "$" sign.
Say who argued what and why in simple words; for "why did we buy" explain the thesis, the bull/bear disagreement, the risk manager's view and what the stop/target are.
Output exactly ONE JSON object: {"answer": "<Russian text>"}"""


def find_assets(text: str, known: list[str]) -> list[str]:
    low = text.lower()
    out = []
    for k, sym in ALIASES.items():
        if k in low and sym not in out:
            out.append(sym)
    toks = {t.upper() for t in re.findall(r"[A-Za-z]{2,6}", text)}
    for s in known:
        base = s.split("/")[0]
        if base in toks and s not in out:
            out.append(s)
    return out[:2]


def _roles(reports: list[dict]) -> list[dict]:
    return [{"role": ROLES_ALL.get(r["role"], r["role"]), "confidence": round(r["confidence"], 2), "text": r["verdict"][:400]} for r in reports]


def asset_context(board, j, asset: str) -> dict:
    kind = make_asset(asset).kind
    ctx: dict = {"asset": asset, "position": j.positions().get(asset)}
    ex = j.q("SELECT run_id, ts, entry, stop, take, qty FROM decisions WHERE asset=? AND executed=1 AND action='buy' ORDER BY id DESC LIMIT 1", (asset,))
    if ex:
        run = j.q("SELECT mode FROM runs WHERE id=?", (ex[0]["run_id"],))
        ctx["last_purchase"] = {k: ex[0][k] for k in ("ts", "entry", "stop", "take", "qty")}
        if run:
            ctx["purchase_cycle_reports"] = _roles(board.cycle_reports(run[0]["mode"], asset))
    last = board.q("SELECT cycle_id FROM reports WHERE asset=? AND role='trader' ORDER BY id DESC LIMIT 1", (asset,))
    last_any = board.q("SELECT cycle_id FROM reports WHERE asset=? ORDER BY id DESC LIMIT 1", (asset,))
    cid = (last or last_any or [{}])[0].get("cycle_id")
    if cid and (not ex or not ctx.get("purchase_cycle_reports") or cid != (run[0]["mode"] if ex and run else None)):
        ctx["latest_cycle_reports"] = _roles(board.cycle_reports(cid, asset))
    ctx["closed_trades"] = j.q("SELECT exit_ts, entry, exit, pnl, exit_reason FROM trades WHERE asset=? AND exit_reason!='partial' ORDER BY id DESC LIMIT 3", (asset,))
    ctx["lessons"] = review.lessons_for(board, asset, kind)
    nd = board.latest("newsdesk", max_age_h=6)
    if nd and asset in nd["data"].get("assets", {}):
        ctx["news"] = nd["data"]["assets"][asset]
    return ctx


def build_context(board, j, text: str, known: list[str]) -> dict:
    assets = find_assets(text, known + list(CRYPTO_CORE) + list(STOCK_CORE))
    pic = board.latest("picture", max_age_h=12)
    rg = board.latest("regime", max_age_h=6)
    ctx = {"question": text, "positions": j.positions(), "picture": {k: pic["data"].get(k) for k in ("regime", "summary", "main_risks", "forbidden", "size_mult")} if pic else None,
           "market_regime": {k: (v or {}).get("regime") for k, v in (rg["data"].items() if rg else []) if k in ("crypto", "stock")}, "assets": [asset_context(board, j, a) for a in assets]}
    if not assets:
        ctx["recent_decisions"] = [{"asset": r["asset"], "when": r["ts"][:16], "decision": r["verdict"][:200]} for r in board.q("SELECT asset, ts, verdict FROM reports WHERE role='trader' ORDER BY id DESC LIMIT 6")]
        ctx["recent_lessons"] = memory.brief(board, "lesson", days=30, limit=4)
    return json.loads(json.dumps(ctx, default=str, ensure_ascii=False))


def answer(router, ctx: dict) -> str:
    try:
        out = router.ask("qa", ctx, system=SYSTEM)
        a = str(out["answer"]).strip()
        if not a:
            raise ValueError("пусто")
        return a[:1500]
    except LLMUnavailable:
        return "Сейчас не могу ответить: исчерпан лимит помощника. Попробуйте позже."
    except (LLMError, KeyError, ValueError, TypeError):
        return "Не получилось сформулировать ответ. Попробуйте переспросить иначе."
