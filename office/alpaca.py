"""Alpaca Paper: REST-клиент + AlpacaBroker. ТОЛЬКО paper — боевой хост отвергается при создании клиента.
Правила жёстко в коде: вход только side=buy, sell только закрывает существующий лонг, размер ≤ cash (плечо 1x), без опционов."""
import math
import os
import time

import requests

from .config import BY_NAME, COSTS

PAPER_HOST = "paper-api.alpaca.markets"
DUST_USD = 1.0   # позиции дешевле 1 $ — «пыль» после продажи с комиссией в активе (ONDO: 0,00000087 шт.): не позиция, в отчётах и планах не показываем


class AlpacaError(Exception):
    pass


class AlpacaClient:
    def __init__(self, key=None, secret=None, base=None, session=None):
        self.base = (base or os.environ.get("ALPACA_BASE_URL", f"https://{PAPER_HOST}/v2")).rstrip("/")
        if os.environ.get("ALPACA_PAPER", "true").lower() != "true":
            raise AlpacaError("отказ: ALPACA_PAPER != true (только paper)")
        if PAPER_HOST not in self.base:
            raise AlpacaError(f"отказ: base_url не paper ({self.base})")
        self.h = {"APCA-API-KEY-ID": key or os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_KEY_ID", ""),
                  "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET", "")}
        if not all(self.h.values()):
            raise AlpacaError("нет ALPACA_API_KEY/ALPACA_SECRET_KEY в .env")
        self.s = session or requests.Session()

    def _req(self, method, path, **kw):
        # GET ретраится (нестабильная сеть Android); POST/DELETE — нет: повторная отправка ордера опаснее ошибки
        for i in range(3 if method == "GET" else 1):
            try:
                r = self.s.request(method, f"{self.base}{path}", headers=self.h, timeout=20, **kw)
            except requests.RequestException as e:
                if i == 2 or method != "GET":
                    raise AlpacaError(f"{method} {path}: сеть {type(e).__name__}") from e
                time.sleep(2 * (i + 1))
                continue
            if r.status_code >= 500 and method == "GET" and i < 2:
                time.sleep(2 * (i + 1))
                continue
            break
        if r.status_code >= 400:
            raise AlpacaError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self):
        return self._req("GET", "/account")

    def clock(self):
        return self._req("GET", "/clock")

    def positions(self):
        return self._req("GET", "/positions")

    def position(self, symbol):
        """None, если позиции нет."""
        try:
            return self._req("GET", f"/positions/{symbol.replace('/', '')}")
        except AlpacaError as e:
            if " 404" in str(e):
                return None
            raise

    DATA = "https://data.alpaca.markets"

    def _snap(self, symbol: str) -> dict:
        if "/" in symbol:
            return self._data(f"{self.DATA}/v1beta3/crypto/us/snapshots", {"symbols": symbol})["snapshots"][symbol]
        return self._data(f"{self.DATA}/v2/stocks/snapshots", {"symbols": symbol, "feed": "iex"})[symbol]

    def price(self, symbol: str) -> float:
        """Последняя сделка по снапшоту Alpaca (независимый от Yahoo/Binance источник цены)."""
        snap = self._snap(symbol)
        return float((snap.get("latestTrade") or {}).get("p") or snap["dailyBar"]["c"])

    def day_change(self, symbol: str) -> float:
        """Изменение цены к вчерашнему закрытию по снапшоту Alpaca (тот же источник, что у счёта). Крипта: BTC/USD, акции: SPY."""
        snap = self._snap(symbol)
        px = float((snap.get("latestTrade") or {}).get("p") or snap["dailyBar"]["c"])
        return px / float(snap["prevDailyBar"]["c"]) - 1

    def _data(self, url, params):
        for i in range(3):
            try:
                r = self.s.get(url, headers=self.h, params=params, timeout=20)
            except requests.RequestException as e:
                if i == 2:
                    raise AlpacaError(f"data {url}: сеть {type(e).__name__}") from e
                time.sleep(2 * (i + 1))
                continue
            if r.status_code >= 500 and i < 2:
                time.sleep(2 * (i + 1))
                continue
            break
        if r.status_code >= 400:
            raise AlpacaError(f"data {url} -> {r.status_code}: {r.text[:150]}")
        return r.json()

    def account_view(self) -> dict:
        """Цифры результата ровно как у брокера: equity, cash, last_equity (закрытие прошлого дня), позиции с unrealized_pl."""
        a = self.account()
        pos = [{"symbol": p["symbol"], "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]), "price": float(p["current_price"]),
                "value": float(p["market_value"]), "pl": float(p["unrealized_pl"]), "plpc": float(p["unrealized_plpc"])}
               for p in self.positions() if abs(float(p.get("market_value") or 0)) >= DUST_USD]
        return {"equity": float(a["equity"]), "cash": float(a["cash"]), "last_equity": float(a.get("last_equity") or a["equity"]), "positions": pos}

    def orders(self, **params):
        return self._req("GET", "/orders", params=params)

    def order(self, oid):
        return self._req("GET", f"/orders/{oid}")

    def submit(self, body: dict):
        if body.get("side") not in ("buy", "sell"):
            raise AlpacaError("bad side")
        return self._req("POST", "/orders", json=body)

    def cancel(self, oid):
        return self._req("DELETE", f"/orders/{oid}")


