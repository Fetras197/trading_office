# ruff: noqa: F811
"""v5.9: запреты совета на класс активов/отрасль исполняются на входе (случай 24.10: «Запрещено открытие новых длинных позиций в криптоактивах» — и куплены ETH, DOT, CRV)."""
import datetime as dt
import json


from office import bans, research, rules, views
from office.config import make_asset
from tests.test_hall import env, mk, FakeRouter  # noqa: F401  (фикстура env и сборка зала)

NOW = dt.datetime.now(dt.timezone.utc)
TODAY_CASE = ["Запрещено открытие новых длинных позиций в криптоактивах", "Запрещено увеличение плеча по текущим позициям", "Запрещено игнорировать сигналы ликвидации в альткоинах"]


def picture(board, forbidden=None, class_bans=None, hours_ago=0.0, regime="neutral"):
    d = {"regime": regime, "summary": "т", "forbidden": forbidden or [], "avoid_assets": []}
    if class_bans is not None:
        d["class_bans"] = class_bans
    ts = (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    board.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c','picture','*',?,'v',0.8,'[]','{}',?)", (ts, json.dumps(d)))


def test_text_parser_picks_class_entry_bans_only():
    P = lambda t: [(b["scope"], b.get("sector")) for b in bans.parse_text(t)]  # noqa: E731
    assert P(TODAY_CASE[0]) == [("crypto", None)]
    assert P(TODAY_CASE[1]) == [] and P(TODAY_CASE[2]) == []                       # плечо и «игнорировать сигналы» — не запрет входа
    assert P("Не открывать новые позиции в альткоинах") == [("altcoins", None)]
    assert P("Запрещено открывать новые позиции в секторе мем-коинов") == [("altcoins", None)]
    assert P("Избегать покупок в технологическом секторе до стабилизации индекса") == [("sector", "technology")]
    assert P("Не открывать новые позиции в секторе чипов") == [("sector", "semiconductors")]
    assert P("Не открывать новые позиции в секторе недвижимости и промышленности") == [("sector", "real_estate"), ("sector", "industrials")]
    assert P("Покупка активов из сектора XLK и XLRE") == [("sector", "technology"), ("sector", "real_estate")]
    assert P("Не открывать крупные позиции в спекулятивных альткоинах сегодня") == []              # размер, а не запрет входа
    assert P("Не открывать новые позиции до прояснения макро-фона") == [] and P("Не превышать лимиты риска по криптовалютам") == []
    assert P("Открытие новых позиций в акциях с негативными прогнозами аналитиков") == []         # условный запрет не расширяем на класс


def test_structured_bans_are_validated():
    raw = [{"scope": "crypto", "reason": "жёсткая распродажа рынка", "hours": 6}, {"scope": "sector", "sector": "technology", "reason": "отчёты техгигантов", "hours": 24},
           {"scope": "sector", "sector": "weapons", "reason": "неизвестная отрасль", "hours": 5}, {"scope": "crypto", "reason": "кор", "hours": 5},
           {"scope": "crypto", "reason": "срок слишком большой", "hours": 99}, {"scope": "moon", "reason": "нет такого класса", "hours": 2}, "мусор", {"scope": "crypto", "reason": "дубль класса", "hours": 3}]
    out = bans.normalize(raw)
    assert [(b["scope"], b["sector"]) for b in out] == [("crypto", ""), ("sector", "technology")] and all(b["src"] == "council" for b in out)


def test_todays_case_crypto_entries_are_blocked_even_with_all_analysts_buying(env):
    for sym in ("ETH/USD", "DOT/USD", "CRV/USD"):
        h, b, j, br = mk()                                        # аналитики все «за»: раньше это давало вход
        picture(b, TODAY_CASE)
        r = h.run_asset(sym)
        assert r.status == "hold" and not br.bought, sym
        assert "«криптоактивы»" in r.detail and "по Минску" in r.detail
        n = j.nogo_top("2000-01-01")[0]
        assert n["stage"] == "council" and views.STAGE_RU["council"] == "запрет совета на класс"
    nogo = views.nogo_lines(j.nogo_top("2000-01-01", 3), False)
    assert any("запрет совета на класс" in l for l in nogo)


def test_without_ban_the_same_entry_opens(env):
    h, b, j, br = mk()
    picture(b, ["Не игнорировать сигналы ликвидации"])
    assert h.run_asset("DOT/USD").status == "opened"


def test_altcoins_ban_spares_btc_eth(env):
    for sym, opened in (("ETH/USD", True), ("BTC/USD", True), ("DOT/USD", False)):
        h, b, j, br = mk()
        picture(b, class_bans=[{"scope": "altcoins", "sector": "", "reason": "распродажа альткоинов", "until": (NOW + dt.timedelta(hours=5)).isoformat(), "src": "council"}])
        assert (h.run_asset(sym).status == "opened") is opened, sym


def test_ban_survives_newer_pictures_until_its_term_and_expires(env):
    h, b, j, br = mk()
    picture(b, TODAY_CASE, hours_ago=8)                          # утренняя картина с запретом
    picture(b, ["Не игнорировать VIX"], hours_ago=0.1)           # более свежая картина запрет не повторила — он всё равно действует до срока
    assert h.run_asset("DOT/USD").status == "hold"
    h2, b2, j2, br2 = mk()
    picture(b2, TODAY_CASE, hours_ago=13)                        # текстовый запрет живёт 12 часов
    assert h2.run_asset("DOT/USD").status == "opened"
    h3, b3, j3, br3 = mk()
    picture(b3, class_bans=[{"scope": "crypto", "sector": "", "reason": "срок вышел", "until": (NOW - dt.timedelta(minutes=1)).isoformat(), "src": "council"}])
    assert h3.run_asset("DOT/USD").status == "opened"


def test_open_positions_are_not_touched(env):
    h, b, j, br = mk()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES('ETH/USD',1,2300,2200,2500,0,1,'t','majors')")
    picture(b, TODAY_CASE)
    before = j.positions()
    h.run_asset("ETH/USD")
    assert j.positions() == before and not br.bought


def test_sector_ban_blocks_etf_and_matching_stocks_only(env, monkeypatch):
    sectors = {"XLK": {"technology"}, "NVDA": {"technology", "semiconductors"}, "JPM": {"financial"}}
    monkeypatch.setattr(bans, "sectors_of", lambda sym: sectors.get(sym, bans.ETF_SECTOR.get(sym) and {bans.ETF_SECTOR[sym]} or set()))
    b0 = None
    cb = [{"scope": "sector", "sector": "technology", "reason": "технологии перегреты", "until": (NOW + dt.timedelta(hours=5)).isoformat(), "src": "council"}]
    from office.board import Board
    b0 = Board()
    picture(b0, class_bans=cb)
    assert bans.blocking(b0, make_asset("NVDA"))["sector"] == "technology"
    assert bans.blocking(b0, make_asset("XLK")) and not bans.blocking(b0, make_asset("JPM")) and not bans.blocking(b0, make_asset("BTC/USD"))
    picture(Board(), class_bans=[])                              # пустой список структурированных запретов — запретов нет
    b1 = Board()
    picture(b1, class_bans=[{"scope": "stocks", "sector": "", "reason": "акции перекуплены", "until": (NOW + dt.timedelta(hours=2)).isoformat(), "src": "council"}])
    assert bans.blocking(b1, make_asset("JPM")) and not bans.blocking(b1, make_asset("ETH/USD"))


def test_picture_validation_merges_structured_and_text_bans():
    raw = {"regime": "neutral", "summary": "т", "confidence": 0.6, "forbidden": TODAY_CASE + ["Не открывать новые позиции в технологическом секторе"],
           "class_bans": [{"scope": "altcoins", "reason": "распродажа альткоинов", "hours": 6}]}
    pic = research.v_picture(raw)
    kinds = {(b["scope"], b["sector"]): b["src"] for b in pic["class_bans"]}
    assert kinds == {("altcoins", ""): "council", ("crypto", ""): "text", ("sector", "technology"): "text"}
    text = views.picture_text({"ts": "2026-10-24T10:00:00+00:00", "data": {**pic, "regime": "neutral"}}, None)
    assert "не открывать новые позиции: «альткоины»" in text


def test_prompt_asks_for_structured_bans():
    assert "class_bans" in research.PICTURE_SYS and "code enforces class_bans" in research.PICTURE_SYS


def test_not_a_change_of_entry_rules():
    assert "bans" not in json.dumps(rules.snapshot(), default=str)
    assert rules.fingerprint() == rules.fingerprint()
