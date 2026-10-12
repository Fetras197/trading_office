"""Конфигурация: активы, риск-лимиты, пути. Секреты только из .env / окружения."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".claude" / "agents"
DB_PATH = Path(os.environ.get("TO_DB", ROOT / "office.db"))
STOP_FILE = ROOT / "STOP"
RESTART_FILE = ROOT / "RESTART"
TZ_NAME = os.environ.get("OFFICE_TZ", "Europe/Minsk")  # часовой пояс владельца: расписание и время в сообщениях


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
    name: str          # = символ Alpaca: "BTC/USD" | "AAPL" (ключ в журнале; вид актива виден из имени)
    kind: str          # crypto | stock
    ticker: str        # источник свечей: BTCUSDT (Binance) | AAPL (Yahoo)
    alpaca: str        # символ Alpaca
    hl_coin: str = ""  # Hyperliquid coin для деривативов
    cg_id: str = ""    # CoinGecko id


_CG = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def make_asset(symbol: str) -> Asset:
    """Любой символ Alpaca -> Asset (универсум скринера динамический)."""
    symbol = symbol.upper().replace("-", "/")
    if "/" in symbol:
        base = symbol.split("/")[0]
        return Asset(symbol, "crypto", base + "USDT", symbol, base, _CG.get(base, ""))
    return Asset(symbol, "stock", symbol, symbol)


CRYPTO_CORE = ["BTC/USD", "ETH/USD", "SOL/USD"]            # цикл каждый час
STOCK_CORE = ["SPY", "NVDA", "AAPL", "MSFT"]               # цикл в часы биржи
ASSETS = [make_asset(s) for s in CRYPTO_CORE + STOCK_CORE]


class _ByName(dict):
    def __missing__(self, k):
        return make_asset(k)


BY_NAME = _ByName({a.name: a for a in ASSETS})


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

# Кто отвечает за роль: аналитики — Gemini, остальные — Claude (claude -p)
ANALYST_ROLES = ("market", "news", "fundamentals", "quant", "buffett", "munger", "druckenmiller")
# сбор данных/новости/проверка фактов/вычитка — Gemini; решения (картина дня, разборы сделок, ответы на вопросы) — Claude
GEMINI_EXTRA = ("macro", "newsdesk", "cryptodesk", "sectors", "portfolio_an", "factcheck", "proofread", "smartmoney_an")
ROLE_BACKEND = {r: "gemini" for r in ANALYST_ROLES + GEMINI_EXTRA}
ROLE_MODELS = {"executor": "haiku"}  # остальные Claude-роли — DEFAULT_CLAUDE_MODEL
DEFAULT_CLAUDE_MODEL = "sonnet"


def localnow():
    """Текущее время в часовом поясе владельца (проект живёт в proot с другим системным поясом)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(TZ_NAME))


def restart_requested() -> bool:
    return RESTART_FILE.exists()


def reexec_if_requested(note: str = "") -> None:
    """Самоперезапуск без Termux: файл RESTART -> os.execv тем же интерпретатором с теми же аргументами (PID не меняется,
    поэтому office.sh status/alive продолжают работать). Вызывать только между задачами."""
    if not RESTART_FILE.exists():
        return
    import os
    import sys
    RESTART_FILE.unlink(missing_ok=True)
    print(f"RESTART: перезапуск процесса {note}", flush=True)
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
