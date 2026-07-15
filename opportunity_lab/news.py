from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .config import Config
from .markets import Market, words


log = logging.getLogger("opportunity_lab.news")
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class NewsItem:
    item_id: str
    source: str
    title: str
    url: str
    published_ts: float
    summary: str = ""

    @property
    def text(self) -> str:
        return f"{self.title} {self.summary}".strip()


def _clean(value: str | None, limit: int = 1200) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(_TAG.sub(" ", value)).split())[:limit]


def _time(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return time.time()


def parse_feed(content: bytes, source_url: str) -> list[NewsItem]:
    root = ET.fromstring(content)
    entries = root.findall(".//item")
    atom = False
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True
    source = urlparse(source_url).netloc
    result = []
    for entry in entries:
        if atom:
            title = entry.findtext("{http://www.w3.org/2005/Atom}title") or ""
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href", "") if link_node is not None else ""
            summary = (
                entry.findtext("{http://www.w3.org/2005/Atom}summary")
                or entry.findtext("{http://www.w3.org/2005/Atom}content")
                or ""
            )
            published = (
                entry.findtext("{http://www.w3.org/2005/Atom}published")
                or entry.findtext("{http://www.w3.org/2005/Atom}updated")
            )
            guid = entry.findtext("{http://www.w3.org/2005/Atom}id") or link
        else:
            title = entry.findtext("title") or ""
            link = entry.findtext("link") or ""
            summary = (
                entry.findtext("description")
                or entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or ""
            )
            published = (
                entry.findtext("pubDate")
                or entry.findtext("{http://purl.org/dc/elements/1.1/}date")
            )
            guid = entry.findtext("guid") or link
        title = _clean(title, 400)
        link = _clean(link, 1500)
        if not title or not link:
            continue
        identity = f"{source}|{guid}|{title}".encode()
        result.append(
            NewsItem(
                item_id=hashlib.sha256(identity).hexdigest(),
                source=source,
                title=title,
                url=link,
                published_ts=_time(published),
                summary=_clean(summary),
            )
        )
    return result


def fetch_feed(url: str, timeout: int = 25) -> list[NewsItem]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "opportunitylab/0.2 research bot contact=local-user",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_feed(response.read(), url)


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    return f"https://news.google.com/rss/search?{params}"


def query_markets(markets: list[Market], limit: int) -> list[Market]:
    now = time.time()

    def score(market: Market):
        close_bonus = 0.0
        if market.close_ts and market.close_ts > now:
            days = (market.close_ts - now) / 86400
            close_bonus = max(0.0, 30 - min(30, days)) * 1000
        nonsports = 1_000_000 if market.category.lower() not in {"sport", "sports"} else 0
        return nonsports + close_bonus + market.volume_24h + market.volume * 0.001

    eligible = [
        market
        for market in markets
        if market.is_open and 0.03 < (market.midpoint or 0) < 0.97
    ]
    return sorted(eligible, key=score, reverse=True)[: max(0, limit)]


def collect(cfg: Config, markets: list[Market]) -> list[NewsItem]:
    urls = list(cfg.news_feed_urls)
    for market in query_markets(markets, cfg.news_query_market_count):
        terms = list(words(market.title))[:8]
        if len(terms) >= 2:
            urls.append(google_news_url(" ".join(terms)))
    items: dict[str, NewsItem] = {}
    for url in urls:
        try:
            for item in fetch_feed(url):
                items[item.item_id] = item
        except Exception:
            log.exception("news feed failed: %s", url)
    cutoff = time.time() - cfg.news_max_age_hours * 3600
    return [item for item in items.values() if item.published_ts >= cutoff]


def relevance(market: Market, item: NewsItem) -> float:
    market_tokens = set(words(f"{market.title} {market.event_title}"))
    news_tokens = set(words(item.text))
    overlap = market_tokens & news_tokens
    if len(overlap) < 2:
        return 0.0
    coverage = len(overlap) / max(1, len(market_tokens))
    precision = len(overlap) / max(1, min(20, len(news_tokens)))
    return round(0.75 * coverage + 0.25 * precision, 6)


def relevant_pairs(
    markets: list[Market], items: list[NewsItem], limit: int = 12
) -> list[tuple[float, Market, NewsItem]]:
    candidates = []
    for market in markets:
        if not market.is_open or market.yes_ask is None or market.yes_bid is None:
            continue
        for item in items:
            score = relevance(market, item)
            if score >= 0.18:
                candidates.append((score, market, item))
    candidates.sort(key=lambda row: (row[0], row[2].published_ts), reverse=True)
    return candidates[:limit]
