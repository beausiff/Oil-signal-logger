"""IG demo mirror, with IG faked. No network."""

import json

import pytest

from src import broker


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeIG:
    """Just enough of IG's REST surface to drive broker.sync."""

    def __init__(self, positions=None, reject=False):
        self.positions = list(positions or [])
        self.reject = reject
        self.calls = []
        self.next_id = 1

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("/gateway/deal", 1)[1]
        self.calls.append((method, path, json, dict(headers or {})))
        if path == "/session" and method == "POST":
            return _Resp(200, {"currentAccountId": "ABC"}, {"CST": "c", "X-SECURITY-TOKEN": "x"})
        if path == "/positions":
            return _Resp(200, {"positions": [
                {"market": {"epic": p["epic"]},
                 "position": {"dealId": p["id"], "direction": p["dir"], "size": p["size"]}}
                for p in self.positions]})
        if path.startswith("/markets/"):
            return _Resp(200, {"snapshot": {"bid": 9000.0, "offer": 9004.0},
                               "instrument": {"currencies": [{"code": "GBP", "isDefault": True}]}})
        if path == "/positions/otc":
            if (headers or {}).get("_method") == "DELETE":
                self.positions = [p for p in self.positions if p["id"] != json["dealId"]]
            else:
                self.positions.append({"epic": json["epic"], "id": "D%d" % self.next_id,
                                       "dir": json["direction"], "size": json["size"]})
                self.next_id += 1
            return _Resp(200, {"dealReference": "REF"})
        if path.startswith("/confirms/"):
            if self.reject:
                return _Resp(200, {"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"})
            last = self.positions[-1]["id"] if self.positions else "D0"
            return _Resp(200, {"dealStatus": "ACCEPTED", "dealId": last, "level": 9002.0})
        return _Resp(404, {"errorCode": "unknown"})

    def orders(self):
        return [c for c in self.calls if c[1] == "/positions/otc"]


@pytest.fixture
def demo(monkeypatch):
    for key, value in {
        "BROKER_MODE": "demo", "IG_API_KEY": "k", "IG_USERNAME": "u",
        "IG_PASSWORD": "p", "IG_EPIC": "CC.D.LCO.UNC.IP", "IG_SIZE": "1",
    }.items():
        monkeypatch.setenv(key, value)


def run(fake, desired, allow_open):
    return broker.sync(desired, allow_open, ig=broker.IG(http=fake))


def test_off_by_default_makes_no_calls(monkeypatch):
    monkeypatch.delenv("BROKER_MODE", raising=False)
    fake = FakeIG()
    assert run(fake, "long", True) == ""
    assert fake.calls == []


def test_unknown_mode_such_as_live_is_off(monkeypatch, demo):
    monkeypatch.setenv("BROKER_MODE", "live")
    fake = FakeIG()
    assert run(fake, "long", True) == ""
    assert fake.calls == []


def test_entry_opens_a_buy_with_a_backstop(demo):
    fake = FakeIG()
    note = run(fake, "long", True)
    assert note.startswith("ig_demo=opened:long:D1")
    (_, _, body, _), = fake.orders()
    assert body["direction"] == "BUY"
    assert body["stopLevel"] == round(9000.0 * 0.95, 1)
    assert body["currencyCode"] == "GBP"


def test_rerun_never_doubles_up(demo):
    fake = FakeIG(positions=[{"epic": "CC.D.LCO.UNC.IP", "id": "D9", "dir": "BUY", "size": 1}])
    assert run(fake, "long", True) == "ig_demo=in_sync"
    assert fake.orders() == []


def test_flat_closes_what_is_open(demo):
    fake = FakeIG(positions=[{"epic": "CC.D.LCO.UNC.IP", "id": "D9", "dir": "BUY", "size": 1}])
    note = run(fake, "flat", False)
    assert note.startswith("ig_demo=closed:D9")
    (_, _, body, headers), = fake.orders()
    assert headers["_method"] == "DELETE"
    assert body["direction"] == "SELL"
    assert fake.positions == []


def test_missed_entry_is_not_chased(demo):
    fake = FakeIG()
    assert run(fake, "short", False) == "ig_demo=out_of_sync_no_position"
    assert fake.orders() == []


def test_other_markets_are_left_alone(demo):
    fake = FakeIG(positions=[{"epic": "IX.D.FTSE.DAILY.IP", "id": "F1", "dir": "BUY", "size": 1}])
    assert run(fake, "flat", False) == "ig_demo=in_sync"
    assert fake.orders() == []


def test_stake_cap_refuses_to_trade(monkeypatch, demo):
    monkeypatch.setenv("IG_SIZE", "50")
    fake = FakeIG()
    note = run(fake, "long", True)
    assert note.startswith("ig_demo_error=BrokerError")
    assert fake.orders() == []


def test_rejection_becomes_a_note_not_a_crash(demo):
    fake = FakeIG(reject=True)
    note = run(fake, "long", True)
    assert "MARKET_CLOSED" in note


def test_missing_credentials_is_a_note(monkeypatch, demo):
    monkeypatch.delenv("IG_PASSWORD")
    assert "missing IG_API_KEY" in run(FakeIG(), "long", True)
