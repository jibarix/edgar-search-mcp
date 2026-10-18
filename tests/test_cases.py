"""Fixed cases for the parser, filing list, document address check and cache.

No network. Run from the repository root:
    python -X utf8 tests/test_cases.py

Each case is one that the earlier code got wrong or accepted silently.
"""
from __future__ import annotations

import os
import pickle
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")

from edgar import company_lookup  # noqa: E402
from edgar.filing_retrieval import FilingRetrieval  # noqa: E402
from edgar.xbrl_parser import XBRLParser  # noqa: E402
from edgar_mcp.server import _document_url, _resolve  # noqa: E402
from utils import sec_client  # noqa: E402
from utils.cache import Cache  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def fact(start, end, val, form, filed, accn=None):
    row = {"end": end, "val": val, "form": form, "accn": accn or f"{form}-{filed}"}
    if start:
        row["start"] = start
    if filed:
        row["filed"] = filed
    return row


# A calendar-year company. Revenue: 2022 = 100; 2023 quarters 25, 35, 30, 30.
REVENUE = [
    fact("2022-01-01", "2022-12-31", 100, "10-K", "2023-02-15"),
    fact("2023-01-01", "2023-03-31", 25, "10-Q", "2023-05-01"),
    fact("2023-04-01", "2023-06-30", 35, "10-Q", "2023-08-01"),
    fact("2023-01-01", "2023-06-30", 60, "10-Q", "2023-08-01"),
    fact("2022-07-01", "2023-06-30", 110, "10-Q", "2023-08-01"),  # trailing twelve months
    fact("2023-07-01", "2023-09-30", 30, "10-Q", "2023-11-01"),
    fact("2023-01-01", "2023-09-30", 90, "10-Q", "2023-11-01"),
    fact("2023-01-01", "2023-12-31", 120, "10-K", "2024-02-15"),
    fact("2023-01-01", "2023-09-30", 95, "10-Q", "2024-11-01"),  # restated a year later
]


def company(revenue):
    return {"entityName": "Test Co", "facts": {"us-gaap": {"Revenues": {"units": {"USD": revenue}}}}}


def revenue_of(parsed):
    metric = next(m for m in parsed["metrics"].values() if m["tag"] == "us-gaap:Revenues")
    return metric["values"], metric["sources"]


