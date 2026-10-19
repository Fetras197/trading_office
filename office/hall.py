"""Торговый зал (v4, облако): один актив за проход, без claude -p.
Данные -> технический сигнал (код) -> блокировки (лимиты, режим «паника», события, остывание после стопа) -> аналитики (Gemini, с кэшем 90 мин)
-> правило входа (entry.py: «за» не меньше половины и больше «против»; неполные данные = половинный объём) -> RiskGate (лимиты риска, код) -> Alpaca paper.
Каждая отсечка пишется в journal.nogo (для вечернего блока «Почему не вошли»)."""
import concurrent.futures as cf
import dataclasses
import datetime as dt
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import agents, config, data, desks, entry, events, factcheck, indicators, lab, regime, views
from .alpaca import AlpacaError
from .config import COSTS, Limits, make_asset
from .board import PICTURE_MAX_AGE_H
from .llm import LLMError, LLMUnavailable, budget_allow
from .risk import RiskGate

VOTES_TTL_H = 1.5   # свежие голоса аналитиков по активу переиспользуем (экономия бюджета Gemini)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Result:
    asset: str
    status: str   # skipped | hold | rejected | opened | closed | error
    detail: str = ""
    cycle_id: str = ""
    order: dict = field(default_factory=dict)


class Skip(Exception):
    """Актив пропускается в этом проходе (нет данных, лимиты LLM)."""


