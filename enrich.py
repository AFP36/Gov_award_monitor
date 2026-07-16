#!/usr/bin/env python3
"""
Enrichment layer for the agentic scorer.

Deterministic, no-LLM helpers that pull fundamentals from SEC EDGAR
(companyfacts), a price/market-cap quote, and a recent-filings list for a
watchlist company. Everything degrades gracefully: a source that blocks or
404s yields ``None``/empty rather than raising, so the scorer can reason about
whatever is available and reflect gaps in its confidence.

Kept separate from monitor.py so each source is testable in isolation:
    python -c "import enrich, json; print(json.dumps(enrich.fetch_fundamentals('KTOS'), indent=2, default=str))"
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CIK_MAP_PATH = BASE_DIR / "data" / "cik_map.json"

# SEC asks for a descriptive UA with contact info on all programmatic access.
SEC_HEADERS = {"User-Agent": "GovContractMonitor/1.0 research@afeinerproduction.com"}
# Yahoo's public chart endpoint rejects the default requests UA.
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GovContractMonitor/1.0)"}

# US-GAAP concepts we look for, in preference order (companies tag revenue etc.
# under several synonyms depending on filing era / accounting standard).
_CONCEPTS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "assets": ["Assets"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "current_debt": ["LongTermDebtCurrent", "DebtCurrent"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

_cik_cache: dict | None = None


def _load_cik_map() -> dict:
    """Ticker -> zero-padded 10-digit CIK, cached on disk and in memory."""
    global _cik_cache
    if _cik_cache is not None:
        return _cik_cache

    if CIK_MAP_PATH.exists():
        try:
            import json

            with open(CIK_MAP_PATH) as f:
                _cik_cache = json.load(f)
            return _cik_cache
        except Exception:
            pass  # fall through and refetch

    _cik_cache = {}
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        for row in r.json().values():
            ticker = row.get("ticker", "").upper()
            cik = str(row.get("cik_str", "")).zfill(10)
            if ticker:
                _cik_cache[ticker] = cik
        try:
            import json

            CIK_MAP_PATH.parent.mkdir(exist_ok=True)
            with open(CIK_MAP_PATH, "w") as f:
                json.dump(_cik_cache, f)
        except Exception:
            pass
    except Exception as e:
        log.warning(f"Could not fetch SEC ticker->CIK map: {e}")

    return _cik_cache


def resolve_cik(ticker: str) -> str | None:
    return _load_cik_map().get(ticker.upper())


# ---------------------------------------------------------------------------
# companyfacts parsing
# ---------------------------------------------------------------------------

def _fetch_companyfacts(cik: str) -> dict | None:
    try:
        r = requests.get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            headers=SEC_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"EDGAR companyfacts error for CIK {cik}: {e}")
        return None


def _unit_list(facts: dict, names: list[str]) -> list[dict]:
    """First matching concept's fact list (across USD/shares units)."""
    concepts = facts.get("facts", {})
    for taxonomy in ("us-gaap", "dei"):
        section = concepts.get(taxonomy, {})
        for name in names:
            units = section.get(name, {}).get("units")
            if units:
                # Prefer USD, else shares, else whatever's first.
                for key in ("USD", "shares"):
                    if key in units:
                        return units[key]
                return next(iter(units.values()))
    return []


def _days(start: str, end: str) -> int:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except Exception:
        return 0


def _annual_flow(facts: dict, names: list[str]) -> list[tuple[str, float]]:
    """(end_date, value) pairs for full-year periods, newest first."""
    out: dict[str, float] = {}
    for e in _unit_list(facts, names):
        start, end, val = e.get("start"), e.get("end"), e.get("val")
        if not (start and end and val is not None):
            continue
        if 330 <= _days(start, end) <= 400 and e.get("form", "").startswith("10-K"):
            out[end] = float(val)  # later duplicates (amendments) win
    return sorted(out.items(), key=lambda kv: kv[0], reverse=True)


def _latest_instant(facts: dict, names: list[str]) -> float | None:
    """Most recent point-in-time value (balance-sheet item)."""
    best_end, best_val = None, None
    for e in _unit_list(facts, names):
        end, val = e.get("end"), e.get("val")
        if e.get("start") or end is None or val is None:
            continue
        if best_end is None or end > best_end:
            best_end, best_val = end, float(val)
    return best_val


def _safe_div(a, b):
    try:
        if a is None or b in (None, 0):
            return None
        return a / b
    except Exception:
        return None


def _pct(a, b):
    r = _safe_div(a, b)
    return round(r * 100, 2) if r is not None else None


