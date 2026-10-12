import re
from types import SimpleNamespace

from office import report
from office.board import Board


def norm(s):
    return s.replace("\u00a0", " ")  # в тысячах неразрывный пробел (число не рвётся на строки)


def test_number_formats():
    assert norm(report.usd(15020)) == "15 020 $" and report.price(0.51697) == "0,517 $" and norm(report.price(83890.012)) == "83 890,01 $"
    assert report.pct(-0.026) == "−2,6%" and report.pct(0.048) == "+4,8%" and report.pct(0.0, 2) == "0,00%"
    assert report.signed(720) == "+720" and report.signed(-45.4) == "−45"


def test_close_messages():
    assert report.close_message("ONDO/USD", -400, -0.026, "stop") == "🔴 Продажа ONDO — сработал стоп-лосс\nРезультат: −400 $ (−2,6%)"
    assert report.close_message("ONDO/USD", 720, 0.048, "take") == "🟢 Продажа ONDO — взят тейк-профит\nРезультат: +720 $ (+4,8%)"
    assert "по решению трейдера" in report.close_message("AAPL", 10, 0.01, "signal")


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)

    def ask(self, role, payload, system=None):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return {"phrases": a}


def test_phrases_reject_latin_and_fall_back(monkeypatch):
    items = {"market": "RSI overbought", "risk": "reduce"}
    monkeypatch.setattr(report, "GeminiBackend", lambda: FakeLLM([{"market": "RSI высокий", "risk": "уменьшил размер позиции из-за изменчивости"}]))
    monkeypatch.setattr(report, "ClaudeBackend", lambda model="": FakeLLM([{"market": "рынок перекуплен и может откатиться"}]))
    ph = report.phrases(items)
    assert ph["market"] == "рынок перекуплен и может откатиться" and ph["risk"].startswith("уменьшил")
    monkeypatch.setattr(report, "GeminiBackend", lambda: FakeLLM([{"market": "bad"}]))
    monkeypatch.setattr(report, "ClaudeBackend", lambda model="": FakeLLM([report.LLMError("x")]))
    assert report.phrases(items) == {"market": report.FALLBACK["market"], "risk": report.FALLBACK["risk"]}


def test_trade_message_template_and_no_latin(monkeypatch):
    b = Board()
    cid = "c"
    for role, st, v in (("market", "buy", "buy: bullish EMA"), ("quant", "hold", "hold: high vol"), ("bull", "buy", "buy: arg"), ("bear", "sell", "sell: arg"),
                        ("trader", "buy", "buy: ok")):
        b.publish(cid, role, "ONDO/USD", v, 0.7, refs=[] if role in ("market", "quant") else [1], stances={} if role in ("market", "quant") else {1: "agree"},
                  data={"stance": st, "action": st})
    b.publish(cid, "risk_manager", "ONDO/USD", "reduce: x", 0.5, refs=[1], stances={1: "agree"}, data={"verdict": "reduce", "rationale": "vol"})
    b.publish(cid, "portfolio_manager", "ONDO/USD", "approve: x", 0.6, refs=[1], stances={1: "agree"}, data={"decision": "approve"})
    monkeypatch.setattr(report, "phrases", lambda items: {k: f"фраза про {k}".replace("market", "график").replace("quant", "волатильность") if False else "тренд сильный" for k in items})
    res = SimpleNamespace(status="opened", asset="ONDO/USD", cycle_id=cid, order={"price": 0.51697, "qty": 29049.61, "stop": 0.5033, "take": 0.5417, "equity": 100000})
    m = norm(report.trade_message(b, res))
    assert m.startswith("🟢 Покупка ONDO (демо-счёт)\nЦена входа: 0,517 $\nСтоп-лосс: 0,503 $ (−2,6%)\nТейк-профит: 0,542 $ (+4,8%)\nСумма: 15 018 $ · риск: 397 $ (0,4% счёта)")
    assert "• Технический анализ: тренд сильный (за)" in m and "• Квант-аналитик: тренд сильный (сомневается)" in m
    assert "• Спор быка и медведя:" in m and "• Риск-менеджер:" in m and "• Управляющий:" in m
    assert not re.search(r"[A-Za-z#]", m.replace("ONDO", ""))  # ни латиницы, ни номеров отчётов


def test_summary_template():
    import datetime as dt
    m = norm(report.summary_message(100450, 100000, {"SPY": 0.002, "BTC": -0.003}, [("ONDO/USD", 0.012)], 1, "Лаборатория: проверено 28 стратегий, рабочих пока нет",
                               now=dt.datetime(2026, 10, 12, 12, 0)))
    assert m == ("📊 Сводка офиса на 12:00\nСчёт: 100 450 $ (+0,45% за день)\nДля сравнения: S&P 500 +0,2%, Биткоин −0,3%\n"
                 "Открытые позиции: ONDO +1,2%\nСделок за день: 1\nЛаборатория: проверено 28 стратегий, рабочих пока нет")


def test_lab_line():
    st = {"crypto": {"n": 14, "ok": 0, "syms": 1}, "stock": {"n": 14, "ok": 0, "syms": 1}}
    assert report.lab_line(st, []) == "Лаборатория: проверено 28 стратегий (крипта 14, акции 14), рабочих пока нет"


def test_plural_and_lab_line_grammar():
    assert [report.plural(n, "стратегия", "стратегии", "стратегий") for n in (1, 2, 5, 11, 21, 94, 112)] == ["стратегия", "стратегии", "стратегий", "стратегий", "стратегия", "стратегии", "стратегий"]
    st = {"crypto": {"n": 1, "ok": 0, "syms": 1}, "stock": {"n": 1, "ok": 0, "syms": 1}}
    assert "проверено 2 стратегии" in report.lab_line(st, [])


def test_crypto_fundamentals_resolved_by_symbol_not_cg_id(monkeypatch):
    """Причина вечного abstain: у альткойнов не было cg_id -> HTTPError. Теперь поиск по тикеру через /coins/markets."""
    from office import config, data
    seen = {}

    def fake_get(url, **kw):
        seen.update(url=url, params=kw["params"])
        return SimpleNamespace(json=lambda: [{"market_cap": 1, "market_cap_rank": 45, "circulating_supply": 2, "max_supply": 3, "total_volume": 4,
                                              "ath_change_percentage": -50.0}])
    monkeypatch.setattr(data, "_get", fake_get)
    data._FUND_CACHE.clear()
    f = data.fundamentals(config.make_asset("ONDO/USD"))
    assert "error" not in f and seen["params"]["symbols"] == "ondo" and f["market_cap_rank"] == 45
    monkeypatch.setattr(data, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert data.fundamentals(config.make_asset("ONDO/USD")) == f  # кэш спасает при сбое источника
