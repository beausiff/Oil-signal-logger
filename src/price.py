"""Brent price via OilPriceAPI.

GET https://api.oilpriceapi.com/v1/prices/latest?by_code=BRENT_CRUDE_USD
with an `Authorization: Token <key>` header. A closed market or a failed call
returns a blank price and the run continues.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

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


def _price_points(payload) -> List[Tuple[datetime, float]]:
    """Pull (timestamp, price) pairs out of a range response.

    The range endpoint's exact envelope is not pinned down in the public docs,
    so accept the shapes it plausibly returns rather than guessing one and
    failing silently: a bare list, {"data": [...]}, or {"data": {"prices": [...]}}.
    """
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("prices", "results", "items"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
    if not rows:
        return []

    points: List[Tuple[datetime, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        # Deliberately NOT _extract_timestamp: that defaults to "now", which is
        # right for the latest price and badly wrong for a historical point.
        raw = next((row[k] for k in _TIMESTAMP_KEYS if row.get(k)), None)
        when = parse_timestamp(raw) if raw else None
        if when is not None:
            points.append((when, price))
    points.sort(key=lambda item: item[0])
    return points


def parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:  # some feeds return unix seconds
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_brent_range(
    start_utc: datetime,
    end_utc: datetime,
    session: Optional[requests.Session] = None,
) -> List[Tuple[datetime, float]]:
    """Every logged Brent price between two times, oldest first.

    Returns an empty list on any failure. The caller then falls back to the
    prices we logged ourselves, and leaves the cell blank if neither has a
    point close enough to the target.
    """
    api_key = os.environ.get("OILPRICE_API_KEY", "").strip()
    if not api_key:
        return []

    session = session or requests.Session()
    url = config.OILPRICE_BASE_URL + config.OILPRICE_RANGE_PATH
    params = {
        "by_code": config.BRENT_CODE,
        "by_period[from]": int(start_utc.timestamp()),
        "by_period[to]": int(end_utc.timestamp()),
        "per_page": config.OILPRICE_RANGE_PER_PAGE,
    }
    try:
        response = session.get(
            url,
            params=params,
            headers={"Authorization": "Token %s" % api_key},
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        points = _price_points(response.json())
    except requests.RequestException as exc:
        print("price range fetch failed: %s" % type(exc).__name__)
        return []
    except ValueError:
        print("price range returned bad JSON")
        return []

    if len(points) >= config.OILPRICE_RANGE_PAGE_CAP:
        print(
            "price range %s to %s: %d points (AT PAGE CAP, window may be truncated)"
            % (start_utc.isoformat(), end_utc.isoformat(), len(points))
        )
    else:
        print(
            "price range %s to %s: %d points"
            % (start_utc.isoformat(), end_utc.isoformat(), len(points))
        )
    return points


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
