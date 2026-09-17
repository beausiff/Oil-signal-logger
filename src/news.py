"""Fetch, clean and dedupe oil related headlines.

Primary source is Google News RSS search (free, no key). GNews is used as an
optional second source when GNEWS_API_KEY is set.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlencode

import feedparser
import requests

from . import config


@dataclass
class Headline:
    hash: str
    published_utc: str
    source: str
    title: str
    url: str

    def as_dict(self) -> dict:
        return asdict(self)


_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
_TRAILING_SOURCE = re.compile(r"\s+-\s+[^-]{2,40}$")


def normalise_title(title: str) -> str:
    """Lowercase, strip the trailing ' - Source' suffix, drop punctuation."""
    text = html.unescape(title or "").strip()
    text = _TRAILING_SOURCE.sub("", text)
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def headline_hash(title: str, source: str) -> str:
    key = "%s|%s" % (normalise_title(title), (source or "").strip().lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def build_query() -> str:
    parts = []
    for term in config.SEARCH_TERMS:
        parts.append('"%s"' % term if " " in term else term)
    return "(%s) when:1d" % " OR ".join(parts)


def _google_news_url() -> str:
    params = dict(config.GOOGLE_NEWS_PARAMS)
    params["q"] = build_query()
    return "%s?%s" % (config.GOOGLE_NEWS_RSS, urlencode(params))


def _entry_published_utc(entry) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)


def _entry_source(entry) -> str:
    source = getattr(entry, "source", None)
    if source is not None:
        title = getattr(source, "title", None) or (source.get("title") if hasattr(source, "get") else None)
        if title:
            return str(title).strip()
    raw = html.unescape(getattr(entry, "title", "") or "")
    match = _TRAILING_SOURCE.search(raw)
    return match.group(0).lstrip(" -").strip() if match else "unknown"


def _clean_title(raw: str) -> str:
    text = html.unescape(raw or "").strip()
    return _TRAILING_SOURCE.sub("", text).strip()


def fetch_google_news(session: Optional[requests.Session] = None) -> List[Headline]:
    session = session or requests.Session()
    url = _google_news_url()
    response = session.get(
        url,
        timeout=config.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "oil-signal-logger/1.0"},
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    out: List[Headline] = []
    for entry in feed.entries:
        published = _entry_published_utc(entry)
        if published is None:
            continue
        title = _clean_title(getattr(entry, "title", ""))
        if not title:
            continue
        source = _entry_source(entry)
        out.append(
            Headline(
                hash=headline_hash(title, source),
                published_utc=published.isoformat(),
                source=source,
                title=title,
                url=getattr(entry, "link", "") or "",
            )
        )
    return out


def fetch_gnews(session: Optional[requests.Session] = None) -> List[Headline]:
    """Optional second source. Returns an empty list when no key is configured."""
    api_key = os.environ.get("GNEWS_API_KEY", "").strip()
    if not api_key:
        return []

    session = session or requests.Session()
    params = {
        "q": " OR ".join(config.SEARCH_TERMS),
        "lang": "en",
        "max": 50,
        "sortby": "publishedAt",
        "apikey": api_key,
    }
    try:
        response = session.get(
            config.GNEWS_ENDPOINT, params=params, timeout=config.HTTP_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        articles = response.json().get("articles", [])
    except (requests.RequestException, ValueError):
        return []

    out: List[Headline] = []
    for article in articles:
        title = _clean_title(article.get("title", ""))
        source = (article.get("source") or {}).get("name") or "unknown"
        published = article.get("publishedAt")
        if not title or not published:
            continue
        out.append(
            Headline(
                hash=headline_hash(title, source),
                published_utc=published,
                source=source,
                title=title,
                url=article.get("url", ""),
            )
        )
    return out


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_and_dedupe(
    headlines: Iterable[Headline],
    known_hashes: Iterable[str],
    now_utc: datetime,
    lookback_minutes: int = config.NEWS_LOOKBACK_MINUTES,
    cap: int = config.MAX_HEADLINES_PER_RUN,
) -> List[Headline]:
    """Keep recent, unseen headlines. Most recent first, capped."""
    cutoff = now_utc - timedelta(minutes=lookback_minutes)
    seen = set(known_hashes)
    kept: List[Headline] = []

    for headline in headlines:
        published = _parse_iso(headline.published_utc)
        if published is None or published < cutoff:
            continue
        if headline.hash in seen:
            continue
        seen.add(headline.hash)
        kept.append(headline)

    kept.sort(key=lambda h: _parse_iso(h.published_utc) or cutoff, reverse=True)
    return kept[:cap]


def collect(known_hashes: Iterable[str], now_utc: datetime) -> List[Headline]:
    raw: List[Headline] = []
    try:
        raw.extend(fetch_google_news())
    except requests.RequestException as exc:  # a dead feed must not kill the run
        print("google news fetch failed: %s" % exc)
    raw.extend(fetch_gnews())
    return filter_and_dedupe(raw, known_hashes, now_utc)
