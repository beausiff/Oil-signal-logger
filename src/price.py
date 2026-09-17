"""Brent price via OilPriceAPI.

GET https://api.oilpriceapi.com/v1/prices/latest?by_code=BRENT_CRUDE_USD
with an `Authorization: Token <key>` header. A closed market or a failed call
returns a blank price and the run continues.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from . import config

_TIMESTAMP_KEYS = ("created_at", "source_timestamp", "as_of", "updated_at", "timestamp")


@dataclass
class PriceQuote:
    price: Optional[float]
    price_time_utc: str
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.price is not None


def _extract_timestamp(payload: dict) -> str:
    for key in _TIMESTAMP_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return datetime.now(timezone.utc).isoformat()


def fetch_brent(session: Optional[requests.Session] = None) -> PriceQuote:
    api_key = os.environ.get("OILPRICE_API_KEY", "").strip()
    if not api_key:
        return PriceQuote(price=None, price_time_utc="", note="no_oilprice_key")

    session = session or requests.Session()
    url = config.OILPRICE_BASE_URL + config.OILPRICE_LATEST_PATH
    try:
        response = session.get(
            url,
            params={"by_code": config.BRENT_CODE},
            headers={"Authorization": "Token %s" % api_key},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        return PriceQuote(price=None, price_time_utc="", note="price_fetch_failed:%s" % type(exc).__name__)
    except ValueError:
        return PriceQuote(price=None, price_time_utc="", note="price_bad_json")

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = body if isinstance(body, dict) else {}

    raw_price = data.get("price")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return PriceQuote(price=None, price_time_utc="", note="price_missing")

    return PriceQuote(price=price, price_time_utc=_extract_timestamp(data))