def parser_cases():
    parser = XBRLParser()

    values, _ = revenue_of(parser.parse_company_facts(company(REVENUE), "IS", "ytd", 8))
    check("year to date is not the trailing twelve months", values.get("2023-06-30") == 60,
          str(values.get("2023-06-30")))

    # Nothing restated: quarters and year agree, and nothing is flagged.
    first_filed = [f for f in REVENUE if f["val"] != 95]
    parsed = parser.parse_company_facts(company(first_filed), "IS", "quarterly", 8)
    values, sources = revenue_of(parsed)
    q4 = sources["2023-12-31"]
    quarters = [values[p] for p in ("2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31")]
    check("quarters add up to the year when nothing was restated",
          quarters == [25, 35, 30, 30] and sum(quarters) == 120, str(quarters))
    check("derived quarter has its own interval", (q4["start"], q4["end"]) == ("2023-10-01", "2023-12-31"),
          f"{q4['start']} to {q4['end']}")
    check("derived quarter gives both figures",
          q4["derived"] and q4["cumulative"]["val"] == 120 and q4["minus"]["val"] == 90
          and q4["minus"]["filed"] == "2023-11-01" and "warnings" not in q4)
    metric = next(m for m in parsed["metrics"].values() if m["tag"] == "us-gaap:Revenues")
    check("no difference is reported when quarters and year agree",
          "reconciliation" not in metric
          and not any("INCONSISTENT_REPORTING_BASIS" in w for w in parsed["metadata"]["warnings"]))

    # The nine months were restated to 95 after the annual report. One vintage
    # is used throughout, and every place the figures stop agreeing is flagged.
    ytd_values, _ = revenue_of(parser.parse_company_facts(company(REVENUE), "IS", "ytd", 8))
    parsed = parser.parse_company_facts(company(REVENUE), "IS", "quarterly", 8)
    values, sources = revenue_of(parsed)
    q4 = sources["2023-12-31"]
    check("a derived quarter is the difference of the year-to-date figures returned",
          values["2023-12-31"] == ytd_values["2023-12-31"] - ytd_values["2023-09-30"] == 25
          and q4["minus"]["val"] == 95, f"{values['2023-12-31']} from {q4['minus']['val']}")
    check("a restated earlier figure is flagged on the derived quarter",
          any("INCONSISTENT_REPORTING_BASIS" in w for w in q4.get("warnings", [])))
    metric = next(m for m in parsed["metrics"].values() if m["tag"] == "us-gaap:Revenues")
    found = metric.get("reconciliation", {})
    check("quarters that miss the nine months are reported",
          found.get("2023-09-30") == {"year_to_date": 95, "sum_of_quarters": 90, "difference": -5},
          str(found.get("2023-09-30")))
    check("quarters that miss the year are reported",
          found.get("2023-12-31") == {"year_to_date": 120, "sum_of_quarters": 115, "difference": -5},
          str(found.get("2023-12-31")))
    check("the result as a whole carries the warning and its vintage",
          any("INCONSISTENT_REPORTING_BASIS" in w for w in parsed["metadata"]["warnings"])
          and parsed["metadata"]["vintage"].startswith("latest filed"))

    # A first fiscal year on file is anchored by its own annual figures.
    no_prior_year = [f for f in first_filed if f["end"] != "2022-12-31"]
    values, _ = revenue_of(parser.parse_company_facts(company(no_prior_year), "IS", "ytd", 8))
    check("year to date in the first year on file starts where that year's annual figures start",
          values.get("2023-06-30") == 60 and values.get("2023-09-30") == 90, str(values))

    # With no fiscal year on file at all, a lone quarter is not called a year to date.
    lone = [fact("2023-07-01", "2023-09-30", 30, "10-Q", "2023-11-01")]
    parsed = parser.parse_company_facts(company(lone), "IS", "ytd", 8)
    check("a figure is not taken for the year to date without a fiscal year to anchor it",
          parsed["periods"] == [] and parsed["metadata"]["empty_reason"] == "INSUFFICIENT_PERIOD_EVIDENCE"
          and any("2023-09-30" in w for w in parsed["metadata"]["warnings"]),
          str(parsed["metadata"]))
    parsed = parser.parse_company_facts(company(lone), "IS", "quarterly", 8)
    check("thin evidence of the quarter ends is said so",
          parsed["periods"] == ["2023-09-30"]
          and any("INSUFFICIENT_PERIOD_EVIDENCE" in w for w in parsed["metadata"]["warnings"]),
          str(parsed["metadata"]["warnings"]))

    # Five concepts report real quarters; one reports a stub period ending
    # mid-quarter, repeated in six filings. Rows are not evidence: concepts are.
    names = ["Revenues", "NetIncomeLoss", "OperatingIncomeLoss", "GrossProfit", "CostOfRevenue"]
    many = {name: {"units": {"USD": list(REVENUE)}} for name in names}
    many["IncomeTaxExpenseBenefit"] = {"units": {"USD": [
        fact("2023-05-17", "2023-08-15", 7, "10-Q", f"2023-{month:02d}-01", accn=f"stub-{month}")
        for month in range(7, 13)]}}
    parsed = parser.parse_company_facts({"entityName": "Test Co", "facts": {"us-gaap": many}}, "IS", "quarterly", 12)
    check("one concept repeated in many filings does not make a quarter end",
          "2023-08-15" not in parsed["periods"] and "2023-09-30" in parsed["periods"]
          and not any("INSUFFICIENT_PERIOD_EVIDENCE" in w for w in parsed["metadata"]["warnings"]),
          str(parsed["periods"]))

    shifted = [dict(f, start="2023-01-04") if (f["end"], f["val"]) == ("2023-09-30", 90) else f
               for f in REVENUE if f["val"] != 95]
    values, _ = revenue_of(parser.parse_company_facts(company(shifted), "IS", "quarterly", 8))
    check("no quarter is derived from figures with different start dates", values.get("2023-12-31") is None,
          str(values.get("2023-12-31")))

    try:
        parser.parse_company_facts(company(REVENUE), "IS", "annual", 4, as_of="2023-6-30")
        check("a malformed cutoff date is refused", False)
    except ValueError:
        check("a malformed cutoff date is refused", True)

    parsed = parser.parse_company_facts(company(REVENUE), "IS", "annual", 4, as_of="2023-06-30")
    check("cutoff hides the later annual report", parsed["periods"] == ["2022-12-31"], str(parsed["periods"]))

    undated = REVENUE + [fact("2024-01-01", "2024-12-31", 140, "10-K", None, accn="no-date")]
    free = parser.parse_company_facts(company(undated), "IS", "annual", 4)
    cut = parser.parse_company_facts(company(undated), "IS", "annual", 4, as_of="2030-01-01")
    check("a fact with no filing date is left out under a cutoff",
          free["periods"][0] == "2024-12-31" and cut["periods"][0] == "2023-12-31",
          f"{free['periods'][0]} / {cut['periods'][0]}")

    transition = REVENUE + [fact("2024-01-01", "2024-06-30", 50, "10-KT", "2024-08-15")]
    parsed = parser.parse_company_facts(company(transition), "IS", "annual", 4)
    check("a transition period is not a fiscal year and is reported",
          "2024-06-30" not in parsed["periods"] and any("2024-06-30" in w for w in parsed["metadata"]["warnings"]),
          str(parsed["metadata"]["warnings"]))

    euros = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": REVENUE}},
        "NetIncomeLoss": {"units": {"EUR": [fact("2023-01-01", "2023-12-31", 9, "10-K", "2024-02-15")]}},
    }}}
    parsed = parser.parse_company_facts(euros, "IS", "annual", 4)
    check("amounts in another currency are named as skipped",
          parsed["metadata"].get("skipped_units") == {"us-gaap:NetIncomeLoss": ["EUR"]},
          str(parsed["metadata"].get("skipped_units")))

    only_euros = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": {"units": {"EUR": REVENUE}}}}}
    parsed = parser.parse_company_facts(only_euros, "IS", "annual", 4)
    check("a statement wholly in another currency says so instead of looking empty",
          parsed["periods"] == [] and parsed["metadata"]["empty_reason"] == "UNSUPPORTED_UNITS"
          and parsed["metadata"]["skipped_units"] == {"us-gaap:Revenues": ["EUR"]}, str(parsed["metadata"]))
    parsed = parser.parse_company_facts(company(REVENUE), "IS", "annual", 4, as_of="2020-01-01")
    check("a cutoff before the first filing says so",
          parsed["periods"] == [] and parsed["metadata"]["empty_reason"] == "NOTHING_ON_FILE_AT_CUTOFF",
          str(parsed["metadata"]))
    parsed = parser.parse_company_facts(company(REVENUE), "BS", "annual", 4)
    check("a statement with no reported concepts says so",
          parsed["periods"] == [] and parsed["metadata"]["empty_reason"] == "NO_FACTS_FOR_STATEMENT",
          str(parsed["metadata"]))


