#!/usr/bin/env python3
"""
DoD (Dept. of War) daily contracts backfill.

The Pentagon publishes a same-day digest of contract awards >= $7.5M at
war.gov/News/Contracts/. That HTML sits behind Akamai bot-blocking, so a server
can't fetch it directly. But two adjacent endpoints ARE reachable:
  1. the contracts RSS feed (article titles + URLs, no award text), and
  2. the Wayback Machine, which archives each daily article ~1 day later.

So this is a ~1-day-lagged BACKFILL, not a same-day source: it catches DoD
awards to watchlist companies that weren't self-announced via press release/8-K.
De-dup on seen_awards.json keeps it from re-alerting anything already surfaced.

Fetched once per run (not per company); returns {ticker: [award, ...]}.
Testable alone:
    python -c "import json,dod; print(dod.fetch_dod_contracts(json.load(open('data/watchlist.json'))['watchlist'], {}, 5))"
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from html import unescape

import requests

log = logging.getLogger(__name__)

DOD_RSS = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max={max}"
WAYBACK_AVAIL = "https://archive.org/wayback/available?url={url}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; GovContractMonitor/1.0)"}

# Section headers used to attribute each award to a service/agency.
_BRANCHES = [
    "DEFENSE ADVANCED RESEARCH PROJECTS AGENCY", "DEFENSE LOGISTICS AGENCY",
    "DEFENSE INFORMATION SYSTEMS AGENCY", "DEFENSE THREAT REDUCTION AGENCY",
    "DEFENSE HEALTH AGENCY", "MISSILE DEFENSE AGENCY",
    "WASHINGTON HEADQUARTERS SERVICES", "U.S. TRANSPORTATION COMMAND",
    "U.S. SPECIAL OPERATIONS COMMAND", "SPACE FORCE", "AIR FORCE",
    "MARINE CORPS", "ARMY", "NAVY",
]


def _amount(text: str) -> float:
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def _to_text(html: str) -> str:
    """Strip an archived article page down to readable paragraph text."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>", "\n\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return t


def _parse_article(text: str, watchlist: list, article_date: str, url: str) -> list:
    """Award dicts for any watchlist company named in a daily contracts article."""
    awards = []
    current_branch = "Department of War"
    for para in text.split("\n\n"):
        p = para.strip()
        if not p:
            continue
        up = p.upper()
        branch = next(
            (b for b in _BRANCHES if up == b or up.startswith(b + " ")), None
        )
        if branch and len(p) < 70:
            current_branch = branch.title()
            continue
        low = p.lower()
        if "awarded" not in low and "contract" not in low:
            continue
        for company in watchlist:
            names = company.get("search_names") or [company.get("name", "")]
            if not any(n and n.lower() in low for n in names):
                continue
            mno = re.search(r"\(([A-Z0-9][A-Z0-9\-]{6,})\)\s*\.?\s*$", p)
            contract_no = mno.group(1) if mno else ""
            uid = "dod_" + hashlib.sha1(
                f"{company['ticker']}|{contract_no or p[:80]}".encode()
            ).hexdigest()[:16]
            awards.append({
                "source": "DoD Contracts",
                "id": uid,
                "company_name": company.get("name"),
                "ticker": company["ticker"],
                "description": p[:500],
                "value_usd": _amount(p),
                "agency": current_branch,
                "contract_type": "DoD Daily Contract Announcement",
                "award_date": article_date,
                "url": url,
                "ic_redacted": False,
                "is_new": True,
            })
            break  # at most one award per company per paragraph
    return awards


def fetch_dod_contracts(watchlist: list, config: dict, days_back: int = 3) -> dict:
    """Fetch recent DoD daily contract digests (via Wayback) and match the
    watchlist. Returns {ticker: [award, ...]}. Never raises."""
    cfg = config.get("dod", {})
    if not cfg.get("enabled", True):
        return {}
    max_articles = cfg.get("max_articles", 5)
    timeout = cfg.get("timeout_seconds", 20)
    rss_url = cfg.get("rss_url") or DOD_RSS.format(max=max_articles)

    try:
        import feedparser
    except ImportError:
        log.warning("feedparser not installed — skipping DoD contracts")
        return {}

    # 1. Recent daily contract articles from the reachable RSS feed.
    try:
        r = requests.get(rss_url, headers=UA, timeout=timeout)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        log.warning(f"DoD contracts RSS unavailable: {e}")
        return {}

    cutoff = datetime.utcnow() - timedelta(days=days_back)
    by_ticker: dict = {}
    for entry in feed.entries[:max_articles]:
        link = entry.get("link", "")
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if not link or not pub:
            continue
        pub_dt = datetime(*pub[:6])
        if pub_dt < cutoff:
            continue
        article_date = pub_dt.strftime("%Y-%m-%d")

        # 2. The war.gov article is Akamai-blocked — fetch the Wayback snapshot.
        try:
            av = requests.get(
                WAYBACK_AVAIL.format(url=link), headers=UA, timeout=timeout
            ).json()
            snap = av.get("archived_snapshots", {}).get("closest", {})
            if not snap.get("available"):
                log.info(f"DoD {article_date}: not yet archived — will retry next run")
                continue
            ar = requests.get(snap["url"], headers=UA, timeout=timeout)
            ar.raise_for_status()
        except Exception as e:
            log.warning(f"DoD {article_date}: Wayback fetch failed: {e}")
            continue

        for aw in _parse_article(_to_text(ar.text), watchlist, article_date, link):
            by_ticker.setdefault(aw["ticker"], []).append(aw)

    if by_ticker:
        n = sum(len(v) for v in by_ticker.values())
        log.info(f"DoD contracts: matched {n} award(s) for {sorted(by_ticker)}")
    return by_ticker
