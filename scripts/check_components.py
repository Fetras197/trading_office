"""Проверка компонентов запуском -> reports/components.md. Запуск: .venv/bin/python scripts/check_components.py"""
import json, os, subprocess, sys, time
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for l in (ROOT / ".env").read_text().splitlines():
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); os.environ.setdefault(k, v)
rows = []

def check(name, fn, ver=""):
    try:
        rows.append((name, ver, "OK", str(fn() or "")[:90]))
    except Exception as e:
        rows.append((name, ver, "FAIL", f"{type(e).__name__}: {str(e)[:80]}"))

def v(p):
    try: return metadata.version(p)
    except Exception: return "?"

def git(d):
    return subprocess.run(["git", "-C", str(ROOT / "vendor" / d), "log", "-1", "--format=%h %ad", "--date=short"], capture_output=True, text=True).stdout.strip()

def ta():
    from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
    return create_bull_researcher(object()).__name__
def hedge():
    sys.path.insert(0, str(ROOT / "vendor/ai-hedge-fund"))
    from hedge_fund.signals.buffett import BuffettAgent
    return BuffettAgent.get_system_prompt(None)[:30]
def talib_():
    import talib, numpy as np; return talib.RSI(np.random.rand(40))[-1]
def alpaca_():
    from alpaca.trading.client import TradingClient
    a = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True).get_account()
    return f"{a.status} equity={a.equity}"
def riskfolio():
    import riskfolio as rp, numpy as np, pandas as pd
    r = pd.DataFrame(np.random.randn(200, 3) * .01, columns=list("abc"))
    p = rp.Portfolio(returns=r); p.assets_stats(method_mu="hist", method_cov="hist")
    return p.optimization(model="Classic", rm="CVaR", obj="MinRisk").T.round(2).values.tolist()
def pypfopt():
    from pypfopt import EfficientFrontier, expected_returns, risk_models
    import numpy as np, pandas as pd
    px = pd.DataFrame(np.cumprod(1 + np.random.randn(200, 3) * .01, axis=0), columns=list("abc"))
    ef = EfficientFrontier(expected_returns.mean_historical_return(px), risk_models.sample_cov(px)); return ef.min_volatility()
def vbt():
    import vectorbt as vb, numpy as np, pandas as pd
    px = pd.Series(np.cumprod(1 + np.random.randn(300) * .01))
    return round(vb.Portfolio.from_signals(px, px.vbt.crossed_above(px.rolling(10).mean()), px.vbt.crossed_below(px.rolling(10).mean())).total_return(), 3)
def yf():
    import yfinance as y; return len(y.Ticker("SPY").history(period="5d"))
def claude():
    r = subprocess.run(["claude", "-p", "Ответь одним словом: ok", "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--setting-sources", "", "--strict-mcp-config", "--model", "haiku"], capture_output=True, text=True, timeout=120, cwd="/tmp")
    assert r.returncode == 0, r.stderr[:80]; return r.stdout.strip()[:30]
def gemini():
    import requests
    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json={"contents": [{"parts": [{"text": "say ok"}]}]}, timeout=40)
    r.raise_for_status(); return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()[:20]
def mcp():
    out = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=200, cwd=str(ROOT), env={**os.environ, "PATH": os.environ["PATH"] + ":/root/.local/bin"}).stdout
    return " | ".join(l.split(" - ")[0].split(":")[0] + (" ✔" if "✔" in l else " ✘") for l in out.splitlines() if " - " in l)
def plugins():
    out = subprocess.run(["claude", "plugin", "list"], capture_output=True, text=True, timeout=60).stdout
    return ", ".join(p for p in ("quantitative-trading", "equity-research", "financial-analysis") if p in out)

check("TradingAgents (vendor, ядро)", ta, f"{v('tradingagents')} @ {git('TradingAgents')}")
check("ai-hedge-fund (Buffett/Munger/Druckenmiller)", hedge, git("ai-hedge-fund"))
check("Vibe-Trading (справочно)", lambda: (ROOT / "vendor/Vibe-Trading/README.md").exists() or 1 / 0, git("Vibe-Trading"))
check("TA-Lib", talib_, v("ta-lib"))
check("alpaca-py + Alpaca paper", alpaca_, v("alpaca-py"))
check("riskfolio-lib (CVaR)", riskfolio, v("riskfolio-lib"))
check("PyPortfolioOpt", pypfopt, v("pyportfolioopt"))
check("vectorbt", vbt, v("vectorbt"))
check("yfinance", yf, v("yfinance"))
check("claude -p (Claude)", claude, "cli")
check("Gemini REST", gemini, "gemini-3.1-flash-lite")
check("Claude plugins", plugins)
check("MCP (claude mcp list)", mcp)
rows.append(("transformers/FinBERT", "-", "SKIP", "решение владельца: torch не ставится (OOM) → сентимент Gemini/Claude по заголовкам, DECISIONS #16"))
md = "# Компоненты (" + time.strftime("%Y-%m-%d %H:%M") + ")\n\n| Компонент | Версия | Статус | Проверка |\n|---|---|---|---|\n" + "\n".join(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows) + "\n"
(ROOT / "reports/components.md").write_text(md); print(md)
