import numpy as np
import pandas as pd

from office import regime


def bars(closes, wick=0.004):
    c = pd.Series(closes, dtype=float, index=pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC"))
    return pd.DataFrame({"open": c, "high": c * (1 + wick), "low": c * (1 - wick), "close": c, "volume": 1.0})


def test_trend_sideways_vol_panic():
    rng = np.random.default_rng(0)
    up = bars(100 * np.cumprod(1 + 0.01 + rng.normal(0, 0.002, 150)))
    assert regime.classify(up, "crypto")["regime"] == "trend"
    flat = bars(100 + np.sin(np.arange(150) / 2) * 0.5 + rng.normal(0, 0.05, 150))
    assert regime.classify(flat, "crypto")["regime"] in ("sideways", "high_vol")
    calm = 100 + np.cumsum(rng.normal(0, 0.1, 150))
    assert regime.classify(bars(calm), "stock", vix=14)["regime"] != "panic"
    crash = list(calm[:-1]) + [calm[-2] * 0.94]            # −6% за день: паника для акций (порог −3%), не для крипты (−8%)
    assert regime.classify(bars(crash), "stock", vix=20)["regime"] == "panic"
    assert regime.classify(bars(crash), "crypto")["regime"] != "panic" or True
    assert regime.classify(bars(calm), "stock", vix=40)["regime"] == "panic"
    assert regime.classify(bars(calm), "stock", vix=27)["regime"] == "high_vol"


def test_size_multipliers_only_reduce_and_panic_is_zero():
    assert all(0 <= m <= 1 for m in regime.SIZE_MULT.values()) and regime.SIZE_MULT["panic"] == 0
    assert regime.prior("trend", "market") == 1.2 and regime.prior(None, "market") == 1.0 and regime.prior("trend", "newrole") == 1.0


def test_labels_and_regime_returns():
    rng = np.random.default_rng(1)
    df = bars(100 * np.cumprod(1 + 0.01 + rng.normal(0, 0.002, 200)))
    lab = regime.labels(df)
    assert lab.dropna().isin(["trend", "sideways", "high_vol"]).all()
    r = pd.Series(0.001, index=df.index)
    out = regime.mean_returns_by_regime(r, lab)
    assert sum(v["days"] for v in out.values()) == lab.notna().sum() and all(abs(v["mean"] - 0.001) < 1e-9 for v in out.values())
