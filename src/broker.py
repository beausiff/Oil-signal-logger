"""Mirror the paper position onto an IG demo account.

Off unless BROKER_MODE=demo. There is no live mode: pointing this at real
money needs a code change, on purpose.

The broker is the source of truth for what is actually open, so every call
starts by reading IG's open positions on the epic. That makes a rerun safe: it
never opens a second position, and a close that failed last hour is retried
this hour.

Nothing in here may fail the hourly run. Every error becomes a note.

Environment:
    BROKER_MODE    off (default) | demo
    IG_API_KEY     demo API key from My IG > Settings > API keys
    IG_USERNAME    demo login
    IG_PASSWORD    demo password
    IG_ACCOUNT_ID  optional, which demo account to trade on
    IG_EPIC        the market, e.g. found with the ig_check workflow
    IG_EXPIRY      DFB for a daily funded spread bet, "-" for a CFD
    IG_SIZE        stake per point, capped at config.BROKER_MAX_SIZE
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import requests

from . import config

DEMO_URL = "https://demo-api.ig.com/gateway/deal"


class BrokerError(Exception):
    pass


def mode() -> str:
    value = (os.environ.get("BROKER_MODE") or "off").strip().lower()
    return value if value in ("off", "demo") else "off"


@dataclass
class OpenPosition:
    deal_id: str
    side: str  # long | short
    size: float


class IG:
    """The handful of IG REST calls this needs, nothing more."""

    def __init__(self, http=None, base_url: str = DEMO_URL):
        self.http = http or requests.Session()
        self.base = base_url
        self.api_key = os.environ.get("IG_API_KEY", "")
        self.tokens: dict = {}

    def _headers(self, version: int, extra: Optional[dict] = None) -> dict:
        headers = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "Version": str(version),
        }
        headers.update(self.tokens)
        if extra:
            headers.update(extra)
        return headers

    def _call(self, method: str, path: str, version: int, body=None, extra=None) -> dict:
        resp = self.http.request(
            method, self.base + path, json=body,
            headers=self._headers(version, extra), timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            raise BrokerError("%s %s -> %s %s" % (method, path, resp.status_code, resp.text[:200]))
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            return {}

    def login(self) -> None:
        username = os.environ.get("IG_USERNAME", "")
        password = os.environ.get("IG_PASSWORD", "")
        if not (self.api_key and username and password):
            raise BrokerError("missing IG_API_KEY, IG_USERNAME or IG_PASSWORD")
        resp = self.http.request(
            "POST", self.base + "/session",
            json={"identifier": username, "password": password},
            headers=self._headers(2), timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            raise BrokerError("login -> %s %s" % (resp.status_code, resp.text[:200]))
        self.tokens = {
            "CST": resp.headers.get("CST", ""),
            "X-SECURITY-TOKEN": resp.headers.get("X-SECURITY-TOKEN", ""),
        }
        wanted = (os.environ.get("IG_ACCOUNT_ID") or "").strip()
        current = (resp.json() or {}).get("currentAccountId", "")
        if wanted and wanted != current:
            self._call("PUT", "/session", 1, {"accountId": wanted, "defaultAccount": False})

    def positions(self, epic: str) -> List[OpenPosition]:
        data = self._call("GET", "/positions", 2)
        out = []
        for item in data.get("positions", []):
            if (item.get("market") or {}).get("epic") != epic:
                continue
            pos = item.get("position") or {}
            out.append(OpenPosition(
                deal_id=pos.get("dealId", ""),
                side="long" if pos.get("direction") == "BUY" else "short",
                size=float(pos.get("size") or 0),
            ))
        return out

    def market(self, epic: str) -> dict:
        return self._call("GET", "/markets/%s" % epic, 3)

    def confirm(self, deal_reference: str) -> dict:
        data = self._call("GET", "/confirms/%s" % deal_reference, 1)
        if data.get("dealStatus") != "ACCEPTED":
            raise BrokerError("rejected: %s" % data.get("reason", "unknown"))
        return data

    def open(self, epic: str, expiry: str, side: str, size: float) -> dict:
        info = self.market(epic)
        snap = info.get("snapshot") or {}
        bid, offer = snap.get("bid"), snap.get("offer")
        if bid is None or offer is None:
            raise BrokerError("no IG quote for %s" % epic)
        band = config.BROKER_STOP_PCT / 100.0
        stop = bid * (1 - band) if side == "long" else offer * (1 + band)
        currencies = (info.get("instrument") or {}).get("currencies") or []
        currency = next((c.get("code") for c in currencies if c.get("isDefault")), None)
        currency = currency or (currencies[0].get("code") if currencies else "GBP")
        ref = self._call("POST", "/positions/otc", 2, {
            "epic": epic,
            "expiry": expiry,
            "direction": "BUY" if side == "long" else "SELL",
            "size": size,
            "orderType": "MARKET",
            "currencyCode": currency,
            "forceOpen": True,
            "guaranteedStop": False,
            "stopLevel": round(stop, 1),
        })
        return self.confirm(ref["dealReference"])

    def close(self, position: OpenPosition) -> dict:
        ref = self._call("POST", "/positions/otc", 1, {
            "dealId": position.deal_id,
            "direction": "SELL" if position.side == "long" else "BUY",
            "size": position.size,
            "orderType": "MARKET",
        }, extra={"_method": "DELETE"})
        return self.confirm(ref["dealReference"])


def stake() -> float:
    try:
        size = float(os.environ.get("IG_SIZE") or 1)
    except ValueError:
        raise BrokerError("IG_SIZE is not a number")
    if size <= 0 or size > config.BROKER_MAX_SIZE:
        raise BrokerError("IG_SIZE %s outside 0 to %s" % (size, config.BROKER_MAX_SIZE))
    return size


def sync(desired: str, allow_open: bool, ig: Optional[IG] = None) -> str:
    """Make IG match the paper position and return a note for the signals row.

    Closes anything on the epic that disagrees with `desired`. Opens only when
    the paper rules just entered (`allow_open`), so a missed entry is never
    chased at a worse price later.
    """
    if mode() == "off":
        return ""
    tag = "ig_demo"
    try:
        epic = (os.environ.get("IG_EPIC") or "").strip()
        if not epic:
            raise BrokerError("IG_EPIC not set")
        expiry = (os.environ.get("IG_EXPIRY") or "DFB").strip()
        ig = ig or IG()
        ig.login()

        done = []
        remaining = []
        for pos in ig.positions(epic):
            if pos.side == desired:
                remaining.append(pos)
                continue
            result = ig.close(pos)
            done.append("closed:%s@%s" % (pos.deal_id, result.get("level")))

        if desired in ("long", "short") and not remaining:
            if allow_open:
                result = ig.open(epic, expiry, desired, stake())
                done.append("opened:%s:%s@%s" % (desired, result.get("dealId"), result.get("level")))
            else:
                done.append("out_of_sync_no_position")

        return "%s=%s" % (tag, ",".join(done) or "in_sync")
    except Exception as exc:  # noqa: BLE001 - the broker must never fail the run
        return ("%s_error=%s: %s" % (tag, type(exc).__name__, exc))[:240]
