#!/usr/bin/env python3
"""
Gov Contract Monitor
Monitors SAM.gov, USASpending.gov, and SEC EDGAR for contract awards
matching a watchlist of small/micro-cap defense & tech tickers.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
import schedule

import dod
import scorer

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
WATCHLIST_PATH = BASE_DIR / "data" / "watchlist.json"
# De-dup state lives in MONITOR_STATE_DIR when set (a Render persistent disk in
# production), so it survives across the worker's scheduled runs and redeploys.
# Falls back to the repo's data/ dir for local use.
STATE_DIR = Path(os.environ.get("MONITOR_STATE_DIR") or (BASE_DIR / "data"))
SEEN_PATH = STATE_DIR / "seen_awards.json"
LOG_PATH = BASE_DIR / "logs" / "monitor.log"

# Ensure required directories exist before logging initializes
(BASE_DIR / "logs").mkdir(exist_ok=True)
(BASE_DIR / "output").mkdir(exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def redact_api_key(text: str) -> str:
    """Strip api_key query-param values out of error messages before logging."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1***REDACTED***", text)


_dotenv_loaded = False


def load_dotenv() -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (once).

    Convenience for local runs so you don't re-export secrets each terminal.
    Real environment variables always win (Render's dashboard vars take
    precedence), and .env is gitignored so nothing secret is committed.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_config() -> dict:
    load_dotenv()
    config = load_json(CONFIG_PATH)

    # Environment variables override JSON placeholders (Render cron sets these as secrets)
    if os.environ.get("SAM_GOV_API_KEY"):
        config["sam_gov"]["api_key"] = os.environ["SAM_GOV_API_KEY"]
    if os.environ.get("EMAIL_SENDER"):
        config["alert_settings"]["email"]["from_address"] = os.environ["EMAIL_SENDER"]
    if os.environ.get("EMAIL_PASSWORD"):
        config["alert_settings"]["email"]["app_password"] = os.environ["EMAIL_PASSWORD"]
    if os.environ.get("EMAIL_RECIPIENTS"):
        config["alert_settings"]["email"]["to_addresses"] = [
            addr.strip() for addr in os.environ["EMAIL_RECIPIENTS"].split(",") if addr.strip()
        ]

    return config


def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(load_json(SEEN_PATH))
    return set()


def save_seen(seen: set) -> None:
    save_json(SEEN_PATH, list(seen))


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def score_award(award: dict, company: dict, config: dict) -> int:
    """
    Score an award 1-10 for trading relevance.
    Higher = more likely to move the stock.
    """
    score = 0
    weights = config["scoring_weights"]
    value = award.get("value_usd", 0)
    revenue = company.get("annual_revenue_m", 100) * 1_000_000

    # Revenue ratio: award value vs annual revenue
    if revenue > 0:
        ratio = value / revenue
        if ratio >= 0.5:
            score += weights["revenue_ratio_multiplier"] * 3
        elif ratio >= 0.2:
            score += weights["revenue_ratio_multiplier"] * 2
        elif ratio >= 0.05:
            score += weights["revenue_ratio_multiplier"]

    # Contract type bonuses
    contract_type = award.get("contract_type", "").lower()
    description = award.get("description", "").lower()

    if "sole source" in contract_type or "sole source" in description:
        score += weights["sole_source_bonus"]
    if "ota" in contract_type or "other transaction" in description:
        score += weights["ota_bonus"]
    if award.get("ic_redacted"):
        score += weights["ic_redacted_bonus"]
    if award.get("is_new", True):
        score += weights["new_contract_bonus"]
    else:
        score += weights["recompete_penalty"]

    # Sector tier bonus
    sectors = company.get("sectors", [])
    tier1 = {"defense", "drone", "ai", "space", "intel", "isr"}
    if any(s in tier1 for s in sectors):
        score += weights["tier1_sector_bonus"]
    else:
        score += weights["tier2_sector_bonus"]

    # Dollar size absolute floor boosts
    if value >= 50_000_000:
        score += 2
    elif value >= 10_000_000:
        score += 1

    return min(max(score, 1), 10)


