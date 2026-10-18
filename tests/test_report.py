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
    ph = report.phrases(items)
    assert ph["market"] == report.FALLBACK["market"] and ph["risk"].startswith("уменьшил")   # латиница в фразе = брак → запасная фраза
    monkeypatch.setattr(report, "GeminiBackend", lambda: FakeLLM([report.LLMError("x")]))
    assert report.phrases(items) == {"market": report.FALLBACK["market"], "risk": report.FALLBACK["risk"]}


def test_trade_message_template_and_no_latin(monkeypatch):
    b = Board()
    cid = "c"
    for role, st, v in (("market", "buy", "buy: bullish EMA"), ("quant", "hold", "hold: high vol")):
        b.publish(cid, role, "BTC/USD", v, 0.7, data={"stance": st})
    b.publish(cid, "decision", "BTC/USD", "buy: тренд", 0.6, data={"gaps": ["нет свежих новостей"]})
    monkeypatch.setattr(report, "phrases", lambda items: {k: "тренд сильный" for k in items})
    res = SimpleNamespace(status="opened", asset="BTC/USD", cycle_id=cid, order={"price": 0.51697, "qty": 29049.61, "stop": 0.5033, "take": 0.5417, "equity": 100000,
                                                                                 "tech": "trend", "half": True, "gaps": ["нет свежих новостей"]})
    m = norm(report.trade_message(b, res))
    assert m.startswith("🟢 Покупка BTC (демо-счёт)\nЦена входа: 0,517 $\nСтоп-лосс: 0,503 $ (−2,6%)\nТейк-профит: 0,542 $ (+4,8%)\nСумма: 15 018 $ · риск: 397 $ (0,4% счёта)")
    assert "• Технический анализ: тренд сильный (за)" in m and "• Квант-аналитик: тренд сильный (сомневается)" in m
    assert "• Технический сигнал: растущий тренд" in m and "• Объём вдвое меньше обычного: нет свежих новостей" in m
    assert "Спор быка" not in m and "Риск-менеджер" not in m
    assert not re.search(r"[A-Za-z#]", m.replace("BTC", ""))  # ни латиницы, ни номеров отчётов


def test_summary_template():
    import datetime as dt
    view = {"equity": 100450.0, "cash": 84000.5, "last_equity": 100000.0,
            "positions": [{"symbol": "ONDOUSD", "plpc": 0.012, "pl": 180.0}, {"symbol": "AAPL", "plpc": -0.004, "pl": -35.0}]}
    m = norm(report.summary_message(view, {"SPY": 0.002, "BTC": -0.003}, 1, "Лаборатория: проверено 28 стратегий, рабочих пока нет", now=dt.datetime(2026, 10, 12, 12, 0)))
    assert m == ("📊 Сводка офиса на 12:00\nСчёт: 100 450 $ (+0,45% за день)\nСвободные деньги: 84 000 $\nДля сравнения: S&P 500 +0,2%, Биткоин −0,3%\n"
                 "Открытые позиции: ONDO +1,2% (+180 $), AAPL −0,4% (−35 $)\nСделок за день: 1\nЛаборатория: проверено 28 стратегий, рабочих пока нет")


def test_alpaca_account_view_and_day_change():
    from office.alpaca import AlpacaClient

    class R:
        def __init__(s, j, code=200): s._j, s.status_code, s.text = j, code, "x"
        def json(s): return s._j

    class S:
        def request(s, method, url, **kw):
            if url.endswith("/account"):
                return R({"equity": "100450.1", "cash": "84000", "last_equity": "100000"})
            return R([{"symbol": "ONDOUSD", "qty": "100", "avg_entry_price": "0.5", "current_price": "0.51", "market_value": "51", "unrealized_pl": "1", "unrealized_plpc": "0.02"}])

        def get(s, url, **kw):
            if "crypto" in url:
                return R({"snapshots": {"BTC/USD": {"latestTrade": {"p": 101}, "prevDailyBar": {"c": 100}, "dailyBar": {"c": 101}}}})
            return R({"SPY": {"latestTrade": {"p": 99}, "prevDailyBar": {"c": 100}, "dailyBar": {"c": 99}}})
    c = AlpacaClient(key="k", secret="s", session=S())
    v = c.account_view()
    assert v["equity"] == 100450.1 and v["last_equity"] == 100000 and v["positions"][0]["pl"] == 1
    assert abs(c.day_change("BTC/USD") - 0.01) < 1e-9 and abs(c.day_change("SPY") + 0.01) < 1e-9


