"""SMOKE-тест исполнения (НЕ естественный сигнал): ответ роли trader подставляется кодом (buy, стоп 1.5 ATR, тейк 3 ATR),
остальные роли (аналитики Gemini, bull/bear, risk, PM, executor), RiskGate и Alpaca paper — настоящие. Печатает id ордера и цепочку.
Использование: python scripts/smoke_order.py BTC/USD [--close]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from office import report  # noqa: E402
from office.supervisor import Supervisor  # noqa: E402

sym, close = sys.argv[1], "--close" in sys.argv
s = Supervisor()
real = s.hall._ask_json


def fake_trader(role, payload, must, fb=None):
    if role != "trader":
        return real(role, payload, must, fb)
    p, atr = payload["price"], payload["atr14"]
    ids = [r["id"] for r in payload["reports"]]
    return {"action": "buy", "entry": p, "stop": p - 1.5 * atr, "take_profit": p + 3 * atr, "confidence": 0.5,
            "rationale": "SMOKE-TEST: подставленный ответ trader для проверки исполнения", "refs": ids, "stances": {str(i): "agree" for i in ids}}


s.hall._ask_json = fake_trader
r = s.hall.run_asset(sym)
print(json.dumps({"status": r.status, "detail": r.detail, "order": r.order, "cycle_id": r.cycle_id}, ensure_ascii=False, default=str))
print(report.chain_text(s.board, r.cycle_id, r.asset))
if r.status == "opened":
    s.notify("🧪 SMOKE-TEST (trader подставлен)\n" + report.trade_message(s.board, r))
    if close:
        ev = s.broker.sell(sym, r.order["price"], __import__("office.hall", fromlist=["utcnow"]).utcnow(), "smoke-close")
        print("закрыто:", ev, "order", s.broker.last_order_id)