def score_award_dispatch(award: dict, company: dict, config: dict) -> dict:
    """Score an award with the agentic scorer, falling back to the rule-based
    engine if the LLM path is unavailable. Mutates and returns ``award``.

    Sets ``value_score`` (primary sort key), ``catalyst_score``, ``analysis``
    (the full structured dict, or None on fallback), ``scored_by``, and — for
    backward compatibility with the email/Slack renderers — ``score``.
    """
    cfg = config.get("agentic_scoring", {})
    if cfg.get("enabled", True):
        try:
            analysis = scorer.score_award_agentic(award, company, config)
            award["analysis"] = analysis
            award["catalyst_score"] = analysis.get("catalyst", {}).get("score")
            award["value_score"] = analysis.get("value", {}).get("score")
            award["score"] = award["value_score"]  # digest is sorted by value score
            award["scored_by"] = "agentic"
            return award
        except scorer.ScorerUnavailable as e:
            if not cfg.get("fallback_to_rule_based", True):
                raise
            log.warning(f"Agentic scorer unavailable ({e}); falling back to rule-based")

    rule_score = score_award(award, company, config)
    award["analysis"] = None
    award["catalyst_score"] = rule_score
    award["value_score"] = rule_score
    award["score"] = rule_score
    award["scored_by"] = "rule_based"
    return award


# ---------------------------------------------------------------------------
# SAM.gov
# ---------------------------------------------------------------------------

# SAM.gov circuit breaker. SAM is a flaky, auth-walled, quota-limited secondary
# source (see CLAUDE.md). Rather than block the whole watchlist sweep retrying a
# rate-limited endpoint, we fail fast on a 429/error and, after a few strikes,
# disable SAM for the remainder of the run and rely on USASpending + EDGAR.
_sam_breaker = {"strikes": 0, "disabled": False}


def reset_sam_breaker() -> None:
    _sam_breaker["strikes"] = 0
    _sam_breaker["disabled"] = False


def fetch_sam_awards(company: dict, config: dict, days_back: int = 1) -> list:
    """Query SAM.gov contract awards for a company (secondary, best-effort).

    Fails fast: a 429/error is logged and skipped (no long blocking retry), and
    after ``max_failures_before_disable`` strikes SAM is switched off for the
    rest of the run so one dead source can't wedge the sweep.
    """
    sam_cfg = config.get("sam_gov", {})
    if not sam_cfg.get("enabled", True):
        return []
    if _sam_breaker["disabled"]:
        return []

    api_key = sam_cfg.get("api_key", "")
    if not api_key or not api_key.startswith("SAM-"):
        log.debug("SAM.gov API key not configured — skipping SAM search")
        return []

    max_failures = sam_cfg.get("max_failures_before_disable", 2)
    req_delay = sam_cfg.get("request_delay_seconds", 12)

    def _record_failure(reason: str) -> None:
        _sam_breaker["strikes"] += 1
        log.warning(
            f"SAM.gov {reason} (strike {_sam_breaker['strikes']}/{max_failures}) — skipping"
        )
        if _sam_breaker["strikes"] >= max_failures:
            _sam_breaker["disabled"] = True
            log.warning(
                "SAM.gov disabled for the rest of this run — relying on USASpending + EDGAR"
            )

    awards = []
    # SAM.gov requires MM/DD/YYYY date format
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%m/%d/%Y")
    today = datetime.utcnow().strftime("%m/%d/%Y")

    # Use only first (most specific) search name to stay within limits.
    for name in company["search_names"][:1]:
        params = {
            "api_key": api_key,
            "ptype": "a",           # awarded contracts only
            "awardeeEntityName": name,
            "postedFrom": since,
            "postedTo": today,
            "limit": 50,
            "offset": 0,
        }
        url = sam_cfg["base_url"] + "?" + urlencode(params)
        time.sleep(req_delay)  # stay under the free-tier per-minute limit
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 429:
                # Quota/rate blocked — fail fast; the 90s retry just 429s again.
                _record_failure("rate-limited")
                return awards
            r.raise_for_status()
            _sam_breaker["strikes"] = 0  # a good response resets the breaker
            data = r.json()
            hits = data.get("opportunitiesData", [])
            for h in hits:
                award_info = h.get("award", {}) or {}
                awardee_name = award_info.get("awardee", {}).get("name", "")
                # Skip if awardee doesn't actually match our company name
                if awardee_name and name.split()[0].lower() not in awardee_name.lower():
                    continue
                # SAM.gov 'description' field is a URL to the doc, not text — use title only
                awards.append({
                    "source": "SAM.gov",
                    "id": h.get("noticeId", ""),
                    "company_name": awardee_name or name,
                    "ticker": company["ticker"],
                    "description": h.get("title", ""),
                    "value_usd": _parse_value(award_info.get("amount", 0)),
                    "agency": h.get("fullParentPathName", "").split(".")[0],
                    "contract_type": h.get("type", ""),
                    "award_date": award_info.get("date", h.get("postedDate", "")),
                    "url": f"https://sam.gov/opp/{h.get('noticeId', '')}/view",
                    "ic_redacted": False,
                    "is_new": True,
                })
        except Exception as e:
            _record_failure(f"error ({redact_api_key(str(e))})")

    return awards


