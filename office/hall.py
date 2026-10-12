"""Торговый зал: цепочка ролей по одному активу с публикацией каждого отчёта на шину board.db.
Аналитики (Gemini) -> bull/bear x2 раунда (промпты TradingAgents, LLM=claude -p) -> trader -> risk (+Riskfolio CVaR)
-> PM (+PyPortfolioOpt) -> RiskGate (код) -> executor -> Alpaca paper. Любой невалидный ответ роли после аналитиков = пропуск актива."""
import concurrent.futures as cf
import datetime as dt
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher

from . import agents, data, events, portfolio
from .board import PICTURE_MAX_AGE_H, RejectedReport
from . import config
from .config import COSTS, Limits, make_asset
from .llm import LLMError, LLMUnavailable
from .risk import RiskGate

INVESTORS = ("buffett", "munger", "druckenmiller")
ROUNDS = 2


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
    """Актив пропускается в этом цикле (невалидный ответ роли, лимиты LLM, нет данных)."""


class DebateLLM:
    """Утка под TradingAgents: node вызывает llm.invoke(prompt).content. Добавляет правило цитирования и парсит REFS_JSON."""
    def __init__(self, claude):
        self.claude, self.allowed, self.must, self.last, self.feedback, self.must_ids = claude, [], "", None, None, []

    def invoke(self, prompt):
        extra = f"\nYou MUST cite these report ids explicitly: {self.must_ids}." if self.must_ids else ""
        extra += f"\nYour previous reply was rejected: {self.feedback}. Fix it." if self.feedback else ""
        text = self.claude.text(None, prompt + agents.REFS_TEXT % (self.allowed, self.must) + extra, "")
        m = re.search(r"REFS_JSON:\s*(\{.*\})\s*$", text.strip(), re.S)
        if not m:
            raise LLMError("дебаты: нет REFS_JSON")
        try:
            j = json.loads(m.group(1))
            agree, dispute = [int(x) for x in j.get("agree", [])], [int(x) for x in j.get("dispute", [])]
            self.last = {"refs": agree + dispute, "stances": {**{i: "agree" for i in agree}, **{i: "dispute" for i in dispute}},
                         "confidence": float(j["confidence"]), "stance": str(j["stance"]).lower()}
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"дебаты: битый REFS_JSON ({e})") from e
        if self.last["stance"] not in ("buy", "sell", "hold"):
            raise LLMError("дебаты: stance вне buy/sell/hold")
        missing = [i for i in self.must_ids if i not in self.last["refs"]]
        if missing:
            raise LLMError(f"не процитированы обязательные отчёты {missing}")
        return SimpleNamespace(content=text[:m.start()].strip())


def _refs(raw: dict, role: str) -> tuple[list[int], dict]:
    try:
        refs = [int(x) for x in raw["refs"]]
        stances = {int(k): str(v).lower() for k, v in dict(raw["stances"]).items()}
    except (KeyError, TypeError, ValueError) as e:
        raise LLMError(f"{role}: нет/битые refs/stances ({e})") from e
    return refs, stances