def test_lab_line():
    st = {"crypto": {"n": 14, "ok": 0, "syms": 1}, "stock": {"n": 14, "ok": 0, "syms": 1}}
    assert report.lab_line(st, []) == ("Лаборатория: проверено 28 стратегий (крипта 14, акции 14); прошли строгую проверку: 0; на испытании: 0; подтверждены вживую: 0")


def test_plural_and_lab_line_grammar():
    assert [report.plural(n, "стратегия", "стратегии", "стратегий") for n in (1, 2, 5, 11, 21, 94, 112)] == ["стратегия", "стратегии", "стратегий", "стратегий", "стратегия", "стратегии", "стратегий"]
    st = {"crypto": {"n": 1, "ok": 0, "syms": 1}, "stock": {"n": 1, "ok": 0, "syms": 1}}
    assert "проверено 2 стратегии" in report.lab_line({**st, "trial": 2, "live": 1}, []) and "на испытании: 2; подтверждены вживую: 1" in report.lab_line({**st, "trial": 2, "live": 1}, [])


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


def test_proofread_accepts_only_safe_edits():
    from office import proofread as pr

    class B:
        def __init__(self, out): self.out = out
        def ask(self, role, payload, system=None): return {"text": self.out}
    src = "📊 Сводка\nЗа сутки проверено 3 стратегий, все ещё нет"
    assert pr.proofread(src, B("📊 Сводка\nЗа сутки проверено 3 стратегии, всё ещё нет")) == "📊 Сводка\nЗа сутки проверено 3 стратегии, всё ещё нет"
    pr._CACHE.clear()
    assert pr.proofread(src, B("📊 Сводка\nЗа сутки проверено 4 стратегии, всё ещё нет")) == src        # цифра изменена -> отклонено
    pr._CACHE.clear()
    assert pr.proofread(src, B("📊 Сводка\nЗа сутки проверено 3 стратегии, still нет")) == src          # новая латиница -> отклонено
    assert pr.sanitize("risk_manager одобрил (#12)") == "Риск-менеджер одобрил"
    assert pr.sanitize("Открыта позиция ONDO/USD, SPY вырос") == "Открыта позиция ONDO, S&P 500 вырос"


def test_proofread_never_flips_meaning():
    from office import proofread as pr

    class B:
        def __init__(self, out): self.out = out
        def ask(self, role, payload, system=None): return {"text": self.out}
    for src, bad in (("Сегодня нельзя:\n• не покупать перед ФРС", "Сегодня нельзя:\n• покупать перед ФРС"),                 # потеряно «не»
                     ("🔴 Продажа ONDO — по таймеру\nРезультат: −8 $", "🔴 Медвежья сделка ONDO — по таймеру\nРезультат: −8 $"),      # подмена ключевого слова
                     ("Риск-менеджер уменьшил размер позиции из-за высокой изменчивости", "Менеджер по рискам полностью переписал текст другими словами")):
        pr._CACHE.clear()
        assert pr.proofread(src, B(bad)) == src
    pr._CACHE.clear()
    ok = "Все еще нет покупок, ждем решения"
    assert pr.proofread(ok, B("Всё ещё нет покупок, ждём решения")) == "Всё ещё нет покупок, ждём решения"