# ---------------------------------------------------------------------------
# USASpending.gov
# ---------------------------------------------------------------------------

def fetch_usaspending_awards(company: dict, config: dict, days_back: int = 1) -> list:
    """Query USASpending.gov for recent contract awards."""
    awards = []
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = config["usaspending"]["awards_search"]

    for name in company["search_names"]:
        payload = {
            "filters": {
                "time_period": [{"start_date": since, "end_date": datetime.utcnow().strftime("%Y-%m-%d")}],
                "award_type_codes": ["A", "B", "C", "D"],
                "recipient_search_text": [name],
            },
            "fields": [
                "Award ID", "Recipient Name", "Description", "Award Amount",
                "Awarding Agency", "Contract Award Type", "Start Date",
                "generated_internal_id",
            ],
            "sort": "Award Amount",
            "order": "desc",
            "limit": 25,
            "page": 1,
        }
        try:
            r = requests.post(url, json=payload, timeout=20)
            r.raise_for_status()
            results = r.json().get("results", [])
            for h in results:
                award_id = h.get("generated_internal_id", h.get("Award ID", ""))
                awards.append({
                    "source": "USASpending",
                    "id": f"usas_{award_id}",
                    "company_name": h.get("Recipient Name", name),
                    "ticker": company["ticker"],
                    "description": h.get("Description", ""),
                    "value_usd": float(h.get("Award Amount", 0) or 0),
                    "agency": h.get("Awarding Agency", ""),
                    "contract_type": h.get("Contract Award Type", ""),
                    "award_date": h.get("Start Date", ""),
                    "url": f"https://www.usaspending.gov/award/{award_id}",
                    "ic_redacted": False,
                    "is_new": True,
                })
        except Exception as e:
            log.warning(f"USASpending error for {name}: {e}")

    return awards


# ---------------------------------------------------------------------------
# SEC EDGAR (8-K filings)
# ---------------------------------------------------------------------------

def fetch_edgar_filings(company: dict, days_back: int = 1) -> list:
    """Search SEC EDGAR full-text search for recent 8-K contract disclosures."""
    filings = []
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for name in company["search_names"][:2]:
        params = {
            "q": f'"{name}" "contract"',
            "dateRange": "custom",
            "startdt": since,
            "enddt": today,
            "forms": "8-K",
        }
        url = "https://efts.sec.gov/LATEST/search-index?" + urlencode(params)
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "GovContractMonitor/1.0 research@jordan.com"})
            r.raise_for_status()
            hits = r.json().get("hits", {}).get("hits", [])
            for h in hits:
                src = h.get("_source", {})
                filing_date = src.get("file_date", "")
                entity = src.get("entity_name", "")
                ic_redacted = _detect_ic_redaction(src.get("file_description", ""))
                filings.append({
                    "source": "SEC EDGAR 8-K",
                    "id": f"edgar_{h.get('_id', '')}",
                    "company_name": entity or name,
                    "ticker": company["ticker"],
                    "description": src.get("file_description", "") + " " + src.get("period_of_report", ""),
                    "value_usd": _extract_dollar_from_text(src.get("file_description", "")),
                    "agency": _extract_agency_from_text(src.get("file_description", "")),
                    "contract_type": "8-K Disclosure",
                    "award_date": filing_date,
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={src.get('entity_id','')}&type=8-K&dateb=&owner=include&count=10",
                    "ic_redacted": ic_redacted,
                    "is_new": True,
                })
        except Exception as e:
            log.warning(f"EDGAR error for {name}: {e}")

    return filings


