#!/usr/bin/env python3
"""
Gov Contract Monitor
Monitors SAM.gov, USASpending.gov, and SEC EDGAR for contract awards
matching a watchlist of small/micro-cap defense & tech tickers.
"""

import argparse
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

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
WATCHLIST_PATH = BASE_DIR / "data" / "watchlist.json"
SEEN_PATH = BASE_DIR / "data" / "seen_awards.json"
LOG_PATH = BASE_DIR / "logs" / "monitor.log"

# Ensure required directories exist before logging initializes
(BASE_DIR / "logs").mkdir(exist_ok=True)
(BASE_DIR / "output").mkdir(exist_ok=True)

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


def load_config() -> dict:
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


# ---------------------------------------------------------------------------
# SAM.gov
# ---------------------------------------------------------------------------

def fetch_sam_awards(company: dict, config: dict, days_back: int = 1) -> list:
    """Query SAM.gov contract awards for a company."""
    api_key = config["sam_gov"].get("api_key", "")
    if not api_key or not api_key.startswith("SAM-"):
        log.debug("SAM.gov API key not configured — skipping SAM search")
        return []

    awards = []
    since_dt = datetime.utcnow() - timedelta(days=days_back)
    today_dt = datetime.utcnow()
    # SAM.gov requires MM/DD/YYYY date format
    since = since_dt.strftime("%m/%d/%Y")
    today = today_dt.strftime("%m/%d/%Y")

    # SAM.gov free tier: ~10 req/min, ~1000 req/day.
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
        url = config["sam_gov"]["base_url"] + "?" + urlencode(params)
        time.sleep(12)  # SAM.gov free tier: 5 req/min safe margin
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 429:
                log.warning(f"SAM.gov rate limit hit for {name} — waiting 90s")
                time.sleep(90)
                r = requests.get(url, timeout=30)
            r.raise_for_status()
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
            log.warning(f"SAM.gov error for {name}: {redact_api_key(str(e))}")

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
# Keyword matching
# ---------------------------------------------------------------------------

def matches_keywords(award: dict, config: dict) -> bool:
    """Return True if award description matches any keyword cluster."""
    text = (award.get("description", "") + " " + award.get("agency", "")).lower()
    all_keywords = []
    for cluster in config["keyword_clusters"].values():
        all_keywords.extend(cluster)
    return any(kw.lower() in text for kw in all_keywords)


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------

def format_alert_email(alerts: list) -> tuple[str, str]:
    """Return (subject, html_body) for the alert digest."""
    high = [a for a in alerts if a["score"] >= 7]
    medium = [a for a in alerts if 4 <= a["score"] < 7]
    low = [a for a in alerts if a["score"] < 4]

    subject = f"🚨 Gov Contract Alert — {len(alerts)} new awards ({len(high)} HIGH priority) — {datetime.now().strftime('%b %d %Y')}"

    rows = ""
    for group_label, group in [("🔴 HIGH PRIORITY (Score 7-10)", high),
                                ("🟡 MEDIUM (Score 4-6)", medium),
                                ("⚪ LOW (Score 1-3)", low)]:
        if not group:
            continue
        rows += f"<h3 style='margin:20px 0 8px;color:#333;'>{group_label}</h3>"
        for a in group:
            value_str = f"${a['value_usd']:,.0f}" if a["value_usd"] else "Value undisclosed"
            ic_flag = " 🔒 <strong>IC REDACTED</strong>" if a.get("ic_redacted") else ""
            rows += f"""
            <div style='border:1px solid #e0e0e0;border-left:4px solid {"#d32f2f" if a["score"]>=7 else "#f57c00" if a["score"]>=4 else "#aaa"};
                        padding:12px 16px;margin:8px 0;border-radius:4px;background:#fafafa;'>
              <div style='display:flex;justify-content:space-between;align-items:center;'>
                <span style='font-size:20px;font-weight:bold;color:#1565c0;'>{a["ticker"]}</span>
                <span style='background:{"#ffebee" if a["score"]>=7 else "#fff8e1" if a["score"]>=4 else "#f5f5f5"};
                             color:{"#c62828" if a["score"]>=7 else "#e65100" if a["score"]>=4 else "#666"};
                             padding:3px 10px;border-radius:12px;font-size:13px;font-weight:500;'>
                  Score: {a["score"]}/10
                </span>
              </div>
              <p style='margin:6px 0 2px;font-size:15px;font-weight:500;color:#333;'>{a["company_name"]}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Value:</strong> {value_str}{ic_flag}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Agency:</strong> {a.get("agency","Unknown")}</p>
              <p style='margin:2px 0;font-size:13px;color:#555;'><strong>Type:</strong> {a.get("contract_type","")}</p>
              <p style='margin:4px 0;font-size:12px;color:#777;'>{a.get("description","")[:200]}...</p>
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
          {len(alerts)} new awards found · {datetime.now().strftime('%A, %B %d %Y %H:%M UTC')}
        </p>
      </div>
      {rows}
      <p style='font-size:11px;color:#999;margin-top:24px;border-top:1px solid #eee;padding-top:12px;'>
        Sources: SAM.gov · USASpending.gov · SEC EDGAR<br>
        This is a research tool. Not financial advice. Always verify before trading.
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
    high = [a for a in alerts if a["score"] >= 7]
    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Gov Contract Monitor* — {len(alerts)} new awards, {len(high)} HIGH priority"
        }
    }]
    for a in alerts[:5]:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*{a['ticker']}* — Score {a['score']}/10\n"
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

    for company in watchlist:
        ticker = company["ticker"]
        log.info(f"Checking {ticker} — {company['name']}")

        raw_awards = []
        raw_awards += fetch_usaspending_awards(company, config, days_back)
        raw_awards += fetch_sam_awards(company, config, days_back)
        raw_awards += fetch_edgar_filings(company, days_back)

        for award in raw_awards:
            uid = award["id"]
            if uid in seen:
                continue
            if award["value_usd"] < min_value and not award.get("ic_redacted"):
                continue
            if not matches_keywords(award, config) and not award.get("ic_redacted"):
                # Still include EDGAR filings even without keyword match
                if award["source"] != "SEC EDGAR 8-K":
                    continue

            award["score"] = score_award(award, company, config)
            all_alerts.append(award)
            seen.add(uid)
            log.info(f"  NEW: {ticker} | {award['source']} | ${award['value_usd']:,.0f} | Score {award['score']}")

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


def main():
    parser = argparse.ArgumentParser(description="Gov Contract Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single sweep then exit")
    parser.add_argument("--ticker", metavar="TICKER", help="Check a single ticker (e.g. --ticker KTOS)")
    parser.add_argument("--days", type=int, metavar="N", help="Look back N days instead of the config default")
    args = parser.parse_args()

    log.info("Gov Contract Monitor starting...")
    config = load_config()
    interval = config["alert_settings"]["run_interval_minutes"]

    if args.once or args.ticker or args.days:
        run_monitor(ticker_filter=args.ticker, days_override=args.days)
        return

    # Continuous mode: run immediately then on schedule
    run_monitor()
    schedule.every(interval).minutes.do(run_monitor)
    log.info(f"Scheduler active — running every {interval} minutes. Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
