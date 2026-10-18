"""Конфигурация: активы, риск-лимиты, пути. Секреты только из .env / окружения."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".claude" / "agents"
STATE_DIR = Path(os.environ.get("TO_STATE", ROOT))  # облако: каталог состояния (ветка state); телефон: корень проекта
DB_PATH = Path(os.environ.get("TO_DB", STATE_DIR / "office.db"))
BOARD_PATH = Path(os.environ.get("TO_BOARD", STATE_DIR / "board.db"))
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


# Не торгуем: ONDO (по решению владельца) и стейблкоины (цена ≈ 1 $, сигналов нет, только шум).
STABLE_BASES = frozenset({"USDC", "USDT", "DAI", "PYUSD", "USDG", "TUSD", "USDP", "BUSD", "FDUSD", "USDS", "USD1"})
EXCLUDED = frozenset({"ONDO/USD", "ONDOUSD"})
MIN_TURNOVER = {"crypto": 10_000_000.0, "stock": 10_000_000.0}   # крипта: оборот за 24 ч по CoinGecko ($); акции: средний дневной оборот за 20 дней по барам Alpaca IEX ($)


def excluded(symbol: str) -> bool:
    s = symbol.upper().replace("-", "/")
    return s in EXCLUDED or s.split("/")[0] in STABLE_BASES


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

# Все LLM-роли — Gemini (claude -p в облаке недоступен). Дневной бюджет запросов — llm.DAILY_BUDGET.
# Аналитики зала: главный потребитель бюджета; фоновые отделы — ограниченная доля; остальное (вычитка, пересказы) — мелочь.
ANALYST_ROLES = ("market", "news", "fundamentals", "quant")
GEMINI_EXTRA = ("macro", "newsdesk", "cryptodesk", "sectors", "portfolio_an", "factcheck", "proofread", "smartmoney_an", "picture", "postmortem", "weekly", "notify", "qa")
GEMINI_HALL = frozenset(ANALYST_ROLES)
GEMINI_BG = frozenset(GEMINI_EXTRA) - {"factcheck", "proofread", "notify", "qa"}


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