def _detect_ic_redaction(text: str) -> bool:
    """Flag if filing appears to redact IC contract details."""
    patterns = [
        r"classified", r"redacted", r"not disclosed", r"national security",
        r"intelligence community", r"NRO", r"NGA", r"NSA", r"DIA",
    ]
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in patterns)


def _extract_dollar_from_text(text: str) -> float:
    """Extract largest dollar amount mentioned in text."""
    patterns = [
        r"\$(\d+(?:\.\d+)?)\s*billion",
        r"\$(\d+(?:\.\d+)?)\s*million",
        r"\$(\d[\d,]+)",
    ]
    for i, pattern in enumerate(patterns):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(",", ""))
            if i == 0:
                return val * 1_000_000_000
            elif i == 1:
                return val * 1_000_000
            return val
    return 0.0


def _extract_agency_from_text(text: str) -> str:
    """Pull the most likely agency name from filing text."""
    agencies = [
        "Department of Defense", "Department of the Army", "Department of the Navy",
        "Air Force", "Space Force", "DARPA", "DHS", "NASA", "NRO", "NSA",
        "Defense Intelligence", "Homeland Security", "CBP", "TSA",
    ]
    for agency in agencies:
        if agency.lower() in text.lower():
            return agency
    return ""


def _parse_value(raw) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return float(raw.replace(",", "").replace("$", "").strip() or 0)
    return 0.0


# ---------------------------------------------------------------------------
# Company press releases (RSS) — same-day self-announced awards
# ---------------------------------------------------------------------------

# Win-verb phrases that signal a press-release TITLE is announcing an award.
# Deliberately stronger than a bare "contract" (which shows up in earnings
# titles too, e.g. "backlog including $60M in contracts"): we want the verb of
# actually winning work.
_AWARD_SIGNAL_TERMS = [
    "awarded", "wins", "won ", "secures", "selected to", "selected by",
    "selected for", "receives contract", "receives order", "receives award",
    "receives task order", "task order", "delivery order", "idiq",
    "ota ", "other transaction", "sole source", "sole-source", "prime contract",
    "subcontract", "contract award", "contract to", "contract for",
    "contract from", "production order", "to provide", "to supply", "to deliver",
]

# If any of these appear in the title it's almost certainly earnings / admin /
# capital-markets news, not a fresh award — reject even if a win-verb matched.
_AWARD_EXCLUDE_TERMS = [
    "results", "quarter", "guidance", "earnings", "to report", "financial",
    "fiscal", "appoint", "personnel", "conference call", "webcast", "dividend",
    "offering", "annual meeting", "board of directors", "outlook", "prices ",
    "investor", "presentation",
]


def looks_like_award(text: str, config: dict) -> bool:
    """True if a press-release title reads like a fresh contract award."""
    pr_cfg = config.get("press_release", {})
    positives = pr_cfg.get("award_signal_terms") or _AWARD_SIGNAL_TERMS
    negatives = pr_cfg.get("award_exclude_terms") or _AWARD_EXCLUDE_TERMS
    t = text.lower()
    if any(term in t for term in negatives):
        return False
    return any(term in t for term in positives)


