import pytest

from office import entry
from office.views import nogo_lines
from office.journal import Journal

BASE = {"close": 100.0, "ema20": 99.5, "ema50": 99.0, "ema200": 95.0, "rsi14": 55.0, "macd_hist": 0.3, "bb_lower": 97.0, "atr14": 1.0}


def test_trend_signal_and_partial_scores():
    assert entry.technical(BASE).kind == "trend"
    t = entry.technical({**BASE, "macd_hist": -1})
    assert not t.signal and 0.5 <= t.score < 1 and "импульс (MACD) отрицательный" in t.notes
    assert not entry.technical({**BASE, "rsi14": 80}).signal                     # перегрет


def test_bounce_signal_needs_long_trend_intact():
    b = {**BASE, "close": 96.0, "ema50": 99.0, "ema20": 98.0, "rsi14": 30.0, "macd_hist": -0.5, "bb_lower": 95.0}
    assert entry.technical(b).kind == "bounce"
    assert not entry.technical({**b, "ema200": 100.0}).signal


def test_missing_indicators_no_signal():
    assert not entry.technical({"close": 1.0}).signal


def test_majority_rule():
    v = lambda b, s, h, a=0: entry.Votes(b, s, h, a)  # noqa: E731
    assert entry.majority(v(2, 0, 2)) and entry.majority(v(1, 0, 0)) and entry.majority(v(3, 1, 0))
    assert not entry.majority(v(1, 0, 2)) and not entry.majority(v(2, 2, 0)) and not entry.majority(v(0, 0, 0, 4))
    assert not entry.majority(v(1, 1, 0))


def test_gaps_cause_half_size_conditions():
    full = entry.Votes(3, 0, 1)
    assert entry.data_gaps({"news": [1], "fundamentals": {"pe": 1}}, full) == []
    assert "нет свежих новостей" in entry.data_gaps({"news": [], "fundamentals": {"pe": 1}}, full)
    assert "нет фундаментальных данных" in entry.data_gaps({"news": [1], "fundamentals": {"error": "x"}}, full)
    assert any("мало голосов" in g for g in entry.data_gaps({"news": [1], "fundamentals": {"pe": 1}}, entry.Votes(1, 0, 1)))


def test_levels_rr_is_two_and_crypto_levels_wide_and_inside_gate():
    from office.risk import RiskGate
    stop, take = entry.levels(100, 2)
    assert (100 - stop) * 2 == take - 100
    s2, t2 = entry.levels(100, 1, "revert", "crypto")
    assert (100 - s2) * 2 == pytest.approx(t2 - 100) and 100 - s2 <= RiskGate().l.max_stop_atr - 0.1       # внутри диапазона стопа RiskGate (≤4 ATR)
    assert entry.levels(100, 1, "support", "stock") == (98.5, 103.0)


def test_sideways_and_falling_market_patterns():
    down = {**BASE, "close": 90.0, "ema20": 92.0, "ema50": 95.0, "ema200": 100.0, "macd_hist": -1.0, "rsi14": 30.0, "bb_lower": 89.8, "low_50": 89.5, "atr14": 1.0}
    t = entry.technical(down)
    assert t.signal and t.kind == "support"                                  # у минимума 50 свечей, RSI низкий
    far = entry.technical({**down, "low_50": 80.0})                          # от поддержки далеко, но цена на 2 ATR ниже EMA20 и у нижней полосы
    assert far.signal and far.kind == "revert"
    assert not entry.technical({**down, "low_50": 80.0, "rsi14": 50.0}).signal
    assert not entry.technical({**down, "rsi14": 60.0, "low_50": 80.0}).signal
    assert "support" in entry.COUNTER and "revert" in entry.COUNTER and entry.TECH_RU["support"] == "отскок от поддержки"


def test_nogo_top_picks_best_per_asset_excludes_bought_and_formats():
    j = Journal()
    j.add_nogo("2026-10-12T10:00:00+00:00", "BTC/USD", "signal", "нет сигнала", 0.2)
    j.add_nogo("2026-10-12T11:00:00+00:00", "BTC/USD", "votes", "аналитики против", 0.8)
    j.add_nogo("2026-10-12T11:00:00+00:00", "ETH/USD", "limits", "лимит 3 позиций исчерпан", 0.95)
    j.add_nogo("2026-10-12T11:00:00+00:00", "SOL/USD", "signal", "нет сигнала", 0.3)
    j.add_nogo("2026-10-12T11:00:00+00:00", "AAPL", "votes", "x", 0.9)
    j.add_fill("2026-10-12T11:30:00+00:00", "AAPL", "buy", 1, 1, 0, "entry", None)
    top = j.nogo_top("2026-10-12T00:00:00+00:00", 3)
    assert [r["asset"] for r in top] == ["ETH/USD", "BTC/USD", "SOL/USD"] and top[1]["stage"] == "votes"
    text = "\n".join(nogo_lines(top))
    assert "Почему не вошли" in text and "1) ETH — лимит риска: лимит 3 позиций" in text and "2) BTC — аналитики:" in text
    assert "кандидатов за сутки не оценивали" in nogo_lines([])[0]
