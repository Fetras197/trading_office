"""Supervisor 24/7. Тик 30 с. Расписание:
  15 мин: скринер (все крипто-пары + S&P500/NDX) -> top-5; сверка стопов/тейков с брокером (watch);
  1 ч:    полный цикл зала: крипта BTC/ETH/SOL; акции SPY/NVDA/AAPL/MSFT в часы биржи; + top-5 скринера + уже открытые позиции;
  6 ч:    сводка в Telegram.
Лаборатория — отдельный фоновый процесс (`python -m office lab`). STOP-файл = новые решения не принимаются."""
import concurrent.futures as cf
import fcntl
import time

from . import accuracy, config, data, lab, report, screener
from .alpaca import AlpacaBroker, AlpacaClient
from .board import Board
from .hall import Hall, utcnow
from .journal import Journal
from .llm import ClaudeBackend, RoleRouter

SCREEN_S, CYCLE_S, SUMMARY_S = 15 * 60, 3600, 6 * 3600
MAX_ASSETS = 10  # бюджет LLM на часовой цикл (core + top-5 + позиции)


class Supervisor:
    def __init__(self, board=None, journal=None, client=None, hall=None, notify=report.notify):
        config.load_env()
        self.board = board or Board(config.ROOT / "board.db")
        self.j = journal or Journal(config.DB_PATH)
        self.client = client or AlpacaClient()
        self.broker = AlpacaBroker(self.client, self.j)
        self.notify = notify
        self.hall = hall or Hall(self.board, self.j, self.broker, RoleRouter(), ClaudeBackend(), notify=notify,
                                 lab=lambda s: self._lab(s))
        self.next = {"screen": 0.0, "cycle": 0.0, "summary": time.time() + SUMMARY_S}
        self.candidates: list[str] = []
        self.stock_open = False

    def _lab(self, symbol):
        try:
            return lab.signal_for(self.board, symbol)
        except Exception:  # noqa: BLE001 — лаборатория не критична для зала
            return None

    # --- задачи ---
    def watch(self):
        """Сверка с брокером + эквити-снапшот; закрытия стопом/тейком -> Telegram."""
        prices = {}
        for n in self.j.positions():
            try:
                a = config.BY_NAME[n]
                prices[n] = float(data.candles(a, "1h", **({"limit": 5} if a.kind == "crypto" else {"rng": "5d"}))["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                pass
        for name, ev in self.broker.sync(prices, utcnow()):
            self.notify(f"🔔 {name}: позиция закрыта ({ev['reason']}) pnl {ev['pnl']:.2f}")
        eq = self.broker.equity()
        self.j.snap_equity(utcnow(), eq)
        if self.j.kv_get("start_equity") is None:
            self.j.kv_set("start_equity", eq)

    def screen(self):
        self.stock_open = bool(self.client.clock().get("is_open"))
        held = set(self.j.positions())
        top = screener.scan(self.board, self.client, self.stock_open, exclude=held | set(config.CRYPTO_CORE + config.STOCK_CORE))
        self.candidates = [r["symbol"] for r in top]

    def targets(self) -> list[str]:
        held = list(self.j.positions())
        core = list(config.CRYPTO_CORE) + (list(config.STOCK_CORE) if self.stock_open else [])
        out = []
        for s in held + core + self.candidates:
            if s not in out and (self.stock_open or "/" in s):  # акции вне часов биржи не торгуем
                out.append(s)
        return out[:MAX_ASSETS]

    def cycle(self):
        if config.STOP_FILE.exists():
            return
        res = []
        with cf.ThreadPoolExecutor(2) as ex:
            for r in ex.map(self.hall.run_asset, self.targets()):
                res.append(r)
                print(f"{utcnow()} {r.asset}: {r.status} {r.detail}", flush=True)
                if r.status in ("opened", "closed"):
                    self.notify(report.trade_message(self.board, r))
                elif r.status == "error":
                    self.notify(f"❗ {r.asset}: {r.detail}")
        try:
            accuracy.settle(self.board, self.j, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))
        except Exception:  # noqa: BLE001
            pass
        return res

    def summary(self):
        def pct(sym):
            try:
                a = config.make_asset(sym)
                d = data.candles(a, "1h", **({"limit": 12} if a.kind == "crypto" else {"rng": "5d"}))["close"]
                return float(d.iloc[-1] / d.iloc[-7] - 1)
            except Exception:  # noqa: BLE001
                return 0.0
        self.notify(report.summary(self.board, self.j, self.broker, {"SPY": pct("SPY"), "BTC": pct("BTC/USD")}))

    # --- главный цикл ---
    def tick(self, now=None):
        now = now or time.time()
        for name, fn, every in (("screen", self._screen_and_watch, SCREEN_S), ("cycle", self.cycle, CYCLE_S), ("summary", self.summary, SUMMARY_S)):
            if now >= self.next[name]:
                self.next[name] = now + every
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — упавшая задача не валит supervisor
                    print(f"{utcnow()} {name} упал: {type(e).__name__}: {e}", flush=True)
                    self.notify(f"❗ supervisor/{name}: {type(e).__name__}: {str(e)[:200]}")

    def _screen_and_watch(self):
        self.watch()
        self.screen()

    def run(self):
        lock = open(config.ROOT / ".supervisor.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("supervisor уже запущен")
        self.notify("▶️ Торговый зал запущен (paper)")
        while True:
            self.tick()
            time.sleep(30)


def lab_loop(sleep_s: int = 5):
    """Фоновая лаборатория: по кругу, самые давно не тестированные символы первыми (крипта и core — вперёд)."""
    config.load_env()
    board = Board(config.ROOT / "board.db")
    client = AlpacaClient()
    while True:
        u = screener.universe(client)
        syms = config.CRYPTO_CORE + config.STOCK_CORE + u["crypto"] + u["stock"]
        last = {r["symbol"]: r["t"] for r in board.q("SELECT symbol, MAX(ts) t FROM strategies GROUP BY symbol")}
        order = sorted(dict.fromkeys(syms), key=lambda s: last.get(s, ""))
        for s in order[:40]:
            try:
                lab.test_symbol(board, s)
            except Exception as e:  # noqa: BLE001
                print(f"{utcnow()} lab {s}: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(sleep_s)