def fetch_press_releases(company: dict, config: dict, days_back: int = 1) -> list:
    """Pull recent contract-award press releases from a company's RSS feed.

    This is the fastest same-day signal for a material award — companies
    announce their wins immediately. Only entries that look like awards are
    returned. Degrades gracefully: a missing/blocked/malformed feed (some IR
    hosts sit behind Akamai/Cloudflare) yields [] rather than raising, and the
    EDGAR 8-K path backstops any company without a working feed.
    """
    if not config.get("press_release", {}).get("enabled", True):
        return []
    feed_url = company.get("rss_feed")
    if not feed_url:
        return []

    try:
        import feedparser
    except ImportError:
        log.warning("feedparser not installed — skipping press-release feeds")
        return []

    # Fetch with requests (which enforces a timeout) rather than letting
    # feedparser fetch — feedparser has no timeout, so a blocked/slow IR host
    # (several sit behind Akamai/Cloudflare) could otherwise hang the sweep.
    timeout = config.get("press_release", {}).get("feed_timeout_seconds", 12)
    try:
        r = requests.get(
            feed_url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GovContractMonitor/1.0)"},
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Press-release feed unavailable for {company['ticker']}: {e}")
        return []
    parsed = feedparser.parse(r.content)

    cutoff = datetime.utcnow() - timedelta(days=days_back)
    releases = []
    for entry in parsed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        text = f"{title} {summary}"
        # Detect awards from the TITLE only — award PRs headline the win, while
        # earnings/hire PRs that merely mention "contract" in the body don't.
        if not looks_like_award(title, config):
            continue
        # Window-filter by published date; skip undated entries so we don't
        # re-surface an old backlog (de-dup on seen_awards.json also guards).
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        pub_dt = datetime(*published[:6])
        if pub_dt < cutoff:
            continue
        link = entry.get("link", "") or entry.get("id", "")
        guid = entry.get("id") or link or title
        uid = "pr_" + hashlib.sha1(f"{company['ticker']}|{guid}".encode()).hexdigest()[:16]
        releases.append({
            "source": "Press Release",
            "id": uid,
            "company_name": company["name"],
            "ticker": company["ticker"],
            "description": title + (f" — {summary[:300]}" if summary else ""),
            "value_usd": _extract_dollar_from_text(text),
            "agency": _extract_agency_from_text(text),
            "contract_type": "Company Press Release",
            "award_date": pub_dt.strftime("%Y-%m-%d"),
            "url": link,
            "ic_redacted": False,
            "is_new": True,
        })
    return releases


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

_kw_pattern_cache: dict = {}


def _keyword_pattern(clusters: dict):
    """Compile (and cache) a word-boundary alternation of all cluster keywords.

    Word boundaries stop short acronyms from matching inside unrelated words
    (e.g. "ICE" must not match "off​ice", "EW" must not match "news"), while
    multi-word phrases ("space domain awareness") and hyphenated terms ("C-UAS")
    still match as whole tokens.
    """
    kws = tuple(kw.lower() for cluster in clusters.values() for kw in cluster)
    pattern = _kw_pattern_cache.get(kws)
    if pattern is None:
        alt = "|".join(re.escape(k) for k in kws)
        pattern = re.compile(r"\b(?:" + alt + r")\b") if alt else None
        _kw_pattern_cache[kws] = pattern
    return pattern


def matches_keywords(award: dict, config: dict) -> bool:
    """Return True if award description matches any keyword cluster (whole-word)."""
    text = (award.get("description", "") + " " + award.get("agency", "")).lower()
    pattern = _keyword_pattern(config["keyword_clusters"])
    return bool(pattern.search(text)) if pattern else False


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------

def _fmt_pct(v) -> str:
    return f"{v}%" if isinstance(v, (int, float)) else "n/a"


def _render_analysis(a: dict) -> str:
    """Render the agentic analysis block for one alert (empty on fallback)."""
    analysis = a.get("analysis")
    if not analysis:
        return (
            "<p style='margin:4px 0;font-size:12px;color:#999;font-style:italic;'>"
            "Rule-based score (LLM analysis unavailable this run).</p>"
        )

    catalyst = analysis.get("catalyst", {})
    value = analysis.get("value", {})
    pillars = value.get("pillars", {})
    confidence = analysis.get("confidence", "?")

    pillar_labels = {
        "moat": "Moat", "returns_on_capital": "ROIC/ROE",
        "balance_sheet": "Balance sheet", "owner_earnings_fcf": "FCF",
        "management_capital_allocation": "Mgmt/capital", "margin_of_safety": "Valuation",
    }
    pillar_bits = " · ".join(
        f"{lbl} {pillars.get(k)}" for k, lbl in pillar_labels.items() if pillars.get(k) is not None
    )
    metrics = value.get("key_metrics", []) or []
    metric_bits = " · ".join(
        f"{m.get('metric')}: {m.get('value')}" for m in metrics[:8]
        if isinstance(m, dict) and m.get("metric")
    )

    return f"""
      <div style='margin-top:8px;padding:8px 10px;background:#fff;border:1px solid #eee;border-radius:4px;'>
        <p style='margin:2px 0;font-size:12px;color:#333;'>
          <strong>Why it surfaced (catalyst {catalyst.get('score','?')}/10):</strong>
          {catalyst.get('rationale','')}
        </p>
        <p style='margin:2px 0;font-size:11px;color:#666;'>
          Materiality: {_fmt_pct(catalyst.get('materiality_pct_mktcap'))} of market cap ·
          {_fmt_pct(catalyst.get('materiality_pct_revenue'))} of revenue ·
          {catalyst.get('new_vs_recompete','?')} ·
          {"already priced in" if catalyst.get('already_priced_in') else "not yet priced in"}
        </p>
        <p style='margin:6px 0 2px;font-size:12px;color:#333;'>
          <strong>Business quality (value {value.get('score','?')}/10):</strong>
          {value.get('rationale','')}
        </p>
        <p style='margin:2px 0;font-size:11px;color:#666;'>Pillars: {pillar_bits}</p>
        {"<p style='margin:2px 0;font-size:11px;color:#888;'>Metrics: " + metric_bits + "</p>" if metric_bits else ""}
        <p style='margin:2px 0;font-size:11px;color:#999;'>Confidence: {confidence}</p>
      </div>"""


