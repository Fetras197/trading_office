"""Запасные бесплатные источники: новости, свечи акций, макро (FRED), CPI."""
import pandas as pd
import pytest

from office import data, sources


def test_headlines_chain_falls_through_dedups_and_stops_at_three(monkeypatch):
    monkeypatch.setattr(data, "alpaca_news", lambda s, n=8: [])
    monkeypatch.setattr(data, "yahoo_rss", lambda s, n=8: [{"title": "Apple up", "published": "1"}, {"title": "Apple UP!", "published": "2"}])
    monkeypatch.setattr(data, "yf_news", lambda s, n=8: [])
    monkeypatch.setattr(data, "news", lambda q, n=8: [{"title": "Other news", "published": "3"}, {"title": "Third", "published": "4"}])
    monkeypatch.setattr(data, "bing_news", lambda q, n=8: pytest.fail("до Bing доходить не должно: 3 заголовка уже есть"))
    out = data.headlines("AAPL")
    assert [h["title"] for h in out] == ["Apple up", "Other news", "Third"]


def test_headlines_crypto_uses_crypto_rss_and_survives_source_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("blocked")
    monkeypatch.setattr(data, "alpaca_news", boom)
    monkeypatch.setattr(data, "crypto_rss", lambda c, n=8: [{"title": f"{c} rally", "published": "1"}])
    monkeypatch.setattr(data, "news", lambda q, n=8: [])
    monkeypatch.setattr(data, "bing_news", lambda q, n=8: [{"title": "Bitcoin ETF flows", "published": "2"}])
    assert [h["title"] for h in data.headlines("BTC/USD")] == ["BTC rally", "Bitcoin ETF flows"]
    monkeypatch.setattr(data, "crypto_rss", boom)
    monkeypatch.setattr(data, "bing_news", lambda q, n=8: [])
    assert data.headlines("BTC/USD") == []


def test_rss_parsing_and_crypto_filter(monkeypatch):
    xml = b"<rss><channel><item><title>Bitcoin hits high</title><pubDate>x</pubDate></item><item><title>Solana outage</title></item></channel></rss>"
    monkeypatch.setattr(data, "_get", lambda url, **kw: type("R", (), {"content": xml})())
    got = data.crypto_rss("BTC")
    assert [h["title"] for h in got] == ["Bitcoin hits high", "Bitcoin hits high"][:len(got)] and got[0]["title"] == "Bitcoin hits high"
    assert data.crypto_rss("SOL")[0]["title"] == "Solana outage"


def test_stock_candles_fall_back_to_alpaca_when_yahoo_blocked(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)

    def blocked(*a, **k):
        raise RuntimeError("429")
    monkeypatch.setattr(data, "yahoo_candles", blocked)
    seen = {}
    monkeypatch.setattr(data, "alpaca_bars", lambda syms, kind, days=90, timeframe="1Day": seen.update(days=days, tf=timeframe, kind=kind) or {"AAPL": df})
    assert len(data.stock_candles("AAPL", "1h", "60d")) == 5 and seen == {"days": 60, "tf": "1Hour", "kind": "stock"}
    with pytest.raises(ValueError):
        data.stock_candles("^VIX", "1d", "6mo")           # индексов в Alpaca нет


def test_yahoo_close_falls_back_to_fred_for_macro(monkeypatch):
    monkeypatch.setattr(data, "stock_candles", lambda *a, **k: (_ for _ in ()).throw(ValueError("blocked")))
    seen = []
    s = pd.Series([16.0, 17.0], index=pd.to_datetime(["2026-10-14", "2026-10-15"]))
    monkeypatch.setattr(sources, "fred_series", lambda sid, days=220: seen.append(sid) or s)
    assert sources.yahoo_close("^VIX", "1mo").iloc[-1] == 17.0 and seen == ["VIXCLS"]
    with pytest.raises(ValueError):
        sources.yahoo_close("GC=F")                       # золота нет во FRED: ошибка уходит вызывающему (market_levels пометит поле)


def test_bls_falls_back_to_fred_cpi(monkeypatch):
    monkeypatch.setattr(sources, "_bls", lambda: (_ for _ in ()).throw(OSError("ssl")))
    idx = pd.to_datetime(["2025-09-01", "2026-08-01", "2026-09-01"])
    cpi = pd.Series([300.0, 308.0, 311.1], index=idx)
    un = pd.Series([4.1, 4.2], index=pd.to_datetime(["2026-08-01", "2026-09-01"]))
    monkeypatch.setattr(sources, "fred_series", lambda sid, days=220: cpi if sid == "CPIAUCNS" else un)
    r = sources.bls_macro()
    assert r["cpi_period"] == "2026-09" and r["cpi_yoy"] == pytest.approx(0.037, abs=1e-4) and r["unemployment"] == 4.2 and r["src"] == "FRED"
