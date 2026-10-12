"""Портфельные оценки: CVaR (Riskfolio-Lib) для risk-менеджера, распределение (PyPortfolioOpt) для PM."""
import numpy as np
import pandas as pd

from . import data
from .config import make_asset


def daily_closes(symbols: list[str]) -> pd.DataFrame:
    cols = {}
    for s in symbols:
        a = make_asset(s)
        try:
            d = data.candles(a, "1d", **({"limit": 400} if a.kind == "crypto" else {}))["close"]
            cols[s] = d.groupby(d.index.tz_localize(None).normalize()).last()
        except Exception:  # noqa: BLE001 — нет данных по символу: исключаем из портфеля
            continue
    return pd.DataFrame(cols).ffill().dropna().tail(180)


def cvar95(returns: pd.DataFrame, weights: dict[str, float]) -> float:
    """Дневной CVaR 95% портфеля (доля), Riskfolio-Lib. Положительное число = ожидаемый убыток в худших 5% дней."""
    import riskfolio as rp
    w = np.array([weights.get(c, 0.0) for c in returns.columns])
    return float(rp.RiskFunctions.CVaR_Hist((returns.to_numpy() @ w).reshape(-1, 1), alpha=0.05))


def allocate(prices: pd.DataFrame, max_w: float = 0.25) -> dict[str, float]:
    """PyPortfolioOpt: max Sharpe (фоллбек min-vol) с CASH-активом; вес каждого актива ≤ max_w."""
    from pypfopt import EfficientFrontier, expected_returns, risk_models
    px = prices.copy()
    px["CASH"] = 1.0
    mu = expected_returns.mean_historical_return(px, frequency=252)
    cov = risk_models.sample_cov(px, frequency=252) + np.eye(px.shape[1]) * 1e-8
    for opt in ("max_sharpe", "min_volatility"):
        try:
            ef = EfficientFrontier(mu, cov, weight_bounds=(0, 1))
            ef.add_constraint(lambda w: w[: -1] <= max_w)
            getattr(ef, opt)()
            return {k: float(v) for k, v in ef.clean_weights().items() if k != "CASH"}
        except Exception:  # noqa: BLE001 — не сошлось (все mu < 0 и т.п.)
            continue
    return {c: 0.0 for c in prices.columns}