class Hall:
    def __init__(self, board, journal, broker, router, claude, lab=None, notify=None, limits: Limits = Limits(), auto_trade=True, picture=None):
        self.board, self.j, self.broker, self.router, self.claude = board, journal, broker, router, claude
        self.picture = picture or (lambda: board.latest('picture', max_age_h=PICTURE_MAX_AGE_H))  # актуальная «Картина дня» (обязательна для ссылок)
        self.lab, self.notify, self.gate, self.auto_trade = lab, notify or (lambda t: None), RiskGate(limits), auto_trade
        self.exec_lock = threading.Lock()

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

    # ---------- публикация с повтором ----------
    def _chain_call(self, cid, role, asset, call, must_cite):
        """call() -> (verdict, confidence, refs, stances, data, raw). 2 попытки: ошибка LLM или отклонение шиной -> повтор -> Skip."""
        last = None
        for _ in range(2):
            try:
                verdict, conf, refs, stances, d, raw = call(str(last) if last else None)
                rid = self.board.publish(cid, role, asset, verdict, conf, refs, stances, d, must_cite)
                return rid, raw
            except LLMUnavailable as e:
                raise Skip(f"{role}: лимит LLM ({e})") from e
            except (LLMError, RejectedReport) as e:
                last = e
        raise Skip(f"{role}: невалидный ответ -> пропуск ({last})")

    def _ask_json(self, role, payload, must, fb=None):
        if fb:
            payload = {**payload, "previous_rejection": fb}
        return self.claude.ask(role, payload, system=agents.system_prompt(role, must))

    # ---------- аналитики ----------
    def _analysts(self, cid, asset, b):
        payloads = {"market": {"market": {k: b[k] for k in ("indicators_1h", "indicators_4h", "last_candles_1h", "tradingview")}},
                    "news": {"asset": asset.name, "news": b["news"]},
                    "fundamentals": {"asset": asset.name, "kind": asset.kind, "fundamentals": b["fundamentals"]}}
        try:
            payloads["quant"] = {"asset": asset.name, "quant": agents.quant_stats(asset)}
        except Exception as e:  # noqa: BLE001
            payloads["quant"] = {"error": f"unavailable: {type(e).__name__}"}
        if asset.kind == "stock":
            try:
                inv = agents.investor_data(asset)
                for r in INVESTORS:
                    payloads[r] = {"asset": asset.name, **inv}
            except Exception:  # noqa: BLE001 — нет фундаментала: инвесторы не голосуют
                pass
        if asset.kind == "crypto":
            payloads["fundamentals"]["derivatives"] = b.get("derivatives")

        def one(role):
            try:
                n = agents.run_analyst(role, self.router, payloads[role])
            except (LLMError, KeyError) as e:  # лимит/невалидный ответ аналитика = пропуск его голоса
                n = {"stance": "abstain", "confidence": 0.0, "rationale": f"{type(e).__name__}: {str(e)[:150]}"}
            return role, n
        with cf.ThreadPoolExecutor(3) as ex:
            res = list(ex.map(one, list(payloads)))
        out = []
        for role, n in res:
            rid = self.board.publish(cid, role, asset.name, f"{n['stance']}: {n['rationale']}", n["confidence"],
                                     data={**n, "price": b["indicators_1h"]["close"]})
            out.append({"id": rid, "role": role, **n})
        if self.lab:
            sig = self.lab(asset.name)
            if sig:
                rid = self.board.publish(cid, "lab", asset.name, f"{sig['stance']}: {sig['rationale']}", sig["confidence"],
                                         data={**sig, "price": b["indicators_1h"]["close"]})
                out.append({"id": rid, "role": "lab", **sig})
        return out

    def _research_ctx(self, asset) -> dict:
        """Свежие выводы исследовательского отдела по активу: новости, деривативы, события ближайших 6 часов."""
        out = {}
        nd = self.board.latest("newsdesk", max_age_h=3)
        if nd and asset.name in nd["data"].get("assets", {}):
            out["news_desk"] = nd["data"]["assets"][asset.name]
        cd = self.board.latest("cryptodesk", max_age_h=3)
        if cd and asset.kind == "crypto":
            out["crypto_desk"] = {"market": cd["data"].get("market"), "asset": cd["data"].get("assets", {}).get(asset.hl_coin)}
        up = events.upcoming(self.board, 6)
        if up:
            out["events_next_6h"] = [{"when_utc": e["ts"], "what": events.title_ru(e), "impact": e["impact"]} for e in up][:5]
        return out

    @staticmethod
    def _pic_line(pic):
        d = pic["data"]
        return (f"[#{pic['id']} picture] regime={d.get('regime')} size_mult={d.get('size_mult')} (conf {pic['confidence']:.2f}): {d.get('summary', '')} "
                f"Risks: {'; '.join(d.get('main_risks', []))}. Forbidden today: {'; '.join(d.get('forbidden', []))}")

    @staticmethod
    def _tagged(reports):
        return "\n".join(f"[#{r['id']} {r['role']}] {r['stance']} (conf {r['confidence']:.2f}): {r['rationale']}" for r in reports)

    # ---------- дебаты ----------
    def _debate(self, cid, asset, analysts, b, pic):
        live = [r for r in analysts if r["stance"] != "abstain"]
        by = lambda *roles: self._tagged([r for r in live if r["role"] in roles]) or "(no report)"  # noqa: E731
        state = {"company_of_interest": asset.alpaca, "asset_type": asset.kind,
                 "market_report": by("market", "quant", "lab") + "\n" + self._pic_line(pic), "sentiment_report": by(*INVESTORS) if asset.kind == "stock" else by("quant"),
                 "news_report": by("news"), "fundamentals_report": by("fundamentals"),
                 "investment_debate_state": {"history": "", "bull_history": "", "bear_history": "", "current_response": "", "count": 0}}
        duck = DebateLLM(self.claude)
        nodes = {"bull": create_bull_researcher(duck), "bear": create_bear_researcher(duck)}
        ids = [r["id"] for r in live]
        last = {}  # role -> id последнего отчёта
        texts = {}
        for rnd in range(1, ROUNDS + 1):
            for role in ("bull", "bear"):
                other = "bear" if role == "bull" else "bull"
                must = ["analyst", "picture"] + ([other] if other in last else [])
                allowed = ids + [pic["id"]] + ([last[other]] if other in last else []) + ([last[role]] if role in last else [])

                def call(fb=None, role=role, must=must, allowed=allowed, other=other):
                    duck.allowed, duck.must, duck.last, duck.feedback = allowed, ", ".join(must), None, fb
                    duck.must_ids = [pic["id"]] + ([last[other]] if other in last else [])
                    st = {**state, "investment_debate_state": dict(state["investment_debate_state"])}
                    new = nodes[role](st)["investment_debate_state"]
                    text = new["current_response"].split(":", 1)[1].strip()
                    j = duck.last
                    return (f"{j['stance']}: {text[:600]}", j["confidence"], j["refs"], j["stances"],
                            {"round": rnd, "stance": j["stance"], "text": text, "price": b["indicators_1h"]["close"]}, (new, text))
                rid, (new, text) = self._chain_call(cid, role, asset.name, call, tuple(must))
                state["investment_debate_state"] = new
                last[role], texts[role] = rid, text
                new["history"] += f" [report #{rid}]"  # оппонент видит id отчёта, на который можно сослаться
        return last, texts

    # ---------- основной цикл ----------
    def run_asset(self, symbol: str) -> Result:
        asset, cid, ts = make_asset(symbol), uuid.uuid4().hex[:12], utcnow()
        try:
            return self._run(asset, cid, ts)
        except Skip as e:
            return Result(asset.name, "skipped", str(e), cid)
        except (LLMError, RejectedReport) as e:  # любая ошибка/невалидный ответ -> пропуск актива
            return Result(asset.name, "skipped", f"{type(e).__name__}: {e}", cid)
        except Exception as e:  # noqa: BLE001 — сбой одного актива не валит цикл остальных
            return Result(asset.name, "error", f"{type(e).__name__}: {e}"[:200], cid)

    def _run(self, asset, cid, ts) -> Result:
        try:
            df = data.candles(asset, "1h", **({"limit": 500} if asset.kind == "crypto" else {"rng": "60d"}))
            if len(df) < 210:
                raise Skip("мало свечей")
            b = data.bundle(asset, df)
        except Skip:
            raise
        except Exception as e:  # noqa: BLE001
            raise Skip(f"нет данных: {type(e).__name__}") from e
        price, atr = b["indicators_1h"]["close"], b["indicators_1h"]["atr14"]
        run_id = self.j.add_run(ts, asset.name, price, atr, cid)
        acct = self._account(asset, ts)

        if not acct["position"]:  # лимиты исчерпаны -> новый вход невозможен: не тратим LLM-бюджет
            l = self.gate.l
            why = ("дневной стоп" if self.gate.trading_halted(acct["equity"], acct["day_start_equity"]) else
                   "лимит входов/сутки" if acct["entries_today"] >= l.max_trades_day else
                   "лимит позиций" if acct["open_positions"] >= l.max_positions else "")
            if why:
                self.j.set_summary(run_id, f"skip: {why}")
                return Result(asset.name, "hold", f"{why}: новый вход невозможен", cid)
        pic = self.picture()
        if not pic:
            raise Skip("нет актуальной «Картины дня» — без неё зал не работает")
        pd_ = pic["data"]
        if not acct["position"]:  # запреты исследовательского отдела: до LLM-вызовов (экономия бюджета)
            why = (f"событие: {events.blocked(self.board, asset.name, asset.kind)}" if events.blocked(self.board, asset.name, asset.kind) else
                   "картина дня: новые входы запрещены" if pd_.get("no_new_entries") else
                   "картина дня: актив в списке запретов" if asset.name in (pd_.get("avoid_assets") or []) else "")
            if why:
                self.j.set_summary(run_id, f"skip: {why}")
                return Result(asset.name, "hold", why, cid)
        analysts = self._analysts(cid, asset, b)
        live = [r for r in analysts if r["stance"] != "abstain"]
        if len(live) < 2:
            raise Skip(f"недостаточно аналитиков ({len(live)} из {len(analysts)}; остальные abstain)")
        if not acct["position"] and not any(r["stance"] in ("buy", "sell") and r["confidence"] >= 0.5 for r in live):
            self.j.set_summary(run_id, "short-circuit: нет directional мнения >=0.5")
            return Result(asset.name, "hold", "short-circuit: нет directional мнения >=0.5", cid)

        last, texts = self._debate(cid, asset, analysts, b, pic)
        reports = [{"id": r["id"], "role": r["role"], "stance": r["stance"], "confidence": r["confidence"], "rationale": r["rationale"]} for r in live]
        reports += [{"id": last[k], "role": k, "stance": "-", "confidence": 0.0, "rationale": texts[k][:500]} for k in ("bull", "bear")]
        reports.append({"id": pic["id"], "role": "picture", "stance": pd_.get("regime", "neutral"), "confidence": pic["confidence"],
                        "rationale": f"{pd_.get('summary', '')} Risks: {'; '.join(pd_.get('main_risks', []))}. Forbidden today: {'; '.join(pd_.get('forbidden', []))}"})
        tp = {"asset": asset.name, "price": price, "atr14": atr, "account": acct, "position": acct["position"], "reports": reports,
              "day_picture": {"regime": pd_.get("regime"), "size_mult": pd_.get("size_mult"), "priority_assets": pd_.get("priority_assets"), "expectation": pd_.get("expectation")},
              "research": self._research_ctx(asset)}

        def trader(fb=None):
            raw = self._ask_json("trader", tp, "bull, bear and picture (the Picture of the Day)", fb)
            a = str(raw.get("action", "")).lower()
            if a not in ("buy", "sell", "hold"):
                raise LLMError(f"trader: action {a!r}")
            refs, st = _refs(raw, "trader")
            return (f"{a}: {str(raw.get('rationale', ''))[:400]}", float(raw["confidence"]), refs, st, {**raw, "action": a}, raw)
        tid, t = self._chain_call(cid, "trader", asset.name, trader, ("bull", "bear", "picture"))
        action = str(t["action"]).lower()
        if action == "hold" or (action == "sell" and not acct["position"]):
            self.j.set_summary(run_id, f"trader: {action}")
            return Result(asset.name, "hold", "trader: hold", cid)
        if action == "sell":
            return self._close(asset, cid, run_id, ts, price, tid, acct)

        entry = float(t.get("entry") or price)
        try:
            stop, take = float(t["stop"]), float(t["take_profit"])
        except (KeyError, TypeError, ValueError) as e:
            raise Skip(f"trader: нет stop/take_profit ({e})") from e
        if not (0 < stop < entry < take):
            raise Skip(f"trader: нелогичные уровни stop={stop} entry={entry} take={take}")
        # --- risk: Riskfolio CVaR + wshobson risk-manager ---
        syms = [asset.name] + [s for s in self.j.positions() if s != asset.name]
        px = portfolio.daily_closes(syms)
        rets = px.pct_change().dropna()
        w_new = {asset.name: self.gate.l.max_notional}
        w_held = {s: self.gate.l.max_notional for s in syms}
        cv = {"asset_cvar95_daily": round(portfolio.cvar95(rets[[asset.name]], {asset.name: 1.0}), 4) if asset.name in rets else None,
              "portfolio_cvar95_with_candidate": round(portfolio.cvar95(rets, w_held), 4) if len(rets) > 30 else None,
              "held": list(self.j.positions()), "candidate_notional_cap": w_new[asset.name]}
        rp = {**tp, "proposal": t, "riskfolio": cv, "reports": reports + [{"id": tid, "role": "trader", "stance": action, "confidence": float(t["confidence"]), "rationale": str(t.get("rationale", ""))[:400]}]}

        def risk(fb=None):
            raw = self._ask_json("risk_manager", rp, "trader", fb)
            v = str(raw.get("verdict", "")).lower()
            if v not in ("approve", "reduce", "reject"):
                raise LLMError(f"risk_manager: verdict {v!r}")
            refs, st = _refs(raw, "risk_manager")
            return (f"{v}: {str(raw.get('rationale', ''))[:400]}", 0.5 if v != "reject" else 0.8, refs, st, {**raw, "riskfolio": cv}, raw)
        rid, rm = self._chain_call(cid, "risk_manager", asset.name, risk, ("trader",))
        # --- PM: PyPortfolioOpt ---
        alloc = portfolio.allocate(px, self.gate.l.max_notional) if len(px.columns) and len(rets) > 30 else {}
        w_pm = min(self.gate.l.max_notional, max(0.05, alloc.get(asset.name, 0.05)))
        pp = {**rp, "risk_manager": rm, "pyportfolioopt": {"weights": {k: round(v, 3) for k, v in alloc.items()}, "weight_cap_for_candidate": round(w_pm, 3)},
              "reports": rp["reports"] + [{"id": rid, "role": "risk_manager", "stance": str(rm["verdict"]), "confidence": 0.5, "rationale": str(rm.get("rationale", ""))[:300]}]}

        def pm(fb=None):
            raw = self._ask_json("portfolio_manager", pp, "trader, risk_manager", fb)
            d = str(raw.get("decision", "")).lower()
            if d not in ("approve", "reject"):
                raise LLMError(f"portfolio_manager: decision {d!r}")
            refs, st = _refs(raw, "portfolio_manager")
            return (f"{d}: {str(raw.get('rationale', ''))[:400]}", 0.6, refs, st, {**raw, "pyportfolioopt": pp["pyportfolioopt"]}, raw)
        pid, pmr = self._chain_call(cid, "portfolio_manager", asset.name, pm, ("trader", "risk_manager"))
        if str(rm["verdict"]).lower() == "reject" or str(pmr["decision"]).lower() == "reject":
            why = "risk_manager: reject" if str(rm["verdict"]).lower() == "reject" else "portfolio_manager: reject"
            self.j.add_decision(run_id, ts, asset.name, "buy", entry, stop, take, None, "rejected", why)
            return Result(asset.name, "rejected", why, cid)
        mult = min(1.0, max(0.0, float(pmr.get("size_mult", 1.0) or 0)), float(pd_.get("size_mult", 1.0)))  # картина дня может только уменьшить
        if str(rm["verdict"]).lower() == "reduce":
            mult = min(mult, min(1.0, max(0.0, float(rm.get("size_mult", 1.0) or 0))))
        # --- RiskGate (код) + исполнение: под локом и со свежим состоянием счёта (лимиты не гонятся между параллельными активами) ---
        with self.exec_lock:
            if config.STOP_FILE.exists():
                return Result(asset.name, "hold", "STOP-файл: новые решения не принимаются", cid)
            acct = self._account(asset, utcnow())
            v = self.gate.check_entry(kind=asset.kind, costs=COSTS[asset.kind], entry=entry, stop=stop, atr=atr, equity=acct["equity"],
                                      day_start_equity=acct["day_start_equity"], entries_today=acct["entries_today"],
                                      open_positions=acct["open_positions"], cash=acct["cash"], has_position=bool(acct["position"]), size_mult=mult)
            qty = v.qty
            if v.ok:
                cap = w_pm * acct["equity"] / entry  # PM-распределение PyPortfolioOpt только уменьшает размер
                qty = int(min(qty, cap)) if asset.kind == "stock" else int(min(qty, cap) * 1e6) / 1e6
                if qty * entry < self.gate.l.min_notional_usd or qty <= 0:
                    v.ok, v.reason = False, "размер после PM-распределения ниже минимума"
            did = self.j.add_decision(run_id, ts, asset.name, "buy", entry, stop, take, qty, "ok" if v.ok else "rejected", v.reason)
            if not v.ok:
                return Result(asset.name, "rejected", v.reason, cid)
            ticket = {"asset": asset.name, "side": "buy", "qty": qty, "entry": price, "stop": stop, "take": take, "notional": round(qty * price, 2),
                      "cash": acct["cash"], "equity": acct["equity"], "as_of": b["as_of"], "reports": [{"id": pid, "role": "portfolio_manager", "stance": "approve", "confidence": 0.6, "rationale": ""}]}

            def execu(fb=None):
                raw = self._ask_json("executor", ticket, "portfolio_manager", fb)
                if "ticket_ok" not in raw:
                    raise LLMError("executor: нет ticket_ok")
                refs, st = _refs(raw, "executor")
                return (f"{'ok' if raw['ticket_ok'] else 'issues'}: {str(raw.get('rationale', ''))[:300]}", 0.5, refs, st, raw, raw)
            eid, ex = self._chain_call(cid, "executor", asset.name, execu, ("portfolio_manager",))
            if ex.get("ticket_ok") is False:
                self.j.update_decision(did, verdict="rejected", reason=f"executor: {ex.get('issues')}")
                return Result(asset.name, "rejected", f"executor: {ex.get('issues')}", cid)
            if not self.auto_trade:
                return Result(asset.name, "hold", "dry-run: ордер не отправлен", cid)
            o = self.broker.buy(asset.name, qty, price, stop, take, ts, run_id)
            self.j.update_decision(did, executed=1)
            order = {**o, "order_id": self.broker.last_order_id, "stop": stop, "take": take, "equity": acct["equity"]}
            return Result(asset.name, "opened", f"qty={qty} stop={stop} take={take}", cid, order)

    def _close(self, asset, cid, run_id, ts, price, tid, acct):
        did = self.j.add_decision(run_id, ts, asset.name, "sell", price, None, None, acct["position"]["qty"], "ok", "trader: закрыть", approved=1)
        if not self.auto_trade:
            return Result(asset.name, "hold", "dry-run: закрытие не отправлено", cid)
        r = self.broker.sell(asset.name, price, ts, "signal")
        self.j.update_decision(did, executed=1)
        pos = acct["position"]
        return Result(asset.name, "closed", f"pnl={r['pnl']:.2f}", cid,
                      {"order_id": self.broker.last_order_id, **r, "pnl_pct": r["pnl"] / (pos["avg"] * pos["qty"]) if pos["avg"] * pos["qty"] else 0.0})