def _sym(alpaca_symbol: str) -> str:
    return alpaca_symbol.replace("/", "")  # позиции Alpaca приходят как BTCUSD


class AlpacaBroker:
    """Интерфейс как у SimBroker: cash / equity / position / buy / sell. Состояние позиций — в Journal (зеркало) + сверка с Alpaca."""
    def __init__(self, client: AlpacaClient, journal, assets=None):
        self.c, self.j, self.assets, self.last_order_id = client, journal, BY_NAME, None

    @property
    def cash(self):
        return float(self.c.account()["cash"])

    def equity(self, prices=None):
        return float(self.c.account()["equity"])

    def position(self, name):
        return self.j.positions().get(name)

    def _wait_fill(self, oid, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            o = self.c.order(oid)
            if o["status"] in ("filled", "canceled", "expired", "rejected"):
                return o
            time.sleep(1)
        return self.c.order(oid)

    def buy(self, name, qty, ref_price, stop, take, ts, run_id=None):
        a = self.assets[name]
        if self.position(name):
            raise AlpacaError(f"позиция {name} уже открыта")
        if qty * ref_price > self.cash:
            raise AlpacaError("размер больше cash (плечо запрещено)")
        if a.kind == "stock":
            # настоящий bracket: вход + стоп + тейк на стороне брокера. Дробные акции в bracket не поддерживаются.
            o = self.c.submit({"symbol": a.alpaca, "qty": str(int(qty)), "side": "buy", "type": "market",
                               "time_in_force": "gtc", "order_class": "bracket",
                               "take_profit": {"limit_price": f"{take:.2f}"}, "stop_loss": {"stop_price": f"{stop:.2f}"}})
        else:
            o = self.c.submit({"symbol": a.alpaca, "qty": f"{qty:.6f}", "side": "buy", "type": "market", "time_in_force": "gtc"})
        o = self._wait_fill(o["id"])
        self.last_order_id = o["id"]
        if o["status"] != "filled":
            # рынок закрыт/не исполнился — отменяем, позицию не заводим (bracket-дочерние отменяются вместе с родителем)
            try:
                self.c.cancel(o["id"])
            except AlpacaError:
                pass
            o = self.c.order(o["id"])  # за время отмены ордер мог исполниться
            if float(o.get("filled_qty") or 0) > 0:
                raise AlpacaError(f"вход исполнен ({o['status']}, filled_qty={o['filled_qty']}) в момент отмены — проверь позицию {a.alpaca} вручную")
            raise AlpacaError(f"вход не исполнен: {o['status']}")
        price, fq = float(o["filled_avg_price"]), float(o["filled_qty"])
        fee_usd = 0.0
        if a.kind == "crypto":
            # Alpaca берёт комиссию крипты в получаемом активе → реальный остаток меньше filled_qty
            live = self.c.position(a.alpaca)
            have = float(live["qty_available"]) if live else 0.0
            fee_usd = max(0.0, (fq - have) * price)
            fq = have
        stop_oid = None
        if a.kind == "crypto":
            try:  # bracket для крипты запрещён API → отдельный защитный stop_limit (limit на 1% ниже стопа)
                qty_s = math.floor(fq * 1e6) / 1e6
                so = self.c.submit({"symbol": a.alpaca, "qty": f"{qty_s:.6f}", "side": "sell", "type": "stop_limit",
                                    "time_in_force": "gtc", "stop_price": f"{stop:.4f}", "limit_price": f"{stop * 0.99:.4f}"})
                stop_oid = so["id"]
            except AlpacaError as e:
                mo = self._wait_fill(self.c.submit({"symbol": a.alpaca, "qty": f"{math.floor(fq * 1e6) / 1e6:.6f}", "side": "sell",
                                                    "type": "market", "time_in_force": "gtc"})["id"])
                if mo["status"] == "filled":
                    raise AlpacaError(f"защитный стоп не встал ({e}) — позиция немедленно закрыта") from e
                # закрытие не подтверждено: позицию обязательно ведём в журнале, иначе она «потеряется» без стопа
                self._record_open(name, fq, price, stop, take, fee_usd, ts, run_id, None)
                raise AlpacaError(f"СТОП НЕ ВСТАЛ и закрытие не подтверждено ({mo['status']}) — проверь позицию {a.alpaca} вручную") from e
        self._record_open(name, fq, price, stop, take, fee_usd, ts, run_id, stop_oid)
        return {"price": price, "qty": fq, "fee": fee_usd}

    def _record_open(self, name, qty, price, stop, take, fee_usd, ts, run_id, stop_oid):
        self.j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)",
                  (name, qty, price, stop, take, fee_usd, run_id, ts))
        self.j.kv_set(f"stop_oid:{name}", stop_oid)
        self.j.add_fill(ts, name, "buy", qty, price, fee_usd, "entry", run_id)

    def sell(self, name, ref_price, ts, reason="signal"):
        """Закрыть существующий лонг (только полностью)."""
        p = self.position(name)
        if not p:
            raise AlpacaError(f"нет позиции {name}")
        a = self.assets[name]
        for o in self.c.orders(status="open", symbols=a.alpaca):
            try:
                self.c.cancel(o["id"])
            except AlpacaError:
                pass
        time.sleep(1)
        live = self.c.position(a.alpaca)
        qty = float(live["qty_available"]) if live else p["qty"]
        o = self._wait_fill(self.c.submit({"symbol": a.alpaca, "qty": f"{math.floor(qty * 1e6) / 1e6:.6f}" if a.kind == "crypto" else str(int(qty)),
                                           "side": "sell", "type": "market", "time_in_force": "gtc"})["id"])
        self.last_order_id = o["id"]
        if o["status"] != "filled":
            raise AlpacaError(f"закрытие не исполнено: {o['status']}")
        return self._book_close(name, float(o["filled_avg_price"]), ts, reason)

    def _book_close(self, name, price, ts, reason):
        p = self.position(name)
        exit_fee = p["qty"] * price * COSTS[self.assets[name].kind].fee  # оценка: комиссия Alpaca крипты в USD при продаже
        fees = p["fees"] + exit_fee
        pnl = (price - p["avg"]) * p["qty"] - fees
        risk = p["qty"] * (p["avg"] - p["stop"])
        self.j._x("DELETE FROM positions WHERE asset=?", (name,))
        self.j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (name, p["opened_ts"], ts, p["avg"], price, p["qty"], fees, pnl, pnl / risk if risk > 0 else None, p["run_id"], reason))
        self.j.add_fill(ts, name, "sell", p["qty"], price, exit_fee, reason, p["run_id"])
        return {"price": price, "pnl": pnl, "reason": reason}

    # ---------- сопровождение позиции (3.2): стоп только вверх, частичная фиксация ----------
    def _open_sells(self, a):
        return [o for o in self.c.orders(status="open", symbols=a.alpaca) if o.get("side") == "sell"]

    def _install_stop(self, a, qty, stop, take=None):
        """Защитный ордер на остаток: крипто — stop_limit, акции — OCO (стоп + тейк). Возвращает id (или None для акций с OCO)."""
        if a.kind == "crypto":
            so = self.c.submit({"symbol": a.alpaca, "qty": f"{math.floor(qty * 1e6) / 1e6:.6f}", "side": "sell", "type": "stop_limit", "time_in_force": "gtc",
                                "stop_price": f"{stop:.4f}", "limit_price": f"{stop * 0.99:.4f}"})
            return so["id"]
        body = {"symbol": a.alpaca, "qty": str(int(qty)), "side": "sell", "type": "limit", "time_in_force": "gtc", "order_class": "oco",
                "stop_loss": {"stop_price": f"{stop:.2f}"}, "take_profit": {"limit_price": f"{(take or stop * 1.1):.2f}"}}
        return self.c.submit(body)["id"]

    def move_stop(self, name, new_stop):
        """Поднять защитный стоп (никогда не опускает). True, если стоп переставлен."""
        p = self.position(name)
        if not p or new_stop <= p["stop"]:
            return False
        a = self.assets[name]
        done = False
        for o in self._open_sells(a):
            if o.get("type") in ("stop", "stop_limit"):
                body = {"stop_price": f"{new_stop:.4f}" if a.kind == "crypto" else f"{new_stop:.2f}"}
                if o.get("type") == "stop_limit":
                    body["limit_price"] = f"{new_stop * 0.99:.4f}" if a.kind == "crypto" else f"{new_stop * 0.99:.2f}"
                try:
                    r = self.c._req("PATCH", f"/orders/{o['id']}", json=body)   # Alpaca replace создаёт НОВЫЙ ордер с новым id
                    if a.kind == "crypto" and r.get("id"):
                        self.j.kv_set(f"stop_oid:{name}", r["id"])
                    done = True
                except AlpacaError:
                    done = self._restop(a, p, new_stop, o)   # замена не принята: отмена и новый ордер (с откатом)
        if done:
            self.j._x("UPDATE positions SET stop=? WHERE asset=?", (new_stop, name))
        return done

    def _restop(self, a, p, new_stop, old):
        qty = float(old["qty"])
        try:
            self.c.cancel(old["id"])
            time.sleep(1)
            oid = self._install_stop(a, qty, new_stop, p["take"])
        except AlpacaError:
            try:   # откат: вернуть прежний стоп, чтобы позиция не осталась без защиты
                oid = self._install_stop(a, qty, p["stop"], p["take"])
            except AlpacaError as e:
                self.sell(a.name, p["avg"], time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "no_stop")
                raise AlpacaError(f"стоп {a.name} не удалось переставить и вернуть — позиция закрыта по рынку ({e})") from e
            if a.kind == "crypto":
                self.j.kv_set(f"stop_oid:{a.name}", oid)
            return False
        if a.kind == "crypto":
            self.j.kv_set(f"stop_oid:{a.name}", oid)
        return True

    def sell_part(self, name, frac, ts, new_stop, reason="partial"):
        """Продать долю позиции по рынку и сразу поставить защиту на остаток (стоп new_stop, тейк прежний). Возвращает {'price','pnl','qty'} или None."""
        p = self.position(name)
        if not p:
            return None
        a = self.assets[name]
        for o in self._open_sells(a):
            try:
                self.c.cancel(o["id"])
            except AlpacaError:
                pass
        time.sleep(1)
        live = self.c.position(a.alpaca)
        have = float(live["qty_available"]) if live else p["qty"]
        sell_q = math.floor(have * frac * 1e6) / 1e6 if a.kind == "crypto" else int(have * frac)
        rest = have - sell_q
        price_hint = float(live["current_price"]) if live else p["avg"]
        if sell_q <= 0 or sell_q * price_hint < 10 or rest * price_hint < 10:
            self._reprotect(a, p, have, p["stop"])
            return None
        try:
            o = self._wait_fill(self.c.submit({"symbol": a.alpaca, "qty": f"{sell_q:.6f}" if a.kind == "crypto" else str(sell_q), "side": "sell", "type": "market", "time_in_force": "gtc"})["id"])
        except AlpacaError:
            self._reprotect(a, p, have, p["stop"])
            raise
        if o["status"] != "filled":
            self._reprotect(a, p, have, p["stop"])
            raise AlpacaError(f"частичная продажа не исполнена: {o['status']}")
        px = float(o["filled_avg_price"])
        live2 = self.c.position(a.alpaca)
        rest = float(live2["qty_available"]) if live2 else rest
        self._reprotect(a, p, rest, max(new_stop, p["stop"]))
        exit_fee = sell_q * px * COSTS[a.kind].fee
        entry_fee = p["fees"] * sell_q / p["qty"] if p["qty"] else 0.0
        pnl = (px - p["avg"]) * sell_q - entry_fee - exit_fee
        risk = sell_q * (p["avg"] - p["stop"])
        self.j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (name, p["opened_ts"], ts, p["avg"], px, sell_q, entry_fee + exit_fee, pnl, pnl / risk if risk > 0 else None, p["run_id"], reason))
        self.j._x("UPDATE positions SET qty=?, fees=?, stop=? WHERE asset=?", (rest, p["fees"] - entry_fee, max(new_stop, p["stop"]), name))
        self.j.add_fill(ts, name, "sell", sell_q, px, exit_fee, reason, p["run_id"])
        return {"price": px, "pnl": pnl, "qty": sell_q}

    def _reprotect(self, a, p, qty, stop):
        try:
            oid = self._install_stop(a, qty, stop, p["take"])
            if a.kind == "crypto":
                self.j.kv_set(f"stop_oid:{a.name}", oid)
        except AlpacaError as e:   # без защиты не оставляем
            self.sell(a.name, p["avg"], time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "no_stop")
            raise AlpacaError(f"защиту {a.name} поставить не удалось — позиция закрыта по рынку ({e})") from e

    DUST_USD = DUST_USD

    def live_positions(self) -> dict:
        return {p["symbol"]: p for p in self.c.positions() if abs(float(p.get("market_value") or 0)) >= self.DUST_USD}

    def orphans(self) -> list[str]:
        """Позиции на брокере, которых нет в журнале офиса (кроме пыли)."""
        mine = {_sym(self.assets[n].alpaca) for n in self.j.positions()}
        return [s for s in self.live_positions() if s not in mine]

    def sync(self, prices: dict, ts, highs: dict | None = None):
        """Сверка с Alpaca (источник истины): закрытые у брокера позиции → в журнал с ценой и временем из истории ордеров; крипто-тейк исполняется здесь.
        Возвращает [(имя, {price, pnl, reason, offline})]; offline=True — закрылась задолго до этой сверки (пока офис был выключен)."""
        import datetime as dt
        events = []
        live = self.live_positions()
        for name, p in list(self.j.positions().items()):
            a = self.assets[name]
            if _sym(a.alpaca) not in live:  # позиции нет на брокере → закрылась по стопу/тейку
                sells, soid = [], self.j.kv_get(f"stop_oid:{name}")
                if soid:  # крипто: точный защитный ордер
                    so = self.c.order(soid)
                    sells = [so] if so.get("status") == "filled" else []
                if not sells:
                    sells = [o for o in self.c.orders(status="closed", symbols=a.alpaca, direction="desc", limit=10)
                             if o["side"] == "sell" and o["status"] == "filled"]
                px = float(sells[0]["filled_avg_price"]) if sells else min(prices.get(name, p["stop"]), p["stop"])  # без данных — консервативно
                fill_ts, offline = ts, False
                if sells and sells[0].get("filled_at"):
                    ft = dt.datetime.fromisoformat(sells[0]["filled_at"].replace("Z", "+00:00"))
                    fill_ts = ft.isoformat(timespec="seconds")
                    try:
                        offline = (dt.datetime.fromisoformat(ts) - ft).total_seconds() > 900
                    except ValueError:
                        pass
                reason = "take" if px >= p["avg"] and p["take"] and px >= p["take"] * 0.998 else "stop"
                events.append((name, {**self._book_close(name, px, fill_ts, reason), "offline": offline}))
            else:
                ev = self._guard_live(name, p, a, prices.get(name), (highs or {}).get(name), ts)
                if ev:
                    events.append((name, ev))
        return events

    STOP_LIMIT_GAP = 0.99   # лимит защитного stop_limit крипты стоит на 1% ниже стопа

    def _guard_live(self, name, p, a, px, high, ts):
        """Живая позиция на проходе (раз в 30–60 мин). Alpaca не даёт для крипты две заявки продажи на один объём (проверено: 403 insufficient balance) и запрещает OCO,
        поэтому на брокере стоит ТОЛЬКО защитный stop_limit, а тейк — по цене на проходе:
        · тейк: цена на проходе ≥ тейка ИЛИ максимум свечей между проходами коснулся тейка, а цена всё ещё выше входа с издержками → продажа по рынку (заявка стопа при этом отменяется в sell());
        · стоп-разрыв: цена ушла ниже лимита stop_limit (заявка не исполнилась) → продажа по рынку;
        · иначе — убедиться, что защитная заявка на месте, и поставить её, если пропала."""
        if a.kind == "crypto" and px:
            fee = 2 * (COSTS["crypto"].fee + COSTS["crypto"].slippage)
            if p["take"] and (px >= p["take"] or (high and high >= p["take"] and px > p["avg"] * (1 + fee))):
                return self.sell(name, px, ts, "take")
            if p["stop"] and px <= p["stop"] * self.STOP_LIMIT_GAP:
                return self.sell(name, px, ts, "stop")
        self.ensure_protection(name, p, a)
        return None

    def ensure_protection(self, name, p=None, a=None) -> str:
        """Есть ли у позиции защитная заявка на брокере (stop/stop_limit); нет — ставим (крипта: stop_limit, акции: OCO стоп+тейк). Возвращает 'ok' | 'reinstalled'."""
        p, a = p or self.position(name), a or self.assets[name]
        if any(o.get("type") in ("stop", "stop_limit") for o in self._open_sells(a)):
            return "ok"
        live = self.c.position(a.alpaca)
        qty = float(live["qty_available"]) if live else 0.0
        if qty <= 0:
            return "ok"
        oid = self._install_stop(a, qty, p["stop"], p["take"])
        if a.kind == "crypto":
            self.j.kv_set(f"stop_oid:{name}", oid)
        return "reinstalled"
