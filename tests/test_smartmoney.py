import io
import json

from office import smartmoney as sm


def row(addr, acc, m_pnl, m_roi, a_pnl):
    return {"ethAddress": addr, "accountValue": str(acc), "windowPerformances": [["day", {"pnl": "1", "roi": "0"}], ["month", {"pnl": str(m_pnl), "roi": str(m_roi)}], ["allTime", {"pnl": str(a_pnl), "roi": "1"}]]}


def test_streaming_selection_filters_and_ranks():
    rows = [row("a", 5e6, 9e5, 0.2, 3e6), row("b", 1e5, 9e6, 0.9, 3e6), row("c", 5e6, -1, 0.2, 3e6), row("d", 5e6, 3e5, 0.01, 3e6), row("e", 9e6, 2e6, 0.3, 1e5), row("f", 2e6, 5e5, 0.3, 2e6)]
    assert [x["address"] for x in sm.select_leaders(rows)] == ["a", "f"]            # b мал, c убыточен, d слабый ROI, e без истории
    big = json.dumps({"leaderboardRows": [row(f"x{i}", 2e6, 1e5 + i, 0.2, 2e6) for i in range(200)]})
    import ijson
    got = sm.select_leaders(ijson.items(io.BytesIO(big.encode()), "leaderboardRows.item"), n=5)
    assert len(got) == 5 and got[0]["address"] == "x199"


def pos(coin, szi, val, liq):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(val), "liquidationPx": str(liq) if liq else None}}


def test_aggregate_and_voice():
    states = [{"assetPositions": [pos("ETH", 10, 2e6, 3700), pos("BTC", -1, 1e6, 105000)]}, {"assetPositions": [pos("ETH", 5, 1.5e6, 3900), pos("ETH", -1, 0.2e6, 4300)]},
              {"assetPositions": [pos("ETH", 3, 1e6, 3000)]}]
    coins = sm.aggregate(states, {"ETH": 4000.0, "BTC": 100000.0})
    e = coins["ETH"]
    assert e["traders"] == 4 and e["net"] > 0.8 and e["liq_long"][3] == 1.5e6 and e["liq_long"][10] == 2e6 and e["liq_long"][20] == 0
    v = sm.signal_for("ETH", {"coins": coins})
    assert v["stance"] == "buy" and 0.5 < v["confidence"] <= 0.75 and "Hyperliquid" in v["rationale"] and "ликвидации их лонгов" in v["rationale"]
    assert sm.signal_for("BTC", {"coins": coins}) is None                          # < 3 трейдеров — своего мнения нет
    assert sm.signal_for("XXX", {"coins": coins}) is None


def test_refresh_uses_cached_leaders_and_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "CACHE", tmp_path)
    (tmp_path / "hl_leaders.json").write_text(json.dumps([{"address": "a"}, {"address": "b"}]))
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)

    def post(body):
        if body["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "ETH"}]}, [{"markPx": "4000"}]]
        return {"assetPositions": [pos("ETH", 1, 2e6, 3000)]}
    snap = sm.refresh(post=post)
    assert snap["leaders"] == 2 and snap["read"] == 2 and snap["coins"]["ETH"]["traders"] == 2 and (tmp_path / "smartmoney.json").exists()
    assert sm.load()["coins"]["ETH"]["longs_usd"] == 4e6
