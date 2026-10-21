from office.hall import Result


class C:
    def account_view(self): return {"equity": 100_000.0, "last_equity": 100_000.0, "cash": 100_000.0, "positions": []}
    def price(self, s): return 1.0
    def clock(self): return {"is_open": False}
    def day_change(self, s): return 0.0


class FakeHall:
    fresh_left = 3

    def __init__(self): self.ran = []

    def run_asset(self, s):
        self.ran.append(s)
        return Result(s, "hold", "нет технического сигнала", "c")