def format_alert_email(alerts: list) -> tuple[str, str]:
    """Return (subject, html_body). Sorted/grouped by VALUE score, with the
    catalyst score shown as the reason each name surfaced."""
    def vscore(a):
        return a.get("value_score") if a.get("value_score") is not None else a.get("score", 0)

    high = [a for a in alerts if vscore(a) >= 7]
    medium = [a for a in alerts if 4 <= vscore(a) < 7]
    low = [a for a in alerts if vscore(a) < 4]

    subject = (
        f"🚨 Gov Contract Alert — {len(alerts)} new awards "
        f"({len(high)} high-value) — {datetime.now().strftime('%b %d %Y')}"
    )

    rows = ""
    for group_label, group in [("🟢 HIGH VALUE (Value 7-10)", high),
                                ("🟡 MEDIUM (Value 4-6)", medium),
                                ("⚪ LOWER (Value 1-3)", low)]:
        if not group:
            continue
        rows += f"<h3 style='margin:20px 0 8px;color:#333;'>{group_label}</h3>"
        for a in group:
            vs = vscore(a)
            cs = a.get("catalyst_score", "?")
            value_str = f"${a['value_usd']:,.0f}" if a["value_usd"] else "Value undisclosed"
            ic_flag = " 🔒 <strong>IC REDACTED</strong>" if a.get("ic_redacted") else ""
            accent = "#2e7d32" if vs >= 7 else "#f57c00" if vs >= 4 else "#aaa"
            rows += f"""
            <div style='border:1px solid #e0e0e0;border-left:4px solid {accent};
                        padding:12px 16px;margin:8px 0;border-radius:4px;background:#fafafa;'>
              <div style='display:flex;justify-content:space-between;align-items:center;'>
                <span style='font-size:20px;font-weight:bold;color:#1565c0;'>{a["ticker"]}</span>
                <span>
                  <span style='background:#e8f5e9;color:#2e7d32;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:500;'>
                    Value {vs}/10</span>
                  &nbsp;
                  <span style='background:#e3f2fd;color:#1565c0;padding:3px 10px;border-radius:12px;font-size:13px;font-weight:500;'>
                    Catalyst {cs}/10</span>
                </span>
              </div>
              <p style='margin:6px 0 2px;font-size:15px;font-weight:500;color:#333;'>{a["company_name"]}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Award:</strong> {value_str}{ic_flag}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Agency:</strong> {a.get("agency","Unknown")}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Type:</strong> {a.get("contract_type","")}</p>
              <p style='margin:4px 0;font-size:12px;color:#777;'>{a.get("description","")[:200]}...</p>
              {_render_analysis(a)}
              <div style='margin-top:8px;'>
                <a href='{a["url"]}' style='font-size:12px;color:#1565c0;'>View award →</a>
                &nbsp;&nbsp;
                <a href='https://finance.yahoo.com/quote/{a["ticker"]}'
                   style='font-size:12px;color:#1565c0;'>Yahoo Finance →</a>
                &nbsp;&nbsp;
                <a href='https://efts.sec.gov/LATEST/search-index?q=%22{a["ticker"]}%22&forms=8-K'
                   style='font-size:12px;color:#1565c0;'>Latest 8-Ks →</a>
              </div>
            </div>"""

    html = f"""
    <html><body style='font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;'>
      <div style='background:#1565c0;color:white;padding:16px 20px;border-radius:6px;margin-bottom:20px;'>
        <h2 style='margin:0;'>Gov Contract Monitor</h2>
        <p style='margin:4px 0 0;opacity:.85;font-size:13px;'>
          {len(alerts)} new awards found · sorted by value score · {datetime.now().strftime('%A, %B %d %Y %H:%M UTC')}
        </p>
      </div>
      {rows}
      <p style='font-size:11px;color:#999;margin-top:24px;border-top:1px solid #eee;padding-top:12px;'>
        Sources: SAM.gov · USASpending.gov · SEC EDGAR · Anthropic Claude (scoring)<br>
        Research / screening only — this is <strong>not</strong> a recommendation to buy or sell any
        security, and not financial advice. Scores are model-generated and may be wrong. Always do
        your own due diligence and verify before trading.
      </p>
    </body></html>"""

    return subject, html


