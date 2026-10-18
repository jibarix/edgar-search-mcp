"""Period selection checks for XBRLParser against SEC Company Facts.

Standard library only. Run from the repository root with EDGAR_USER_AGENT set:
    python -X utf8 tests/test_periods.py
Facts are fetched once from data.sec.gov and kept in cache/probe.

Checks, for 22 companies with different fiscal calendars:
  1. annual   - every returned period is a fiscal year end, and revenue and
                net income equal the 52/53-week figure in the latest annual form
  2. quarterly - the four quarters of the latest fiscal year sum to that year,
                within filing rounding (0.05%)
  3. ytd      - the figure at the fiscal year end equals the annual figure
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from edgar_mcp.config.constants import HTTP_HEADERS  # noqa: E402
from edgar_mcp.edgar.xbrl_parser import XBRLParser  # noqa: E402

PROBE = os.path.join(ROOT, "cache", "probe")
TICKERS = ["AAPL", "MSFT", "AN", "KMX", "LAD", "PAG", "GPI", "SAH", "COST", "NKE", "WMT",
           "DG", "HD", "DE", "CSCO", "JPM", "KO", "TSLA", "NVDA", "F", "ORLY", "AZO"]
REVENUE = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
CONCEPTS = REVENUE + ["NetIncomeLoss"]
ANNUAL_FORMS = ("10-K", "10-K/A")
# Quarters are rounded separately in filings; their sum may miss the year by this share.
ROUNDING = 0.0005


def fetch(url, path):
    if not os.path.exists(path):
        if not HTTP_HEADERS["User-Agent"]:
            sys.exit('set EDGAR_USER_AGENT to your name and email first, e.g. "Jane Doe jane@example.com"')
        time.sleep(0.2)
        request = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(request, timeout=60) as response, open(path, "wb") as out:
            out.write(response.read())
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def annual_truth(facts, concept):
    """Fiscal-year figures straight from the raw facts: 52/53-week durations in
    annual forms, the most recently filed value for each year end."""
    best = {}
    for row in facts["facts"].get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD", []):
        if row.get("form") not in ANNUAL_FORMS or not row.get("start"):
            continue
        days = (dt.date.fromisoformat(row["end"]) - dt.date.fromisoformat(row["start"])).days
        if 340 < days < 380 and (row["end"] not in best or row["filed"] >= best[row["end"]][0]):
            best[row["end"]] = (row["filed"], row["val"])
    return {end: val for end, (_, val) in best.items()}


def by_concept(parsed):
    return {m["tag"].split(":")[1]: m["values"] for m in parsed.get("metrics", {}).values()}


def main():
    os.makedirs(PROBE, exist_ok=True)
    tickers = fetch("https://www.sec.gov/files/company_tickers.json", os.path.join(PROBE, "tickers.json"))
    cik = {row["ticker"]: row["cik_str"] for row in tickers.values()}
    parser = XBRLParser()
    problems = 0
    checked = {"annual": 0, "quarterly": 0, "ytd": 0}

    for ticker in TICKERS:
        facts = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik[ticker]:010d}.json",
                      os.path.join(PROBE, f"facts_{cik[ticker]}.json"))
        annual = parser.parse_company_facts(facts, statement_type="IS", period_type="annual", num_periods=5) or {}
        quarterly = parser.parse_company_facts(facts, statement_type="IS", period_type="quarterly", num_periods=8) or {}
        ytd = parser.parse_company_facts(facts, statement_type="IS", period_type="ytd", num_periods=8) or {}
        a, q, y = by_concept(annual), by_concept(quarterly), by_concept(ytd)
        periods = annual.get("periods", [])
        messages, notes = [], []

        all_year_ends = set()
        for concept in CONCEPTS:
            all_year_ends |= set(annual_truth(facts, concept))
        expected_periods = sorted(all_year_ends, reverse=True)[:5]
        if periods != expected_periods:
            messages.append(f"annual periods {periods}, expected {expected_periods}")

        for concept in CONCEPTS:
            truth = annual_truth(facts, concept)
            for end in periods:
                if end not in truth:
                    continue
                got = a.get(concept, {}).get(end)
                checked["annual"] += 1
                if got != truth[end]:
                    messages.append(f"annual {concept} {end}: got {got}, 10-K says {truth[end]}")

            # the latest fiscal year: four quarters sum to the year; ytd equals the year
            if not periods or periods[0] not in truth:
                continue
            fye, year = periods[0], truth[periods[0]]
            quarter_ends = quarterly.get("periods", [])
            if fye in quarter_ends:
                four = quarter_ends[quarter_ends.index(fye):][:4]
                values = [q.get(concept, {}).get(end) for end in four]
                checked["quarterly"] += 1
                if len(four) == 4 and all(v is None for v in values[1:]):
                    notes.append(f"{concept}: tagged for fiscal years only, no quarterly facts")
                elif len(four) < 4 or any(v is None for v in values):
                    messages.append(f"quarterly {concept} FY {fye}: quarters {dict(zip(four, values))}")
                elif abs(sum(values) - year) > ROUNDING * abs(year):
                    messages.append(f"quarterly {concept} FY {fye}: quarters sum {sum(values)}, year {year}")
            else:
                messages.append(f"quarterly periods lack fiscal year end {fye}: {quarter_ends[:5]}")
            checked["ytd"] += 1
            if y.get(concept, {}).get(fye) != year:
                messages.append(f"ytd {concept} {fye}: got {y.get(concept, {}).get(fye)}, year {year}")

        print(f"{ticker:5} annual {periods}  {'OK' if not messages else ''}")
        for message in messages:
            print("    !!", message)
        for note in notes:
            print("    note:", note)
        problems += len(messages)

    print(f"checked: {checked}  problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