def lookup_cases():
    feed = {
        "0": {"cik_str": 1, "ticker": "AAA", "title": "Twin Holdings Inc"},
        "1": {"cik_str": 2, "ticker": "BBB", "title": "Twin Holdings Inc."},
        "2": {"cik_str": 3, "ticker": "CCC", "title": "Two Class Corp"},
        "3": {"cik_str": 3, "ticker": "CCD", "title": "Two Class Corp"},
    }
    real = company_lookup.get_json
    company_lookup.get_json = lambda url, *args, **kwargs: (feed, {})
    try:
        kind, matches = company_lookup.find_company("Twin Holdings Inc")
        check("a name shared by two companies identifies neither",
              kind == "shared_name" and sorted(m["cik"] for m in matches) == ["0000000001", "0000000002"],
              f"{kind} {[m['cik'] for m in matches]}")
        kind, matches = company_lookup.find_company("Twin Holdings")
        check("nor does that name reached by adding a suffix", kind == "shared_name", kind)
        kind, matches = company_lookup.find_company("Two Class Corp")
        check("one company under two tickers is still one company",
              kind == "name" and matches[0]["cik"] == "0000000003", kind)
        try:
            _resolve("Twin Holdings Inc")
            check("a shared name is refused with the candidates", False)
        except Exception as e:
            check("a shared name is refused with the candidates",
                  "AMBIGUOUS_COMPANY" in str(e) and "CIK 0000000002" in str(e), str(e)[:90])
    finally:
        company_lookup.get_json = real
        company_lookup.company_cache.delete("company_tickers_v4")