def send_email(subject: str, html_body: str, config: dict) -> None:
    cfg = config["alert_settings"]["email"]
    if not cfg.get("enabled"):
        return
    if cfg["app_password"].startswith("YOUR_"):
        log.warning("Email not configured — printing alert to console instead")
        print(f"\n{'='*60}\n{subject}\n{'='*60}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_address"]
    msg["To"] = ", ".join(cfg["to_addresses"])
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["from_address"], cfg["app_password"])
            s.sendmail(cfg["from_address"], cfg["to_addresses"], msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def send_slack(alerts: list, config: dict) -> None:
    cfg = config["alert_settings"].get("slack", {})
    if not cfg.get("enabled") or cfg.get("webhook_url", "").startswith("YOUR_"):
        return
    high = [a for a in alerts if (a.get("value_score") or a.get("score", 0)) >= 7]
    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Gov Contract Monitor* — {len(alerts)} new awards, {len(high)} high-value"
        }
    }]
    for a in alerts[:5]:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*{a['ticker']}* — Value {a.get('value_score','?')}/10 · "
                         f"Catalyst {a.get('catalyst_score','?')}/10\n"
                         f"${a['value_usd']:,.0f} | {a.get('agency','')}\n"
                         f"<{a['url']}|View award>")
            }
        })
    try:
        requests.post(cfg["webhook_url"], json={"blocks": blocks}, timeout=10)
    except Exception as e:
        log.warning(f"Slack error: {e}")


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run_monitor(ticker_filter: Optional[str] = None, days_override: Optional[int] = None) -> None:
    log.info("=== Running contract monitor ===")
    config = load_config()
    watchlist = load_json(WATCHLIST_PATH)["watchlist"]
    seen = load_seen()
    reset_sam_breaker()  # give SAM a fresh chance each run; it self-disables if blocked
    min_value = config["alert_settings"]["min_award_value_usd"]
    days_back = days_override if days_override is not None else config["alert_settings"].get("lookback_days", 1)

    if ticker_filter:
        watchlist = [c for c in watchlist if c["ticker"].upper() == ticker_filter.upper()]
        if not watchlist:
            log.error(f"Ticker {ticker_filter} not found in watchlist")
            return
        log.info(f"Filtering to single ticker: {ticker_filter}")

    if days_override:
        log.info(f"Looking back {days_back} days")

    all_alerts = []

    # DoD daily contracts are a single cross-watchlist digest — fetch once (via
    # Wayback; see dod.py) and distribute matches per company below. Uses its own
    # wider lookback to catch the ~1-day-late archived article.
    dod_by_ticker = dod.fetch_dod_contracts(
        watchlist, config, config.get("dod", {}).get("lookback_days", 3)
    )

    for company in watchlist:
        ticker = company["ticker"]
        log.info(f"Checking {ticker} — {company['name']}")

        raw_awards = []
        raw_awards += dod_by_ticker.get(ticker, [])
        raw_awards += fetch_press_releases(company, config, days_back)
        raw_awards += fetch_usaspending_awards(company, config, days_back)
        raw_awards += fetch_sam_awards(company, config, days_back)
        raw_awards += fetch_edgar_filings(company, days_back)

        # Disclosures / already-matched sources bypass the keyword gate that the
        # bulk feeds go through (a DoD digest hit is itself a watchlist match).
        disclosure_sources = {"SEC EDGAR 8-K", "Press Release", "DoD Contracts"}

        for award in raw_awards:
            uid = award["id"]
            if uid in seen:
                continue
            if (award["value_usd"] < min_value
                    and not award.get("ic_redacted")
                    and award["source"] != "Press Release"):
                continue
            if not matches_keywords(award, config) and not award.get("ic_redacted"):
                if award["source"] not in disclosure_sources:
                    continue

            score_award_dispatch(award, company, config)
            all_alerts.append(award)
            seen.add(uid)
            log.info(
                f"  NEW: {ticker} | {award['source']} | ${award['value_usd']:,.0f} | "
                f"catalyst {award['catalyst_score']} / value {award['value_score']} "
                f"({award['scored_by']})"
            )

    save_seen(seen)

    if all_alerts:
        all_alerts.sort(key=lambda x: x["score"], reverse=True)
        subject, html = format_alert_email(all_alerts)
        send_email(subject, html, config)
        send_slack(all_alerts, config)

        # Save to output log
        log_file = BASE_DIR / "output" / f"alerts_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        save_json(log_file, all_alerts)
        log.info(f"Saved {len(all_alerts)} alerts to {log_file}")
    else:
        log.info("No new matching awards found this run.")

    log.info("=== Monitor run complete ===\n")


