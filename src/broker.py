"""Mirror the paper position onto an OANDA practice (demo) account.

Off unless BROKER_MODE=demo. There is no live mode: the practice server URL is
fixed below, and a live token is refused by that server. Pointing this at real
money needs a code change, on purpose.

The broker is the source of truth for what is actually open, so every call
starts by reading OANDA's open trades on the instrument. That makes a rerun
safe: it never opens a second position, and a close that failed last hour is
retried this hour.

Nothing in here may fail the hourly run. Every error becomes a note.

Environment:
    BROKER_MODE       off (default) | demo
    OANDA_API_TOKEN   personal access token from the OANDA hub (practice)
    OANDA_ACCOUNT_ID  e.g. 101-004-12345678-001, shown on the hub dashboard
    OANDA_INSTRUMENT  the market, default BCO_USD (Brent). See oanda_check.
    OANDA_UNITS       units per trade (barrels for BCO_USD), capped at
                      config.BROKER_MAX_UNITS
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import requests

from . import config

PRACTICE_URL = "https://api-fxpractice.oanda.com/v3"
DEFAULT_INSTRUMENT = "BCO_USD"


class BrokerError(Exception):
    pass


def mode() -> str:
    value = (os.environ.get("BROKER_MODE") or "off").strip().lower()
    return value if value in ("off", "demo") else "off"


@dataclass
class OpenPosition:
    deal_id: str  # OANDA trade id
    side: str  # long | short
    size: float  # units, always positive


class Oanda:
    """The handful of OANDA v20 REST calls this needs, nothing more."""

    def __init__(self, http=None, base_url: str = PRACTICE_URL):
        self.http = http or requests.Session()
        self.base = base_url
        self.token = (os.environ.get("OANDA_API_TOKEN") or "").strip()
        self.account = (os.environ.get("OANDA_ACCOUNT_ID") or "").strip()

    def _call(self, method: str, path: str, body=None) -> dict:
        resp = self.http.request(
            method, "%s/accounts/%s%s" % (self.base, self.account, path), json=body,
            headers={
                "Authorization": "Bearer %s" % self.token,
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            },
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            raise BrokerError("%s %s -> %s %s" % (method, path, resp.status_code, resp.text[:200]))
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            return {}

    def login(self) -> None:
        """No session to open; check the settings and that the account answers."""
        if not (self.token and self.account):
            raise BrokerError("missing OANDA_API_TOKEN or OANDA_ACCOUNT_ID")
        self._call("GET", "/summary")

    def positions(self, instrument: str) -> List[OpenPosition]:
        data = self._call("GET", "/openTrades")
        out = []
        for trade in data.get("trades", []):
            if trade.get("instrument") != instrument:
                continue
            units = float(trade.get("currentUnits") or 0)
            if units == 0:
                continue
            out.append(OpenPosition(
                deal_id=str(trade.get("id", "")),
                side="long" if units > 0 else "short",
                size=abs(units),
            ))
        return out

    def instruments(self, names: Optional[str] = None) -> list:
        path = "/instruments" + ("?instruments=%s" % names if names else "")
        return self._call("GET", path).get("instruments", [])

    def quote(self, instrument: str) -> dict:
        prices = self._call("GET", "/pricing?instruments=%s" % instrument).get("prices", [])
        if not prices:
            raise BrokerError("no OANDA quote for %s" % instrument)
        price = prices[0]
        if price.get("tradeable") is False:
            raise BrokerError("%s not tradeable now" % instrument)
        try:
            return {"bid": float(price["bids"][0]["price"]), "ask": float(price["asks"][0]["price"])}
        except (KeyError, IndexError, ValueError, TypeError):
            raise BrokerError("no OANDA quote for %s" % instrument)

    @staticmethod
    def _filled(data: dict) -> dict:
        fill = data.get("orderFillTransaction")
        if not fill:
            cancel = data.get("orderCancelTransaction") or data.get("orderRejectTransaction") or {}
            raise BrokerError("rejected: %s" % (cancel.get("reason") or cancel.get("rejectReason") or "unknown"))
        return fill

    def open(self, instrument: str, side: str, units: float) -> dict:
        quote = self.quote(instrument)
        info = self.instruments(instrument)
        precision = int((info[0] if info else {}).get("displayPrecision", 3))
        band = config.BROKER_STOP_PCT / 100.0
        stop = quote["bid"] * (1 - band) if side == "long" else quote["ask"] * (1 + band)
        signed = units if side == "long" else -units
        fill = self._filled(self._call("POST", "/orders", {"order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": _units_str(signed),
            "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY",
            "stopLossOnFill": {"price": "%.*f" % (precision, stop), "timeInForce": "GTC"},
        }}))
        trade_id = (fill.get("tradeOpened") or {}).get("tradeID", "")
        return {"dealId": trade_id, "level": fill.get("price")}

    def close(self, position: OpenPosition) -> dict:
        fill = self._filled(self._call("PUT", "/trades/%s/close" % position.deal_id, {"units": "ALL"}))
        return {"dealId": position.deal_id, "level": fill.get("price")}


def _units_str(units: float) -> str:
    return str(int(units)) if float(units).is_integer() else str(units)


def stake() -> float:
    try:
        units = float(os.environ.get("OANDA_UNITS") or 1)
    except ValueError:
        raise BrokerError("OANDA_UNITS is not a number")
    if units <= 0 or units > config.BROKER_MAX_UNITS:
        raise BrokerError("OANDA_UNITS %s outside 0 to %s" % (units, config.BROKER_MAX_UNITS))
    return units


def sync(desired: str, allow_open: bool, client: Optional[Oanda] = None) -> str:
    """Make OANDA match the paper position and return a note for the signals row.

    Closes anything on the instrument that disagrees with `desired`. Opens only
    when the paper rules just entered (`allow_open`), so a missed entry is never
    chased at a worse price later.
    """
    if mode() == "off":
        return ""
    tag = "oanda_demo"
    try:
        instrument = (os.environ.get("OANDA_INSTRUMENT") or DEFAULT_INSTRUMENT).strip()
        client = client or Oanda()
        client.login()

        done = []
        remaining = []
        for pos in client.positions(instrument):
            if pos.side == desired:
                remaining.append(pos)
                continue
            result = client.close(pos)
            done.append("closed:%s@%s" % (pos.deal_id, result.get("level")))

        if desired in ("long", "short") and not remaining:
            if allow_open:
                result = client.open(instrument, desired, stake())
                done.append("opened:%s:%s@%s" % (desired, result.get("dealId"), result.get("level")))
            else:
                done.append("out_of_sync_no_position")

        return "%s=%s" % (tag, ",".join(done) or "in_sync")
    except Exception as exc:  # noqa: BLE001 - the broker must never fail the run
        return ("%s_error=%s: %s" % (tag, type(exc).__name__, exc))[:240]
