#!/usr/bin/env python3
"""
Agentic scorer — replaces the rule-based ``score_award`` with a bounded
Claude (Messages API + tool_use) step that returns structured JSON with both
a **catalyst score** (does this award matter to the stock?) and a **value
score** (is the underlying business any good, Buffett/Munger/Pabrai lens).

Design constraints (see CLAUDE.md):
  * Everything around the scorer stays deterministic; only this call is LLM.
  * Bounded tool loop — the agent may fetch fundamentals/quote/filings a fixed
    number of times, then MUST submit via the strict ``submit_analysis`` tool.
    No open-ended autonomy.
  * Structured output is guaranteed by a strict-schema tool, not free text.
  * Model is a config value (default cost-efficient; bump for deeper analysis).

If the ``anthropic`` SDK or ``ANTHROPIC_API_KEY`` is unavailable, callers should
fall back to the deterministic scorer — this module raises ``ScorerUnavailable``
rather than silently degrading, so the reliability-critical alert path stays
explicit.
"""

from __future__ import annotations

import json
import logging
import os

import enrich

log = logging.getLogger(__name__)


class ScorerUnavailable(RuntimeError):
    """Raised when the LLM scorer cannot run (no SDK / no API key)."""


# ---------------------------------------------------------------------------
# Tool definitions exposed to the agent
# ---------------------------------------------------------------------------

_FETCH_TOOLS = [
    {
        "name": "fetch_fundamentals",
        "description": (
            "Fetch SEC EDGAR companyfacts-derived fundamentals (revenue, margins, "
            "ROE/ROIC, FCF, debt, share count, etc.) for a ticker. Use to confirm or "
            "extend the fundamentals already provided."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. KTOS"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "fetch_quote",
        "description": (
            "Fetch the latest share price and derived market cap for a ticker. Use to "
            "compute the award's materiality as a percentage of market cap."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. KTOS"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "fetch_filings",
        "description": (
            "List recent SEC filings (8-K, 10-Q, 10-K) for a ticker. Use to judge "
            "whether the award was already disclosed (i.e. 'already priced in?')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. KTOS"}
            },
            "required": ["ticker"],
        },
    },
]

# Strict-schema tool the agent MUST call to return its verdict. Strict tool use
# guarantees the input validates exactly against this schema.
_SUBMIT_TOOL = {
    "name": "submit_analysis",
    "description": "Submit the final catalyst and value analysis. Call exactly once, when done.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ticker": {"type": "string"},
            "catalyst": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "integer", "description": "1-10; 10 = most likely to move the stock"},
                    "materiality_pct_mktcap": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Award value as % of market cap, or null if unknown",
                    },
                    "materiality_pct_revenue": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Award value as % of annual revenue, or null if unknown",
                    },
                    "new_vs_recompete": {
                        "type": "string",
                        "enum": ["new", "recompete", "unknown"],
                    },
                    "contract_type": {"type": "string"},
                    "already_priced_in": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "score",
                    "materiality_pct_mktcap",
                    "materiality_pct_revenue",
                    "new_vs_recompete",
                    "contract_type",
                    "already_priced_in",
                    "rationale",
                ],
            },
            "value": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "description": "1-10; sum of the six pillar scores"},
                    "pillars": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "moat": {"type": "number", "description": "0-2"},
                            "returns_on_capital": {"type": "number", "description": "0-2"},
                            "balance_sheet": {"type": "number", "description": "0-1.5"},
                            "owner_earnings_fcf": {"type": "number", "description": "0-1.5"},
                            "management_capital_allocation": {"type": "number", "description": "0-1"},
                            "margin_of_safety": {"type": "number", "description": "0-2"},
                        },
                        "required": [
                            "moat",
                            "returns_on_capital",
                            "balance_sheet",
                            "owner_earnings_fcf",
                            "management_capital_allocation",
                            "margin_of_safety",
                        ],
                    },
                    "key_metrics": {
                        "type": "array",
                        "description": "The metrics that drove the value score",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "metric": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["metric", "value"],
                        },
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["score", "pillars", "key_metrics", "rationale"],
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "sources_checked": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ticker", "catalyst", "value", "confidence", "sources_checked"],
    },
}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _system_prompt(pillar_weights: dict) -> str:
    return f"""You are an equity research screening agent for a watchlist of small- and
micro-cap U.S. defense / GovIT / space / drone / AI companies. A federal contract
award just surfaced for one of them. Produce TWO scores plus written analysis.

1. CATALYST score (1-10): how likely is THIS award to significantly move the stock?
   Drivers: materiality (award $ vs. market cap and vs. revenue), new award vs.
   recompete, sole-source / OTA vs. competed, and an "already priced in?" check
   (was it already 8-K'd or widely expected?). A tiny award on a large-cap name is
   a low catalyst even if the business is great.

2. VALUE score (1-10): the underlying business through a Warren Buffett / Charlie
   Munger / Mohnish Pabrai value lens. It is the SUM of six pillars (max points):
     - moat / business quality ....................... 0-{pillar_weights.get('moat', 2)}
     - return on capital (ROIC/ROE) .................. 0-{pillar_weights.get('returns_on_capital', 2)}
     - balance-sheet strength ....................... 0-{pillar_weights.get('balance_sheet', 1.5)}
     - owner earnings / FCF ......................... 0-{pillar_weights.get('owner_earnings_fcf', 1.5)}
     - management & capital allocation .............. 0-{pillar_weights.get('management_capital_allocation', 1)}
     - margin of safety / valuation ................. 0-{pillar_weights.get('margin_of_safety', 2)}
   Keep each pillar within its max; the value score is their sum.

You are given deterministically-fetched fundamentals and a quote. You MAY call
fetch_fundamentals / fetch_quote / fetch_filings to confirm or extend them, but
be economical — a few calls at most. When done, you MUST call submit_analysis.

Rules:
- This is RESEARCH / SCREENING only. Never recommend buying or selling; never tell
  the user to trade. Rate and explain — that is all.
- Be honest about missing data: if fundamentals or the quote are unavailable, set
  confidence to "medium" or "low" and say so in the rationale rather than inventing
  numbers. Use null for materiality percentages you cannot compute.
- Ground every claim in the provided data or a tool result."""