def compute_metrics(facts: dict) -> dict:
    """Derive the value-rubric metric set from parsed companyfacts."""
    m: dict = {}

    def latest_flow(key):
        series = _annual_flow(facts, _CONCEPTS[key])
        return (series[0][1] if series else None), series

    revenue, rev_series = latest_flow("revenue")
    gross_profit, _ = latest_flow("gross_profit")
    op_income, _ = latest_flow("operating_income")
    net_income, _ = latest_flow("net_income")
    ocf, _ = latest_flow("operating_cash_flow")
    capex, _ = latest_flow("capex")
    interest, _ = latest_flow("interest_expense")
    _, share_series = latest_flow("diluted_shares")

    equity = _latest_instant(facts, _CONCEPTS["equity"])
    assets = _latest_instant(facts, _CONCEPTS["assets"])
    cur_assets = _latest_instant(facts, _CONCEPTS["current_assets"])
    cur_liab = _latest_instant(facts, _CONCEPTS["current_liabilities"])
    cash = _latest_instant(facts, _CONCEPTS["cash"])
    lt_debt = _latest_instant(facts, _CONCEPTS["long_term_debt"]) or 0
    cur_debt = _latest_instant(facts, _CONCEPTS["current_debt"]) or 0
    total_debt = (lt_debt + cur_debt) or None

    fcf = (ocf - capex) if (ocf is not None and capex is not None) else None
    invested_capital = (
        (total_debt or 0) + equity if equity is not None else None
    )

    # Revenue growth: latest full year vs prior full year.
    rev_growth = None
    if len(rev_series) >= 2 and rev_series[1][1]:
        rev_growth = _pct(rev_series[0][1] - rev_series[1][1], rev_series[1][1])

    # Diluted-share trend: latest vs prior full year (dilution if positive).
    share_trend = None
    if len(share_series) >= 2 and share_series[1][1]:
        share_trend = _pct(
            share_series[0][1] - share_series[1][1], share_series[1][1]
        )

    m.update(
        {
            "fiscal_year_end": rev_series[0][0] if rev_series else None,
            "revenue_ttm": revenue,
            "revenue_growth_pct": rev_growth,
            "gross_margin_pct": _pct(gross_profit, revenue),
            "operating_margin_pct": _pct(op_income, revenue),
            "net_margin_pct": _pct(net_income, revenue),
            "roe_pct": _pct(net_income, equity),
            "roic_pct": _pct(op_income, invested_capital),
            "fcf": fcf,
            "fcf_margin_pct": _pct(fcf, revenue),
            "debt_to_equity": round(_safe_div(total_debt, equity), 2)
            if _safe_div(total_debt, equity) is not None
            else None,
            "interest_coverage": round(_safe_div(op_income, interest), 2)
            if _safe_div(op_income, interest) is not None
            else None,
            "current_ratio": round(_safe_div(cur_assets, cur_liab), 2)
            if _safe_div(cur_assets, cur_liab) is not None
            else None,
            "cash": cash,
            "total_debt": total_debt,
            "diluted_share_trend_pct": share_trend,
            "net_income": net_income,
            "stockholders_equity": equity,
            "total_assets": assets,
        }
    )
    return m


# ---------------------------------------------------------------------------
# Public enrichment functions (also exposed to the agent as tools)
# ---------------------------------------------------------------------------

def fetch_fundamentals(ticker: str) -> dict:
    """EDGAR-derived fundamentals for a ticker. Never raises."""
    cik = resolve_cik(ticker)
    if not cik:
        return {"ticker": ticker, "error": "CIK not found for ticker", "metrics": {}}
    facts = _fetch_companyfacts(cik)
    if not facts:
        return {"ticker": ticker, "cik": cik, "error": "companyfacts unavailable", "metrics": {}}
    metrics = compute_metrics(facts)
    shares = _latest_instant(facts, ["EntityCommonStockSharesOutstanding"])
    return {
        "ticker": ticker,
        "cik": cik,
        "entity_name": facts.get("entityName"),
        "shares_outstanding": shares,
        "metrics": metrics,
    }


def fetch_quote(ticker: str, shares_outstanding: float | None = None) -> dict:
    """Latest price and (if shares known) derived market cap. Never raises.

    Uses Yahoo's public chart endpoint, which needs no auth but can rate-limit
    or block; on failure we return ``price=None`` and let the caller degrade.
    """
    result: dict = {"ticker": ticker, "price": None, "market_cap": None, "currency": None}
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers=YAHOO_HEADERS,
            params={"range": "1d", "interval": "1d"},
            timeout=15,
        )
        r.raise_for_status()
        meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        result["price"] = price
        result["currency"] = meta.get("currency")
        # Yahoo doesn't return market cap here; derive from EDGAR share count.
        if price is not None and shares_outstanding:
            result["market_cap"] = round(price * shares_outstanding)
    except Exception as e:
        log.warning(f"Quote fetch failed for {ticker}: {e}")
        result["error"] = str(e)
    return result


def fetch_recent_filings(ticker: str, limit: int = 15) -> dict:
    """Recent SEC filings (form, date, doc URL) for context. Never raises."""
    cik = resolve_cik(ticker)
    if not cik:
        return {"ticker": ticker, "error": "CIK not found", "filings": []}
    try:
        r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        cik_int = int(cik)
        filings = []
        for i in range(min(limit, len(forms))):
            accn_nodash = accns[i].replace("-", "") if i < len(accns) else ""
            filings.append(
                {
                    "form": forms[i],
                    "filed": dates[i] if i < len(dates) else None,
                    "url": (
                        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                        f"{accn_nodash}/{docs[i] if i < len(docs) else ''}"
                    ),
                }
            )
        return {"ticker": ticker, "cik": cik, "filings": filings}
    except Exception as e:
        log.warning(f"Filings fetch failed for {ticker}: {e}")
        return {"ticker": ticker, "error": str(e), "filings": []}


def enrich_company(ticker: str) -> dict:
    """Bundle fundamentals + quote for the scoring prompt (deterministic pre-fetch)."""
    fundamentals = fetch_fundamentals(ticker)
    quote = fetch_quote(ticker, fundamentals.get("shares_outstanding"))
    filings = fetch_recent_filings(ticker, limit=8)
    return {
        "ticker": ticker,
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
        "fundamentals": fundamentals,
        "quote": quote,
        "recent_filings": filings.get("filings", []),
    }