def client_cases():
    # The SEC asked for a two-minute pause: nothing more may be sent, by any thread.
    # A stand-in User-Agent gets past the first check; the pause must stop the request.
    real_agent, sec_client.USER_AGENT = sec_client.USER_AGENT, "test test@example.com"
    sec_client._back_off(120)
    try:
        sec_client.fetch("https://data.sec.gov/submissions/CIK0000000001.json")
        check("no request is sent while the SEC's pause is in force", False)
    except sec_client.SecRateLimited as e:
        check("no request is sent while the SEC's pause is in force", "seconds" in str(e), str(e)[:90])
    finally:
        sec_client._blocked_until = 0.0
        sec_client.USER_AGENT = real_agent

    # Without EDGAR_USER_AGENT nothing is sent at all.
    real_agent, sec_client.USER_AGENT = sec_client.USER_AGENT, ""
    try:
        sec_client.fetch("https://data.sec.gov/submissions/CIK0000000001.json")
        check("no request is sent without a User-Agent", False)
    except sec_client.SecNoUserAgent as e:
        check("no request is sent without a User-Agent", e.code == "NO_USER_AGENT", str(e)[:90])
    finally:
        sec_client.USER_AGENT = real_agent


def filing_cases():
    feed = {
        "accessionNumber": ["0000000001-24-000003", "0000000001-24-000002", "0000000001-23-000001",
                            "0000000001-22-000009"],
        "form": ["10-K/A", "10-K", "10-K", "10-K"],
        "filingDate": ["2024-03-01", "2024-02-15", "2023-02-15", ""],
        "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
        "reportDate": ["2023-12-31", "2023-12-31", "2022-12-31", "2021-12-31"],
    }
    retrieval = FilingRetrieval()
    warnings = []
    rows = retrieval._process_filings_data(feed, "10-K", "2024-12-31", "0000000001", False, warnings)
    check("a filing with no date is left out and reported",
          [r["filing_date"] for r in rows] == ["2024-02-15", "2023-02-15"] and len(warnings) == 1,
          str(warnings))
    rows = retrieval._process_filings_data(feed, "10-K", "2024-12-31", "0000000001", True, [])
    check("amendments are returned when asked for", rows[0]["form"] == "10-K/A", rows[0]["form"])
    check("document address is built from CIK, accession and document",
          rows[1]["url"] == "https://www.sec.gov/Archives/edgar/data/1/000000000124000002/b.htm", rows[1]["url"])


def address_cases():
    good = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
    check("a filing document address is accepted", _document_url(good) == good)
    for label, url in [
        ("path that climbs out of the archive", "https://www.sec.gov/Archives/edgar/data/../../../../outside.htm"),
        ("another host", "https://example.com/Archives/edgar/data/320193/000032019323000106/a.htm"),
        ("host suffix trick", "https://www.sec.gov.example.com/Archives/edgar/data/1/000032019323000106/a.htm"),
        ("plain http", "http://www.sec.gov/Archives/edgar/data/320193/000032019323000106/a.htm"),
        ("query string", good + "?x=1"),
        ("encoded slash", "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/..%2f..%2fa.htm"),
        ("binary document", "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/a.pdf"),
    ]:
        try:
            _document_url(url)
            check(f"refused: {label}", False)
        except ValueError:
            check(f"refused: {label}", True)


class Planted:
    """Unpickling this would run code; the cache must never do so."""
    def __reduce__(self):
        return (problems.append, ("cache executed a planted file",))


def cache_cases():
    cache = Cache("cases")
    cache.set("k", {"a": [1, 2]}, meta={"source_url": "u"})
    entry = cache.get_entry("k")
    check("cache returns what was stored", entry["data"] == {"a": [1, 2]} and entry["meta"]["source_url"] == "u")
    cache.set("old", 1, ttl=-1)
    check("an expired entry is not returned", cache.get("old") is None)
    with open(cache._get_cache_path("planted"), "wb") as handle:
        pickle.dump(Planted(), handle)
    check("a planted pickle file is ignored, not executed", cache.get("planted") is None)
    with open(cache._get_cache_path("k"), "w", encoding="utf-8") as handle:
        handle.write('{"key": "other", "expires_at": 9e12, "data": "swapped"}')
    check("an entry stored under another key is not returned", cache.get("k") is None)
    blocked = Cache("blocked")
    blocked.cache_dir = cache._get_cache_path("k")  # a file, so no directory can be made there
    try:
        blocked.set("k", 1)
        blocked.set("unserialisable", {1, 2})
        check("a cache that cannot be written does not fail the request", True)
    except Exception as e:
        check("a cache that cannot be written does not fail the request", False, repr(e))


if __name__ == "__main__":
    parser_cases()
    lookup_cases()
    client_cases()
    filing_cases()
    address_cases()
    cache_cases()
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