class Hall:
    def __init__(self, board, journal, broker, router, lab=None, notify=None, limits: Limits = Limits(), auto_trade=True, smart=None, fresh_rounds: int = 3):
        self.board, self.j, self.broker, self.router = board, journal, broker, router
        self.smart = smart   # coin -> {stance, confidence, rationale} | None (умные деньги)
        self.lab, self.notify, self.gate, self.auto_trade = lab, notify or (lambda t: None), RiskGate(limits), auto_trade
        self.exec_lock = threading.Lock()
        self.stock_open = True   # Office выставляет на каждом проходе: акции вне часов биржи отделы не берут
        self.fresh_left = fresh_rounds   # сколько новых раундов аналитиков можно запустить за проход (бюджет Gemini)
        self._fresh_lock = threading.Lock()

    # ---------- состояние ----------
    def _account(self, asset, ts):
        eq = self.broker.equity()
        k = f"day_start:{ts[:10]}"
        ds = self.j.kv_get(k)
        if ds is None:
            self.j.kv_set(k, eq)
            ds = eq
        pos = self.broker.position(asset.name)
        return {"equity": eq, "cash": self.broker.cash, "day_start_equity": ds, "day_pnl_pct": round(eq / ds - 1, 4) if ds else 0,
                "entries_today": self.j.entries_on(ts[:10]), "open_positions": len(self.j.positions()),
                "position": {k: pos[k] for k in ("qty", "avg", "stop", "take")} if pos else None}

    def _nogo(self, ts, asset, stage, reason, tech, votes=None, score=None):
        self.j.add_nogo(ts, asset.name, stage, reason, entry.proximity(tech, votes) if score is None else score)

    # ---------- аналитики ----------
    def _payloads(self, asset, b) -> dict:
        p = {"market": {"market": {k: b[k] for k in ("indicators_1h", "indicators_4h", "last_candles_1h", "tradingview")}},
             "news": {"asset": asset.name, "news": b["news"]},
             "fundamentals": {"asset": asset.name, "kind": asset.kind, "fundamentals": b["fundamentals"]}}
        try:
            p["quant"] = {"asset": asset.name, "quant": agents.quant_stats(asset)}
        except Exception as e:  # noqa: BLE001
            p["quant"] = {"error": f"unavailable: {type(e).__name__}"}
        if asset.kind == "crypto":
            p["fundamentals"]["derivatives"] = b.get("derivatives")
        sl, tp = entry.levels(100.0, 1.0, "trend", asset.kind)
        q = {"decision": f"open a small LONG position in {asset.name} now? Stop {100 - sl:g}*ATR below, take-profit {tp - 100:g}*ATR above, horizon ~24-72h. "
                         "buy = risk/reward from your domain looks favourable; hold = no edge; sell = expect decline; abstain = your data is missing."}
        return {k: {**v, "question": q} for k, v in p.items()}

    def _ask_analysts(self, asset, b) -> list[dict]:
        payloads = self._payloads(asset, b)

        def one(role):
            try:
                n = agents.run_analyst(role, self.router, payloads[role])
            except (LLMError, KeyError) as e:   # лимит/невалидный ответ = воздержался
                n = {"stance": "abstain", "confidence": 0.0, "rationale": f"{type(e).__name__}: {str(e)[:150]}"}
            return {"role": role, **n}
        with cf.ThreadPoolExecutor(3) as ex:
            out = list(ex.map(one, list(payloads)))
        if self.smart and asset.kind == "crypto":
            try:
                sm = self.smart(asset.hl_coin)
            except Exception:  # noqa: BLE001 — внешний источник не критичен
                sm = None
            if sm:
                out.append({"role": "smartmoney", "stance": sm["stance"], "confidence": sm["confidence"], "rationale": sm["rationale"]})
        return out

    def _votes(self, asset, b, now=None) -> tuple[list[dict], str]:
        """(голоса, источник: 'fresh'|'cache'|'queue'|'budget'). queue/budget — голосов нет: лимит раундов за проход или бюджет Gemini."""
        now = now or time.time()
        c = self.j.kv_get(f"votes:{asset.name}")
        if c and now - c["t"] < VOTES_TTL_H * 3600:
            return c["analysts"], "cache"
        if not budget_allow("market"):
            return [], "budget"
        with self._fresh_lock:
            if self.fresh_left <= 0:
                return [], "queue"
            self.fresh_left -= 1
        analysts = self._ask_analysts(asset, b)
        if entry.count_votes(analysts).live:
            self.j.kv_set(f"votes:{asset.name}", {"t": now, "analysts": analysts})
        return analysts, "fresh"

    def _ban(self, asset) -> str:
        """Действующий запрет «Картины дня» по активу с конкретной причиной ('' если нет). Запрет без причины и срока не действует."""
        pic = self.board.latest("picture", max_age_h=PICTURE_MAX_AGE_H)
        e = events.picture_bans(pic["data"]).get(asset.name) if pic else None
        return f"{e['reason']} (до {views.when_text(e['until'])} по Минску)" if e else ""

    def _publish_votes(self, cid, asset, analysts, price):
        for r in analysts:
            r["id"] = self.board.publish(cid, r["role"], asset.name, f"{r['stance']}: {r['rationale']}", r["confidence"], data={**r, "price": price})

    def _spread(self, asset):
        cl = getattr(self.broker, "c", None)
        if cl is None or not hasattr(cl, "spread"):
            return None
        try:
            return cl.spread(asset.alpaca)
        except Exception:  # noqa: BLE001 — котировки недоступны: не блокируем
            return None

    def _cross_price(self, asset, price):
        """Цена из свечей против снапшота Alpaca (независимый источник): расхождение > 1,5% — данные ненадёжны."""
        if price <= 0:
            raise Skip(f"нулевая цена в свечах ({price})")
        cl = getattr(self.broker, "c", None)
        if cl is None or not hasattr(cl, "price"):
            return
        try:
            other = cl.price(asset.alpaca)
        except Exception:  # noqa: BLE001 — второй источник недоступен: не блокируем
            return
        if price > 0 and other > 0 and factcheck.price_gap(price, other) > 0.015:
            raise Skip(f"цены источников расходятся ({price:.6g} против {other:.6g})")

    # ---------- основной проход ----------
    def run_asset(self, symbol: str) -> Result:
        asset, cid, ts = make_asset(symbol), uuid.uuid4().hex[:12], utcnow()
        try:
            return self._run(asset, cid, ts)
        except Skip as e:
            self.j.add_nogo(ts, asset.name, "data", str(e), 0.0)
            return Result(asset.name, "skipped", str(e), cid)
        except (LLMError, LLMUnavailable) as e:
            return Result(asset.name, "skipped", f"{type(e).__name__}: {e}", cid)
        except Exception as e:  # noqa: BLE001 — сбой одного актива не валит проход по остальным
            return Result(asset.name, "error", f"{type(e).__name__}: {e}"[:200], cid)

    def _run(self, asset, cid, ts) -> Result:
        try:   # дёшево: свечи + индикаторы; тяжёлый пакет (новости, фундаментал, TradingView) — только если есть сигнал
            df = data.candles(asset, "1h", **({"limit": 500} if asset.kind == "crypto" else {"rng": "60d"}))
            if len(df) < 210:
                raise Skip(f"мало свечей ({len(df)})")
            ind = indicators.snapshot(df)
        except Skip:
            raise
        except Exception as e:  # noqa: BLE001
            raise Skip(f"нет данных: {type(e).__name__}") from e
        price, atr = ind["close"], ind["atr14"]
        if not atr:
            raise Skip("нет ATR")
        self._cross_price(asset, price)
        run_id = self.j.add_run(ts, asset.name, price, atr, cid)
        acct = self._account(asset, ts)
        if acct["position"]:   # позицию ведёт manage.py (стоп/тейк/частичная фиксация); новых входов по активу нет
            self.j.set_summary(run_id, "hold: позиция открыта")
            return Result(asset.name, "hold", "позиция уже открыта", cid)
        tech = entry.technical(ind)

        if tech.signal:   # вход для теневого портфеля «только технический сигнал»: те же стоп/тейк, что получил бы офис (с экономикой), но без отделов и голосов
            p0 = entry.trade_plan(price, atr, tech.kind, asset.kind, COSTS[asset.kind])
            if p0.ok:
                self.j.add_signal(ts, asset.name, tech.kind, price, atr, p0.stop, p0.take)

        def hold(stage, why, votes=None, score=None):
            self.j.set_summary(run_id, f"{stage}: {why}")
            self._nogo(ts, asset, stage, why, tech, votes, score)
            return Result(asset.name, "hold", why, cid)

        # --- блокировки: дневной стоп −5% счёта (общий) и защитные запреты; лимиты позиций/входов — по отделам ---
        l = self.gate.l
        if self.gate.trading_halted(acct["equity"], acct["day_start_equity"]):
            return hold("limits", f"дневной стоп {l.daily_stop:.0%} достигнут", score=0.95 if tech.signal else None)
        if config.STOP_FILE.exists():
            return hold("halt", "включён режим паузы (STOP): новые входы запрещены")
        rg = regime.current(self.board, asset.kind)
        rname = rg["regime"] if rg else None
        if rname == "panic":
            return hold("regime", "режим рынка «паника»: новые входы запрещены")
        ev = events.blocked(self.board, asset.name, asset.kind)
        if ev:
            return hold("event", f"событие: {ev}")
        last = self.j.q("SELECT exit_ts FROM trades WHERE asset=? AND exit_reason='stop' ORDER BY id DESC LIMIT 1", (asset.name,))
        if last:
            try:
                t = dt.datetime.fromisoformat(last[0]["exit_ts"])
                t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
                if (dt.datetime.now(dt.timezone.utc) - t).total_seconds() < entry.COOLDOWN_H * 3600:
                    return hold("cooldown", f"остывание: стоп по активу был меньше {entry.COOLDOWN_H:g} ч назад")
            except (ValueError, TypeError):
                pass
        patterns = []
        if any(asset.name in (self.j.kv_get(f"labdesk:{d['id']}") or []) for d in desks.active(self.j) if d["kind"] == "lab"):
            try:
                sg = lab.signal_for(self.board, asset.name)
            except Exception:  # noqa: BLE001 — лаборатория не критична
                sg = None
            if sg and sg.get("stance") == "buy":
                patterns.append("lab")
        if tech.signal:
            patterns.append(tech.kind)
        if not patterns:
            return hold("signal", "нет технического сигнала: " + ("; ".join(tech.notes[:2]) or "условия не выполнены"))

        # --- общий лимит альткоинов: не больше MAX_ALT_POSITIONS открытых позиций по ВСЕМ отделам (BTC и ETH не считаются) ---
        is_alt = "/" in asset.name and asset.name not in entry.ALT_EXEMPT
        if is_alt and len(desks.alt_positions(self.j)) >= entry.MAX_ALT_POSITIONS:
            return hold("alts", f"уже открыто {len(desks.alt_positions(self.j))} альткоин-позиции ({', '.join(a.split('/')[0] for a in desks.alt_positions(self.j))}); общий лимит на все отделы — {entry.MAX_ALT_POSITIONS}", score=0.85)

        # --- спред: BONK 18.10 — круг «купили-продали» стоил 2,7% (≈134 $). Спред шире порога — не входим; реальный спред входит в расчёт экономики как проскальзывание ---
        costs = COSTS[asset.kind]
        sp = self._spread(asset)
        if sp is not None:
            if sp > entry.MAX_SPREAD:
                return hold("spread", f"спред {sp * 100:.2f}% шире допустимых {entry.MAX_SPREAD * 100:.2f}%: круг «купили-продали» съест прибыль", score=0.8)
            costs = dataclasses.replace(costs, slippage=max(costs.slippage, sp / 2))

        # --- отдел: первый подходящий со свободным лимитом (3 позиции, 5 входов/сутки, остаток бюджета) ---
        desk, why_no = None, ""
        for pat in patterns:
            desk, why_no = desks.route(self.j, asset.name, pat, acct["equity"], ts[:10], self.stock_open)
            if desk:
                pattern = pat
                break
        if not desk:
            return hold("desk", why_no, score=0.8)
        # --- экономика: цель с комиссиями ≥ 2× риска с комиссиями (крипта ещё и ≥ 4× комиссии круга) ---
        plan = entry.trade_plan(price, atr, pattern, asset.kind, costs)
        if not plan.ok:
            return hold("economics", plan.reason, score=0.85)

        # --- полный пакет данных и голоса аналитиков (Gemini) ---
        try:
            b = data.bundle(asset, df)
        except Exception as e:  # noqa: BLE001
            raise Skip(f"нет данных: {type(e).__name__}") from e
        analysts, src = self._votes(asset, b)
        votes = entry.count_votes(analysts)
        if votes.live:
            self._publish_votes(cid, asset, analysts, price)
        council = entry.majority(votes)
        gaps = entry.data_gaps(b, votes)
        if not council:
            # режим «только технический сигнал»: аналитики не одобрили (или голосов нет) — входим половинным объёмом, если совет не наложил запрет с конкретной причиной
            ban = self._ban(asset)
            if not entry.TECH_ONLY or ban:
                why = (f"запрет совета: {ban}" if ban else
                       {"queue": "очередь аналитиков: за проход разбирается ограниченное число активов (бюджет Gemini)", "budget": "дневной бюджет Gemini исчерпан"}.get(src) if not votes.live else
                       f"аналитики не одобрили: «за» {votes.buy}, «против» {votes.sell}, сомневаются {votes.hold}")
                return hold("ban" if ban else "votes", why or "нет голосов аналитиков", votes, score=0.9 if ban else None)
            gaps = [("аналитики не одобрили" if votes.live else "нет голосов аналитиков")] + gaps

        # --- решение: размер ---
        if pattern in entry.COUNTER:
            gaps.append("сделка против тренда")
        mult = entry.HALF if gaps else 1.0
        mult = min(mult, regime.SIZE_MULT.get(rname, 1.0))
        stop, take = plan.stop, plan.take
        mode = "council" if council else "tech_only"
        why = f"отдел «{desk['name']}», сигнал «{entry.TECH_RU[pattern]}», «за» {votes.buy} из {votes.live}" + (f"; половинный объём: {'; '.join(gaps)}" if gaps else "")
        self.board.publish(cid, "decision", asset.name, f"buy: {why}", 0.6, data={"tech": pattern, "desk": desk["id"], "mode": mode, "votes": votes.__dict__, "gaps": gaps, "size_mult": mult, "stop": stop, "take": take, "price": price})

        # --- RiskGate (код) + исполнение: под локом и со свежим состоянием счёта ---
        with self.exec_lock:
            if config.STOP_FILE.exists():
                return hold("halt", "включён режим паузы (STOP): новые входы запрещены")
            acct = self._account(asset, utcnow())
            live = getattr(self.broker, "live_positions", lambda: {})()
            if asset.alpaca.replace("/", "") in live:
                return hold("broker", "по активу уже есть позиция у брокера", votes, score=0.9)
            if is_alt and len(desks.alt_positions(self.j)) >= entry.MAX_ALT_POSITIONS:   # другой поток мог открыть альткоин, пока шла проверка аналитиков
                return hold("alts", f"общий лимит {entry.MAX_ALT_POSITIONS} альткоин-позиций достигнут", votes, score=0.85)
            cap = desks.capacity(self.j, desk, acct["equity"], ts[:10])        # свежие лимиты отдела под локом исполнения
            if not cap["ok"]:
                return hold("desk", f"«{desk['name']}»: {cap['why']}", votes, score=0.9)
            v = RiskGate(desks.limits_for(desk, acct["equity"])).check_entry(
                kind=asset.kind, costs=costs, entry=price, stop=stop, atr=atr, equity=acct["equity"], day_start_equity=acct["day_start_equity"],
                entries_today=cap["entries"], open_positions=cap["open"], cash=min(acct["cash"], cap["free"]), has_position=bool(acct["position"]), size_mult=mult)
            did = self.j.add_decision(run_id, ts, asset.name, "buy", price, stop, take, v.qty, "ok" if v.ok else "rejected", v.reason)
            if not v.ok:
                self._nogo(ts, asset, "riskgate", v.reason, tech, votes, score=0.95)
                return Result(asset.name, "rejected", v.reason, cid)
            if not self.auto_trade:
                return Result(asset.name, "hold", "dry-run: ордер не отправлен", cid)
            val = getattr(self.broker, "validate_entry", None)
            if val:   # пара без валидной защитной заявки (шаг цены, минимальное количество) не покупается вообще
                try:
                    val(asset.name, v.qty, price, stop)
                except AlpacaError as e:
                    self._nogo(ts, asset, "protection", str(e)[:200], tech, votes, score=0.9)
                    return Result(asset.name, "skipped", f"защитная заявка невалидна — не покупаем: {e}"[:220], cid)
            try:
                o = self.broker.buy(asset.name, v.qty, price, stop, take, ts, run_id, desk=desk["id"])
            except AlpacaError as e:
                if "вручную" in str(e) or "СТОП НЕ ВСТАЛ" in str(e):
                    raise
                self._nogo(ts, asset, "broker", str(e)[:160], tech, votes, score=0.9)
                return Result(asset.name, "skipped", f"брокер: {e}"[:200], cid)
            self.j.update_decision(did, executed=1)
            self.j.kv_set(f"partial_at:{asset.name}", None)
            self.j.kv_set(f"partial_done:{asset.name}", None)
            self.j.kv_set(f"init_stop:{asset.name}", stop)
            order = {**o, "order_id": o.get("order_id") or self.broker.last_order_id, "stop": stop, "take": take, "equity": acct["equity"], "half": mult < 1.0, "gaps": gaps, "tech": pattern, "mode": mode, "desk": desk["name"], "desk_id": desk["id"]}
            return Result(asset.name, "opened", f"[{desk['name']}] qty={v.qty} stop={stop:.6g} take={take:.6g}" + (" (половинный объём)" if gaps else ""), cid, order)