def run_test_score(ticker: str) -> None:
    """Smoke test: score ONE synthetic award for `ticker` to prove the live
    agentic path works (valid key → Claude responds → structured output parses,
    with real EDGAR/quote enrichment). Sends no email, touches no seen-state.
    """
    config = load_config()
    watchlist = load_json(WATCHLIST_PATH)["watchlist"]
    company = next((c for c in watchlist if c["ticker"].upper() == ticker.upper()), None)
    if not company:
        log.error(f"Ticker {ticker} not in watchlist — cannot build a test award")
        return

    award = {
        "source": "TEST",
        "id": f"test_{ticker.lower()}",
        "company_name": company["name"],
        "ticker": company["ticker"],
        "description": "SYNTHETIC TEST AWARD — sole-source OTA prototype for unmanned "
        "aerial / autonomous systems. Not a real contract; used to exercise the scorer.",
        "value_usd": 50_000_000,
        "agency": "United States Space Force",
        "contract_type": "Sole Source / OTA",
        "award_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "ic_redacted": False,
        "is_new": True,
        "url": "https://example.com/test-award",
    }

    log.info(f"=== SMOKE TEST: scoring a synthetic award for {ticker} ===")
    score_award_dispatch(award, company, config)
    print("\n" + "=" * 60)
    print(f"scored_by: {award['scored_by']}  |  catalyst: {award['catalyst_score']}  |  value: {award['value_score']}")
    print("=" * 60)
    print(json.dumps(award.get("analysis") or {"note": "rule-based fallback (no analysis dict)"}, indent=2, default=str))
    if award["scored_by"] == "rule_based":
        log.warning("Fell back to rule-based — set ANTHROPIC_API_KEY to test the live LLM path.")
    else:
        log.info("Live agentic scoring succeeded ✅")


def main():
    parser = argparse.ArgumentParser(description="Gov Contract Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single sweep then exit")
    parser.add_argument("--ticker", metavar="TICKER", help="Check a single ticker (e.g. --ticker KTOS)")
    parser.add_argument("--days", type=int, metavar="N", help="Look back N days instead of the config default")
    parser.add_argument("--test-score", metavar="TICKER", help="Smoke test: score one synthetic award for TICKER and exit")
    args = parser.parse_args()

    log.info("Gov Contract Monitor starting...")
    config = load_config()

    if args.test_score:
        run_test_score(args.test_score)
        return

    if args.once or args.ticker or args.days:
        run_monitor(ticker_filter=args.ticker, days_override=args.days)
        return

    # Continuous worker mode: run once on startup, then daily at the configured
    # UTC times. Runs as a persistent Render Worker so de-dup state (on the
    # mounted disk) survives between the scheduled runs.
    run_times = config["alert_settings"].get("run_at_utc") or ["12:30", "22:00"]
    run_monitor()
    for t in run_times:
        schedule.every().day.at(t).do(run_monitor)
    log.info(
        f"Scheduler active — running daily at UTC {', '.join(run_times)} "
        f"(≈ 8:30 AM / 6:00 PM ET during EDT). Ctrl+C to stop."
    )

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
