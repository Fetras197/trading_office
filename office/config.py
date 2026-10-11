"""Конфигурация: активы, риск-лимиты, пути. Секреты только из .env / окружения."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".claude" / "agents"
DB_PATH = Path(os.environ.get("TO_DB", ROOT / "office.db"))
STOP_FILE = ROOT / "STOP"


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Asset:
    name: str          # BTC (ключ в журнале)
    kind: str          # crypto | stock
    ticker: str        # источник свечей: BTCUSDT (Binance) | AAPL (Yahoo)
    alpaca: str        # символ Alpaca: BTC/USD | AAPL
    hl_coin: str = ""  # Hyperliquid coin для деривативов
    cg_id: str = ""    # CoinGecko id


ASSETS = [
    Asset("BTC", "crypto", "BTCUSDT", "BTC/USD", "BTC", "bitcoin"),
    Asset("ETH", "crypto", "ETHUSDT", "ETH/USD", "ETH", "ethereum"),
    Asset("SOL", "crypto", "SOLUSDT", "SOL/USD", "SOL", "solana"),
    Asset("AAPL", "stock", "AAPL", "AAPL"),
    Asset("MSFT", "stock", "MSFT", "MSFT"),
    Asset("NVDA", "stock", "NVDA", "NVDA"),
    Asset("SPY", "stock", "SPY", "SPY"),
]
BY_NAME = {a.name: a for a in ASSETS}


@dataclass(frozen=True)
class Limits:
    risk_per_trade: float = 0.02    # макс. убыток до стопа, доля эквити
    max_notional: float = 0.25      # макс. размер позиции, доля эквити
    daily_stop: float = -0.05       # дневной стоп (от эквити на начало UTC-суток)
    max_trades_day: int = 5         # макс. входов в сутки
    max_positions: int = 3          # макс. одновременных позиций
    min_stop_atr: float = 0.5
    max_stop_atr: float = 4.0
    min_notional_usd: float = 10.0


@dataclass(frozen=True)
class Costs:
    fee: float = 0.0
    slippage: float = 0.0005  # 0.05%


COSTS = {"crypto": Costs(fee=0.0025), "stock": Costs(fee=0.0)}  # Alpaca: крипто 15-25 bps, акции 0


START_CASH = 100_000.0  # только для бэктеста (как на paper-счёте)

# Кто отвечает за роль: аналитики — Gemini, остальные — Claude
ROLE_BACKEND = {"tech_analyst": "gemini", "fundamental_analyst": "gemini", "news_sentiment": "gemini",
                "onchain_derivs": "gemini"}  # остальные — claude
ROLE_MODELS = {"executor": "haiku", "journal": "haiku"}  # модели Claude по ролям; остальные Claude-роли — DEFAULT_CLAUDE_MODEL
DEFAULT_CLAUDE_MODEL = "sonnet"
