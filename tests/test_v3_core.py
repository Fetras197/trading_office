import os

from office import config


def test_reexec_only_when_flag(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda exe, argv: calls.append((exe, argv)))
    monkeypatch.setattr(config, "RESTART_FILE", tmp_path / "RESTART")
    config.reexec_if_requested()
    assert calls == []
    (tmp_path / "RESTART").touch()
    config.reexec_if_requested("t")
    assert len(calls) == 1 and not (tmp_path / "RESTART").exists()  # флаг снят, чтобы не зациклиться


def test_localnow_uses_owner_timezone():
    assert config.localnow().utcoffset().total_seconds() == 3 * 3600  # Europe/Minsk


def test_indicator_snapshot_keeps_precision_for_micro_priced_assets():
    import numpy as np
    import pandas as pd
    from office import indicators
    idx = pd.date_range("2026-01-01", periods=300, freq="h", tz="UTC")
    px = 1e-6 * (1 + 0.01 * np.sin(np.arange(300) / 5))            # PEPE-подобная цена ~1e-6
    df = pd.DataFrame({"open": px, "high": px * 1.002, "low": px * .998, "close": px, "volume": 1e9}, index=idx)
    snap = indicators.snapshot(df)
    assert snap["atr14"] and snap["atr14"] > 0 and abs(snap["close"] - px[-1]) / px[-1] < 1e-6
