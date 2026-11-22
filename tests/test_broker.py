"""OANDA practice mirror, with OANDA faked. No network."""

import json

import pytest

from src import broker

ACCOUNT = "101-004-1-001"


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeOanda:
    """Just enough of OANDA's v20 REST surface to drive broker.sync."""

    def __init__(self, trades=None, reject=False, tradeable=True):
        self.trades = list(trades or [])
        self.reject = reject
        self.tradeable = tradeable
        self.calls = []
        self.next_id = 1

    def request(self, method, url, json=None, headers=None, timeout=None):
        assert url.startswith(broker.PRACTICE_URL + "/accounts/" + ACCOUNT)
        path = url.split(ACCOUNT, 1)[1]
        self.calls.append((method, path, json, dict(headers or {})))
        if path == "/summary":
            return _Resp(200, {"account": {"id": ACCOUNT}})
        if path == "/openTrades":
            return _Resp(200, {"trades": [
                {"id": t["id"], "instrument": t["instrument"], "currentUnits": t["units"]}
                for t in self.trades]})
        if path.startswith("/pricing"):
            return _Resp(200, {"prices": [{"tradeable": self.tradeable,
                                           "bids": [{"price": "75.000"}], "asks": [{"price": "75.040"}]}]})
        if path.startswith("/instruments"):
            return _Resp(200, {"instruments": [{"name": "BCO_USD", "displayPrecision": 3}]})
        if path == "/orders":
            if self.reject:
                return _Resp(201, {"orderCancelTransaction": {"reason": "MARKET_HALTED"}})
            order = json["order"]
            tid = str(self.next_id)
            self.next_id += 1
            self.trades.append({"id": tid, "instrument": order["instrument"], "units": order["units"]})
            return _Resp(201, {"orderFillTransaction": {"price": "75.040", "tradeOpened": {"tradeID": tid}}})
        if path.startswith("/trades/") and path.endswith("/close"):
            tid = path.split("/")[2]
            self.trades = [t for t in self.trades if t["id"] != tid]
            return _Resp(200, {"orderFillTransaction": {"price": "74.900"}})
        return _Resp(404, {"errorMessage": "unknown"})

    def orders(self):
        return [c for c in self.calls if c[1] == "/orders" or c[1].endswith("/close")]


@pytest.fixture
def demo(monkeypatch):
    for key, value in {
        "BROKER_MODE": "demo", "OANDA_API_TOKEN": "t", "OANDA_ACCOUNT_ID": ACCOUNT,
        "OANDA_UNITS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OANDA_INSTRUMENT", raising=False)


def run(fake, desired, allow_open):
    return broker.sync(desired, allow_open, client=broker.Oanda(http=fake))


def test_off_by_default_makes_no_calls(monkeypatch):
    monkeypatch.delenv("BROKER_MODE", raising=False)
    fake = FakeOanda()
    assert run(fake, "long", True) == ""
    assert fake.calls == []


def test_unknown_mode_such_as_live_is_off(monkeypatch, demo):
    monkeypatch.setenv("BROKER_MODE", "live")
    fake = FakeOanda()
    assert run(fake, "long", True) == ""
    assert fake.calls == []


def test_only_the_practice_server_is_used():
    assert broker.PRACTICE_URL == "https://api-fxpractice.oanda.com/v3"
    assert broker.Oanda(http=FakeOanda()).base == broker.PRACTICE_URL


def test_token_is_sent_as_bearer(demo):
    fake = FakeOanda()
    run(fake, "flat", False)
    assert fake.calls[0][3]["Authorization"] == "Bearer t"


def test_entry_opens_a_buy_with_a_backstop(demo):
    fake = FakeOanda()
    note = run(fake, "long", True)
    assert note == "oanda_demo=opened:long:1@75.040"
    (_, _, body, _), = fake.orders()
    order = body["order"]
    assert order["instrument"] == "BCO_USD"
    assert order["units"] == "1"
    assert order["type"] == "MARKET"
    assert order["stopLossOnFill"]["price"] == "%.3f" % (75.0 * 0.95)


def test_short_entry_sends_negative_units_and_stop_above(demo):
    fake = FakeOanda()
    assert run(fake, "short", True).startswith("oanda_demo=opened:short:1")
    (_, _, body, _), = fake.orders()
    assert body["order"]["units"] == "-1"
    assert body["order"]["stopLossOnFill"]["price"] == "%.3f" % (75.04 * 1.05)


def test_rerun_never_doubles_up(demo):
    fake = FakeOanda(trades=[{"id": "9", "instrument": "BCO_USD", "units": "1"}])
    assert run(fake, "long", True) == "oanda_demo=in_sync"
    assert fake.orders() == []


def test_flat_closes_what_is_open(demo):
    fake = FakeOanda(trades=[{"id": "9", "instrument": "BCO_USD", "units": "1"}])
    note = run(fake, "flat", False)
    assert note == "oanda_demo=closed:9@74.900"
    (method, path, body, _), = fake.orders()
    assert (method, path, body) == ("PUT", "/trades/9/close", {"units": "ALL"})
    assert fake.trades == []


def test_flip_closes_then_opens(demo):
    fake = FakeOanda(trades=[{"id": "9", "instrument": "BCO_USD", "units": "-1"}])
    note = run(fake, "long", True)
    assert note.startswith("oanda_demo=closed:9@74.900,opened:long:1")
    assert [t["units"] for t in fake.trades] == ["1"]


def test_missed_entry_is_not_chased(demo):
    fake = FakeOanda()
    assert run(fake, "short", False) == "oanda_demo=out_of_sync_no_position"
    assert fake.orders() == []


def test_other_markets_are_left_alone(demo):
    fake = FakeOanda(trades=[{"id": "F1", "instrument": "UK100_GBP", "units": "1"}])
    assert run(fake, "flat", False) == "oanda_demo=in_sync"
    assert fake.orders() == []


def test_instrument_can_be_changed(monkeypatch, demo):
    monkeypatch.setenv("OANDA_INSTRUMENT", "WTICO_USD")
    fake = FakeOanda()
    run(fake, "long", True)
    (_, _, body, _), = fake.orders()
    assert body["order"]["instrument"] == "WTICO_USD"


def test_unit_cap_refuses_to_trade(monkeypatch, demo):
    monkeypatch.setenv("OANDA_UNITS", "5000")
    fake = FakeOanda()
    note = run(fake, "long", True)
    assert note.startswith("oanda_demo_error=BrokerError")
    assert fake.orders() == []


def test_rejection_becomes_a_note_not_a_crash(demo):
    fake = FakeOanda(reject=True)
    note = run(fake, "long", True)
    assert "MARKET_HALTED" in note


def test_closed_market_is_a_note(demo):
    fake = FakeOanda(tradeable=False)
    assert "not tradeable" in run(fake, "long", True)
    assert fake.orders() == []


def test_missing_credentials_is_a_note(monkeypatch, demo):
    monkeypatch.delenv("OANDA_API_TOKEN")
    fake = FakeOanda()
    assert "missing OANDA_API_TOKEN" in run(fake, "long", True)
    assert fake.calls == []