def _user_prompt(award: dict, company: dict, enrichment: dict) -> str:
    return (
        "AWARD (surfaced from public procurement data):\n"
        + json.dumps(
            {
                "source": award.get("source"),
                "company_name": award.get("company_name"),
                "ticker": award.get("ticker"),
                "description": award.get("description"),
                "value_usd": award.get("value_usd"),
                "agency": award.get("agency"),
                "contract_type": award.get("contract_type"),
                "award_date": award.get("award_date"),
                "ic_redacted": award.get("ic_redacted"),
                "url": award.get("url"),
            },
            indent=2,
            default=str,
        )
        + "\n\nWATCHLIST CONTEXT:\n"
        + json.dumps(
            {
                "ticker": company.get("ticker"),
                "name": company.get("name"),
                "sectors": company.get("sectors"),
                "market_cap_tier": company.get("market_cap_tier"),
                "annual_revenue_m": company.get("annual_revenue_m"),
                "notes": company.get("notes"),
            },
            indent=2,
            default=str,
        )
        + "\n\nENRICHMENT (deterministically pre-fetched; may be partial):\n"
        + json.dumps(enrichment, indent=2, default=str)
        + "\n\nAnalyze, then call submit_analysis with both scores."
    )


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _run_tool(name: str, tool_input: dict) -> str:
    ticker = tool_input.get("ticker", "")
    try:
        if name == "fetch_fundamentals":
            return json.dumps(enrich.fetch_fundamentals(ticker), default=str)
        if name == "fetch_quote":
            fund = enrich.fetch_fundamentals(ticker)
            return json.dumps(
                enrich.fetch_quote(ticker, fund.get("shares_outstanding")), default=str
            )
        if name == "fetch_filings":
            return json.dumps(enrich.fetch_recent_filings(ticker), default=str)
        return json.dumps({"error": f"unknown tool {name}"})
    except Exception as e:  # tools must never crash the loop
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def score_award_agentic(award: dict, company: dict, config: dict) -> dict:
    """Run the bounded agentic scorer and return the structured analysis dict.

    Raises ScorerUnavailable if the SDK/key are missing so the caller can fall
    back deterministically.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ScorerUnavailable("ANTHROPIC_API_KEY not set")
    try:
        import anthropic
    except ImportError as e:
        raise ScorerUnavailable("anthropic SDK not installed") from e

    cfg = config.get("agentic_scoring", {})
    model = cfg.get("model", "claude-sonnet-4-6")
    max_iterations = cfg.get("max_tool_iterations", 6)
    max_tokens = cfg.get("max_tokens", 4096)
    pillar_weights = config.get("value_pillars", {})

    client = anthropic.Anthropic(api_key=api_key)

    ticker = award.get("ticker", company.get("ticker", ""))
    enrichment = enrich.enrich_company(ticker)

    tools = _FETCH_TOOLS + [_SUBMIT_TOOL]
    messages = [{"role": "user", "content": _user_prompt(award, company, enrichment)}]
    system = _system_prompt(pillar_weights)

    for iteration in range(max_iterations):
        # On the final allowed turn, force the submit tool so we always get output.
        force_submit = iteration == max_iterations - 1
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        if force_submit:
            kwargs["tool_choice"] = {"type": "tool", "name": "submit_analysis"}

        response = client.messages.create(**kwargs)
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            # Model answered with text but didn't submit — nudge it once.
            messages.append(
                {
                    "role": "user",
                    "content": "Call submit_analysis now with your final scores.",
                }
            )
            continue

        submit = next((t for t in tool_uses if t.name == "submit_analysis"), None)
        if submit is not None:
            analysis = dict(submit.input)
            analysis["_model"] = model
            analysis["_enrichment"] = enrichment
            log.info(
                f"  Agentic score {ticker}: catalyst="
                f"{analysis.get('catalyst', {}).get('score')} "
                f"value={analysis.get('value', {}).get('score')} "
                f"conf={analysis.get('confidence')}"
            )
            return analysis

        # Otherwise execute the fetch tools and feed results back.
        results = []
        for t in tool_uses:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": t.id,
                    "content": _run_tool(t.name, dict(t.input)),
                }
            )
        messages.append({"role": "user", "content": results})

    raise ScorerUnavailable("agent did not submit within the iteration budget")
