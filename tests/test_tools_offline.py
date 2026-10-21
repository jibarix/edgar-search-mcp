"""The tools against made-up SEC answers: search, segments, filed statements, filing history,
retries and frozen copies, statement classification and the company index.

No network: every request goes to a stand-in transport, and any request a
case did not expect fails the run. Run from the repository root:
    python -X utf8 tests/test_tools_offline.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
os.environ.pop("EDGAR_FROZEN_DIR", None)
os.environ.pop("EDGAR_RECORD_DIR", None)
os.environ.pop("EDGAR_COMPANY_INDEX", None)

import httpx  # noqa: E402

from edgar_mcp import server  # noqa: E402
from edgar_mcp.config import settings  # noqa: E402
from edgar_mcp.edgar import company_classifier, company_lookup, filed_statements, filing_retrieval  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def unexpected(request):
    problems.append(f"unexpected request to {request.url}")
    raise AssertionError(f"the network was used: {request.url}")


# Every request fails the run unless a case puts its own transport in place.
sec_client._client = httpx.Client(transport=httpx.MockTransport(unexpected))
sec_client.USER_AGENT = "test test@example.com"
sec_client.API_RETRY_DELAY = 0.001
sec_client.RATE_LIMIT_REQUESTS_PER_SECOND = 10_000


def transport(handler):
    sec_client._client = httpx.Client(transport=httpx.MockTransport(handler))


def call(tool, **kwargs):
    """Run a tool through its real wrapper; returns (result, error code)."""
    try:
        return asyncio.run(tool(**kwargs)), None
    except Exception as e:
        text = str(e)
        return None, text.split(":", 1)[0] if ":" in text else type(e).__name__


# ── search_filings ──────────────────────────────────────────────────────────

def hit(n):
    return {"_id": f"0000000001-24-{n:06d}:doc{n}.htm",
            "_source": {"ciks": ["0000000001"], "display_names": ["Test Co (TST) (CIK 0000000001)"],
                        "form": "10-K", "file_type": "10-K", "file_date": "2024-02-01"}}


def page(total, hits, relation="eq"):
    return {"hits": {"total": {"value": total, "relation": relation}, "hits": hits}}


def search_cases():
    real = server.get_json
    try:
        bad = {
            "a total that is a number": {"hits": {"total": 7, "hits": []}},
            "a total that is true": page(True, []),
            "a negative total": page(-1, []),
            "an unknown relation": page(3, [], relation="about"),
            "hits that are null": {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": None}},
            "a hits object that is a list": {"hits": []},
            "a string answer": "error",
            "a hit with no _source": page(1, [{"_id": "x:y.htm"}]),
            "a hit with an unreadable CIK": page(1, [{"_id": "x:y.htm", "_source": {"ciks": ["12a"]}}]),
            "a hit with a name that is not text": page(1, [{"_id": "x:y.htm", "_source": {"display_names": [5]}}]),
        }
        for label, answer in bad.items():
            server.get_json = lambda url, *a, answer=answer, **k: (answer, {})
            _, code = call(server.search_filings, query="x")
            check(f"search: {label} is an upstream failure", code == "UPSTREAM_UNAVAILABLE", code)

        for relation in ("eq", "gte"):
            server.get_json = lambda url, *a, relation=relation, **k: (page(0, [], relation), {})
            result, code = call(server.search_filings, query="x")
            check(f"search: no hits ({relation}) is a valid empty answer",
                  code is None and result["count"] == 0 and result["total_is_exact"] == (relation == "eq"), code)

        # 10,000 hits: continuation runs through the last upstream page and stops at the ceiling.
        asked = []

        def pages(url, *a, **k):
            start = int(url.split("&from=")[1]) if "&from=" in url else 0
            asked.append(start)
            return page(10000, [hit(start + i) for i in range(100)], relation="gte"), {}
        server.get_json = pages
        offsets, offset = [], 9900
        while offset is not None:
            result, code = call(server.search_filings, query="x", offset=offset, limit=25)
            offsets.append(offset)
            offset = result["next_offset"] if result else None
        check("search: offsets 9900, 9925, 9950, 9975 are followed to the end",
              offsets == [9900, 9925, 9950, 9975] and set(asked) == {9900}, f"{offsets} {sorted(set(asked))}")
        result, code = call(server.search_filings, query="x", offset=9999, limit=25)
        check("search: offset 9999 is accepted", code is None and result["count"] == 1, code)
        _, code = call(server.search_filings, query="x", offset=10000)
        check("search: offset 10000 is refused", code == "INVALID_INPUT", code)
        asked.clear()
        result, _ = call(server.search_filings, query="x", offset=90, limit=25)
        check("search: a page boundary gives a short page and the next offset",
              result["count"] == 10 and result["next_offset"] == 100 and asked == [0], f"{result['count']}")

        server.get_json = lambda url, *a, **k: (page(30, [hit(i) for i in range(30)]), {})
        result, _ = call(server.search_filings, query="x", limit=25)
        result2, _ = call(server.search_filings, query="x", offset=25, limit=25)
        check("search: continuation stops at the actual total",
              result["next_offset"] == 25 and result2["next_offset"] is None and result2["count"] == 5)
    finally:
        server.get_json = real


# ── sec_client: retrieval time, frozen copies, pauses ──────────────────────

def client_cases():
    url = "https://data.sec.gov/submissions/CIK0000000001.json"
    record = tempfile.mkdtemp(prefix="edgar_record_test_")
    transport(lambda r: httpx.Response(200, content=b'{"a": 1}', headers={"Content-Type": "application/json"}))
    sec_client.RECORD_DIR = record
    try:
        live = sec_client.fetch_response(url)
    finally:
        sec_client.RECORD_DIR = None
    about_path = sec_client._copy_paths(record, url)[1]
    about = json.load(open(about_path, encoding="utf-8"))
    check("a recording carries the same retrieval time as the live answer",
          about["retrieved_at"] == live.retrieved_at and not live.frozen)

    sec_client._client = httpx.Client(transport=httpx.MockTransport(unexpected))
    sec_client.FROZEN_DIR = record
    try:
        replay = sec_client.fetch_response(url)
        check("a replay gives the recorded time, body and frozen flag",
              replay.retrieved_at == live.retrieved_at and replay.body == live.body and replay.frozen)
        data, meta = sec_client.get_json(url)
        check("get_json says the answer was frozen", meta["frozen"] and meta["retrieved_at"] == live.retrieved_at)
        try:
            sec_client.fetch_response("https://data.sec.gov/submissions/CIK0000000002.json")
            check("an address with no frozen copy is refused", False)
        except sec_client.SecNoFrozenCopy:
            check("an address with no frozen copy is refused", True)
        try:
            sec_client.fetch_response(url, max_bytes=3)
            check("an oversized frozen body is refused before it is read whole", False)
        except sec_client.SecUnsupported:
            check("an oversized frozen body is refused before it is read whole", True)
        for label, broken in (("not an object", "[1]"), ("no sha256", json.dumps({**about, "sha256": None})),
                              ("not JSON", "{")):
            with open(about_path, "w", encoding="utf-8") as f:
                f.write(broken)
            try:
                sec_client.fetch_response(url)
                check(f"a frozen description that is {label} is refused", False)
            except sec_client.SecError as e:
                check(f"a frozen description that is {label} is refused",
                      e.code == "UPSTREAM_UNAVAILABLE", type(e).__name__)
        with open(about_path, "w", encoding="utf-8") as f:
            json.dump({**about, "sha256": hashlib.sha256(b"other").hexdigest()}, f)
        try:
            sec_client.fetch_response(url)
            check("a changed frozen copy is refused", False)
        except sec_client.SecError as e:
            check("a changed frozen copy is refused", e.code == "UPSTREAM_UNAVAILABLE")
    finally:
        sec_client.FROZEN_DIR = None

    # The disk cache keeps the time of the answer it holds.
    cache = filing_retrieval.Cache("time_test")
    transport(lambda r: httpx.Response(200, content=b'{"b": 2}'))
    _, first = sec_client.get_json(url, cache, "k")
    _, second = sec_client.get_json(url, cache, "k")
    check("a cache hit keeps the first retrieval time",
          second["cache_hit"] and second["retrieved_at"] == first["retrieved_at"])

    # A 503 with a long Retry-After is a pause: RATE_LIMITED, and later requests are held too.
    clock = {"now": 1000.0}
    real_monotonic = sec_client.time.monotonic
    sec_client.time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=lambda s: None)
    try:
        transport(lambda r: httpx.Response(503, headers={"Retry-After": "120"}))
        try:
            sec_client.fetch_response(url)
            check("a 503 asking for two minutes is RATE_LIMITED", False)
        except sec_client.SecError as e:
            check("a 503 asking for two minutes is RATE_LIMITED",
                  e.code == "RATE_LIMITED" and "503" in str(e), str(e)[:80])
        sec_client._client = httpx.Client(transport=httpx.MockTransport(unexpected))
        try:
            sec_client.fetch_response(url)
            check("no request is sent while that pause lasts", False)
        except sec_client.SecRateLimited:
            check("no request is sent while that pause lasts", True)
        clock["now"] += 121
        transport(lambda r: httpx.Response(200, content=b"{}"))
        check("requests go out again once the pause is over", sec_client.fetch(url)[0] == b"{}")
        transport(lambda r: httpx.Response(503))
        try:
            sec_client.fetch_response(url)
            check("a 503 without Retry-After, retried to the end, is UPSTREAM_UNAVAILABLE", False)
        except sec_client.SecError as e:
            check("a 503 without Retry-After, retried to the end, is UPSTREAM_UNAVAILABLE",
                  e.code == "UPSTREAM_UNAVAILABLE", e.code)
    finally:
        sec_client.time = __import__("time")
        sec_client._blocked_until = 0.0
        del real_monotonic
        sec_client._client = httpx.Client(transport=httpx.MockTransport(unexpected))


def lookup_cases():
    feed = {"0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"}}
    real = company_lookup.get_json
    company_lookup.company_cache.delete("company_tickers_v5")
    company_lookup.get_json = lambda url, *a, **k: (feed, {"retrieved_at": "2026-01-01T00:00:00Z", "frozen": False})
    try:
        first, _ = call(server.lookup_company, query="AAA")
        company_lookup.get_json = lambda url, *a, **k: (feed, {"retrieved_at": "2026-02-02T00:00:00Z"})
        second, _ = call(server.lookup_company, query="Alpha")
        check("lookup_company names its source and when it left the SEC",
              first["retrieved_at"] == "2026-01-01T00:00:00Z" and first["source_url"].endswith("company_tickers.json"))
        check("a cached lookup keeps the download time, not a new one",
              second["retrieved_at"] == "2026-01-01T00:00:00Z" and second["returned_at"] >= first["returned_at"])
    finally:
        company_lookup.get_json = real
        company_lookup.company_cache.delete("company_tickers_v5")


# ── filing history and get_segment_revenue ─────────────────────────────────

SUBMISSIONS = {"filings": {"recent": {"accessionNumber": [], "form": [], "filingDate": [], "primaryDocument": [],
                                      "reportDate": []},
                           "files": [{"name": "CIK0000000001-submissions-001.json", "filingTo": "2020-12-31"},
                                     {"name": "CIK0000000001-submissions-002.json", "filingTo": "2015-12-31"}]}}


def history(accession, form, date):
    return {"accessionNumber": [accession], "form": [form], "filingDate": [date], "primaryDocument": ["a.htm"],
            "reportDate": [date]}


def history_cases():
    real = filing_retrieval.get_json
    retrieval = filing_retrieval.FilingRetrieval()

    def answers(table):
        def get(url, *a, **k):
            name = url.rsplit("/", 1)[1]
            got = table.get(name, SUBMISSIONS if name.startswith("CIK0000000001.json") else None)
            if isinstance(got, Exception):
                raise got
            return got, {"source_url": url}
        return get
    try:
        newer_fails = {"CIK0000000001-submissions-001.json": sec_client.SecNoFrozenCopy("no copy"),
                       "CIK0000000001-submissions-002.json": history("0000000001-15-000001", "10-K", "2015-03-01")}
        filing_retrieval.get_json = answers(newer_fails)
        rows, warnings, _ = retrieval.get_filing_metadata("1", "10-K", limit=1)
        check("history: a list goes on past an unreadable file, with a warning",
              len(rows) == 1 and len(warnings) == 1 and "NO_FROZEN_COPY" in warnings[0], str(warnings))
        try:
            retrieval.get_filing_metadata("1", "10-K", limit=1, strict=True)
            check("history: strict raises when a newer file cannot be read", False)
        except sec_client.SecNoFrozenCopy:
            check("history: strict raises when a newer file cannot be read", True)

        filing_retrieval.get_json = answers({
            "CIK0000000001-submissions-001.json": history("0000000001-20-000001", "10-K", "2020-03-01"),
            "CIK0000000001-submissions-002.json": sec_client.SecError("HTTP 503")})
        rows, _, _ = retrieval.get_filing_metadata("1", "10-K", limit=1, strict=True)
        check("history: files are read newest first and stop once enough is found",
              rows and rows[0]["filing_date"] == "2020-03-01")

        # Through the tool: the error code survives, and no 20-F is tried in place of a 10-K.
        real_resolve = server._resolve
        server._resolve = lambda s: {"cik": "0000000001", "matched_on": "cik"}
        try:
            filing_retrieval.get_json = answers(newer_fails)
            _, code = call(server.get_segment_revenue, cik_or_ticker="1")
            check("segments: an unreadable history file is NO_FROZEN_COPY, not NOT_FOUND", code == "NO_FROZEN_COPY", code)
            filing_retrieval.get_json = answers({
                "CIK0000000001-submissions-001.json": sec_client.SecError("HTTP 503"),
                "CIK0000000001-submissions-002.json": history("0000000001-15-000002", "20-F", "2015-03-01")})
            _, code = call(server.get_segment_revenue, cik_or_ticker="1")
            check("segments: a failed 10-K search does not fall back to a 20-F", code == "UPSTREAM_UNAVAILABLE", code)
            filing_retrieval.get_json = answers({
                "CIK0000000001-submissions-001.json": history("0000000001-20-000003", "8-K", "2020-03-01"),
                "CIK0000000001-submissions-002.json": history("0000000001-15-000003", "10-Q", "2015-03-01")})
            _, code = call(server.get_segment_revenue, cik_or_ticker="1")
            check("segments: a complete history with no annual report is NOT_FOUND", code == "NOT_FOUND", code)
        finally:
            server._resolve = real_resolve
    finally:
        filing_retrieval.get_json = real
        filing_retrieval.filing_cache.clear() if hasattr(filing_retrieval.filing_cache, "clear") else None


US_GAAP = "http://fasb.org/us-gaap/2024"
SRT = "http://fasb.org/srt/2024"


def instance(facts, units=None, iso_prefix="iso4217", extra_ns=""):
    """A small XBRL instance. `facts` are (concept, context, unit, value); a
    concept may be "custom:Revenues". Contexts: FY (2025), FY24S (other
    start), and FY with members US, CA (geography), P (product)."""
    units = units if units is not None else {
        "USD": f"<xbrli:measure>{iso_prefix}:USD</xbrli:measure>",
        "U_usd": f"<xbrli:measure>{iso_prefix}:USD</xbrli:measure>",
        "EUR": f"<xbrli:measure>{iso_prefix}:EUR</xbrli:measure>",
        "USDPerShare": (f"<xbrli:divide><xbrli:unitNumerator><xbrli:measure>{iso_prefix}:USD</xbrli:measure>"
                        "</xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure>"
                        "</xbrli:unitDenominator></xbrli:divide>")}

    def ctx(cid, start, members=()):
        seg = "".join(f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>' for d, m in members)
        seg = f"<xbrli:segment>{seg}</xbrli:segment>" if seg else ""
        return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>{seg}'
                f"</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
                "<xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>")
    geo = "srt:StatementGeographicalAxis"
    contexts = [ctx("FY", "2025-01-01"), ctx("FY24S", "2024-12-29"),
                ctx("US", "2025-01-01", [(geo, "country:US")]), ctx("CA", "2025-01-01", [(geo, "country:CA")]),
                ctx("MX", "2025-01-01", [(geo, "country:MX")]),
                ctx("P", "2025-01-01", [("srt:ProductOrServiceAxis", "t:ProductMember")])]
    body = "".join(f'<{c if ":" in c else "us-gaap:" + c} contextRef="{x}" unitRef="{u}" decimals="-6">{v}'
                   f'</{c if ":" in c else "us-gaap:" + c}>' for c, x, u, v in facts)
    unit_xml = "".join(f'<xbrli:unit id="{k}">{v}</xbrli:unit>' for k, v in units.items())
    return (f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
            f'xmlns:us-gaap="{US_GAAP}" xmlns:srt="{SRT}" xmlns:{iso_prefix}="http://www.xbrl.org/2003/iso4217" '
            f'xmlns:country="http://xbrl.sec.gov/country/2024" xmlns:t="http://t.example/2025" '
            f'xmlns:custom="http://custom.example/2025" {extra_ns}>'
            f"{''.join(contexts)}{unit_xml}{body}</xbrli:xbrl>").encode("utf-8")


def segments(raw):
    """get_segment_revenue on one made-up instance; returns (result, code)."""
    real = (server._resolve, server._filings.get_filing_metadata, server.fetch_response)
    server._resolve = lambda s: {"cik": "0000000001", "matched_on": "cik"}
    server._filings.get_filing_metadata = lambda *a, **k: ([{
        "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/t-20251231.htm",
        "form": "10-K", "accession_number": "0000000001-26-000001", "filing_date": "2026-02-01"}], [], {})
    server.fetch_response = lambda url, **k: sec_client.Response(raw, "application/xml", "2026-02-02T00:00:00Z")
    try:
        return call(server.get_segment_revenue, cik_or_ticker="1")
    finally:
        server._resolve, server._filings.get_filing_metadata, server.fetch_response = real


def geo(result):
    return {r["member"].split(":")[-1]: r["value"] for r in result["axes"]["geography"]}


def segment_cases():
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "CA", "U_usd", 20)], iso_prefix="iso"))
    check("segments: two unit ids for USD, under another prefix, are one currency",
          r["unit"] == "USD" and geo(r) == {"US": 70, "CA": 20} and not r["skipped"], str(r and r["skipped"]))
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "CA", "EUR", 18)]))
    check("segments: a member in another currency is left out and named",
          geo(r) == {"US": 70} and [s["reason"] for s in r["skipped"]] == ["in EUR, not USD"], str(r["skipped"]))
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "US", "EUR", 63)]))
    check("segments: a member given in the right currency is not listed as skipped",
          geo(r) == {"US": 70} and not r["skipped"], str(r["skipped"]))
    r, _ = segments(instance([("Revenues", "FY", "EUR", 50), ("Revenues", "US", "EUR", 30)]))
    check("segments: a report in EUR is read in EUR", r["unit"] == "EUR" and geo(r) == {"US": 30})
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "NOPE", 70)]))
    check("segments: an undefined unit is named as the reason",
          not geo(r) and "not defined" in r["skipped"][0]["reason"], str(r["skipped"]))
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USDPerShare", 1)]))
    check("segments: a divided unit is not a currency", "not one currency" in r["skipped"][0]["reason"])
    _, code = segments(instance([("Revenues", "US", "USD", 70), ("Revenues", "CA", "EUR", 20)]))
    check("segments: two currencies and no total are refused", code == "UNSUPPORTED_DOCUMENT", code)
    r, _ = segments(instance([("Revenues", "US", "USD", 70), ("Revenues", "CA", "USD", 20)]))
    check("segments: one currency and no total is read", r["unit"] == "USD" and r["total_revenue"] is None)
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "FY24S", "USD", 101)]))
    check("segments: a figure for another period is not mixed in",
          r["total_revenue"]["value"] == 100 and r["fiscal_year"]["start"] == "2025-01-01")
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("RevenueFromContractWithCustomerExcludingAssessedTax", "CA", "USD", 20),
                              ("RevenueFromContractWithCustomerExcludingAssessedTax", "MX", "USD", 10),
                              ("Revenues", "MX", "USD", 11)]))
    check("segments: the concept covering most members is read first, and both are named",
          geo(r) == {"US": 70, "CA": 20, "MX": 11} or geo(r) == {"US": 70, "CA": 20, "MX": 10},
          f"{geo(r)} {r['concepts']['geography']}")
    check("segments: an axis read under two concepts says so", len(r["concepts"]["geography"]) == 2)
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("custom:Revenues", "US", "USD", 70)]))
    check("segments: a company's own concept named Revenues is not read", not geo(r))
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "CA", "USD", 20), ("Revenues", "MX", "USD", 10)]))
    check("segments: a dominant member (70/20/10) is kept, not taken for a total",
          geo(r) == {"US": 70, "CA": 20, "MX": 10})
    r, _ = segments(instance([("Revenues", "FY", "USD", 100), ("Revenues", "US", "USD", 70),
                              ("Revenues", "CA", "USD", -5)]))
    check("segments: a negative member stays", geo(r).get("CA") == -5)
    check("segments: a live answer says it was not frozen", r["frozen"] is False)


# ── get_filed_statements ────────────────────────────────────────────────────

def filed(embedded=False, inline=True, revenue=100, summary_menu=True, calc11=False, prohibit=False,
          duplicate=None, member_total=False, header="20260702060102", cash_flow=None, opening_2024=7,
          missing_child=False, lease_list=None):
    """A made-up filing folder: name -> bytes. Statements: an income statement
    with an own element that adds into costs, a balance sheet with a line
    tagged only under a member, and a parenthetical page. `cash_flow` adds a
    cash flow statement whose opening and closing balance is that element
    ("us-gaap_Cash", on the balance sheet too, or one of restricted cash);
    `opening_2024` is its balance at the end of 2023. `lease_list` is the text of
    the extensible list naming the lines operating leases are in, at the end of 2025."""
    ns = 'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"'
    gaap = "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd"
    roles = {"IS": "http://t.example/role/Income", "BS": "http://t.example/role/Balance",
             "BSP": "http://t.example/role/BalanceParenthetical", "N1": "http://t.example/role/Policies"}
    titles = {"IS": "0000002 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS",
              "BS": "0000003 - Statement - CONSOLIDATED BALANCE SHEETS",
              "BSP": "0000004 - Statement - CONSOLIDATED BALANCE SHEETS (Parenthetical)",
              "N1": "0000005 - Disclosure - Accounting Policies"}
    if cash_flow:
        roles["CF"] = "http://t.example/role/CashFlows"
        titles["CF"] = "0000006 - Statement - CONSOLIDATED STATEMENTS OF CASH FLOWS"
    change = "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect"
    fx = "us-gaap_EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
    period_label = "http://www.xbrl.org/2003/role/period"

    def loc(element):
        own = element.startswith("t_")
        return (f'<link:loc xlink:type="locator" xlink:href="{"t-20251231.xsd" if own else gaap}#{element}" '
                f'xlink:label="{element}"/>')

    def link(kind, role, arcs, arcrole):
        elements = sorted({e for a in arcs for e in a[:2]})
        body = "".join(loc(e) for e in elements)
        for a in arcs:
            parent, child, attr = a[0], a[1], a[2] if len(a) > 2 else ""
            body += (f'<link:{kind}Arc xlink:type="arc" xlink:arcrole="{arcrole}" xlink:from="{parent}" '
                     f'xlink:to="{child}" {attr}/>')
        return f'<link:{kind}Link xlink:type="extended" xlink:role="{role}">{body}</link:{kind}Link>'

    parent_child = "http://www.xbrl.org/2003/arcrole/parent-child"
    pre = (link("presentation", roles["IS"], [
                ("us-gaap_IncomeStatementAbstract", "us-gaap_Revenues", 'order="1"'),
                ("us-gaap_IncomeStatementAbstract", "us-gaap_CostsAndExpensesAbstract", 'order="2"'),
                ("us-gaap_CostsAndExpensesAbstract", "us-gaap_CostOfRevenue", 'order="1"'),
                ("us-gaap_CostsAndExpensesAbstract", "t_FulfillmentExpense", 'order="2"'),
                ("us-gaap_CostsAndExpensesAbstract", "us-gaap_CostsAndExpenses",
                 'order="3" preferredLabel="http://www.xbrl.org/2003/role/totalLabel"'),
                ("us-gaap_IncomeStatementAbstract", "us-gaap_OperatingIncomeLoss", 'order="3"')], parent_child)
           + link("presentation", roles["BS"], [
                ("us-gaap_StatementOfFinancialPositionAbstract", "us-gaap_StatementTable", 'order="1"'),
                ("us-gaap_StatementTable", "us-gaap_PropertyPlantAndEquipmentByTypeAxis", 'order="1"'),
                ("us-gaap_PropertyPlantAndEquipmentByTypeAxis", "t_VehiclesMember", 'order="1"'),
                ("us-gaap_StatementTable", "us-gaap_StatementLineItems", 'order="2"'),
                ("us-gaap_StatementLineItems", "us-gaap_Cash", 'order="1"'),
                ("us-gaap_StatementLineItems", "t_LeasedAssetsNet", 'order="2"'),
                ("us-gaap_StatementLineItems", "us-gaap_Assets", 'order="3"')], parent_child)
           + link("presentation", roles["BSP"], [
                ("us-gaap_StatementOfFinancialPositionAbstract", "us-gaap_Cash", 'order="1"')], parent_child)
           + link("presentation", roles["N1"], [
                ("us-gaap_AccountingPoliciesAbstract", "us-gaap_Revenues", 'order="1"')], parent_child))
    if cash_flow:
        # Opening and closing cash: two arcs to one element under one parent, told apart by their labels.
        pre += link("presentation", roles["CF"], [
            ("us-gaap_StatementOfCashFlowsAbstract", "us-gaap_NetCashProvidedByUsedInOperatingActivities", 'order="1"'),
            ("us-gaap_StatementOfCashFlowsAbstract", fx, 'order="2"'),
            ("us-gaap_StatementOfCashFlowsAbstract", change, 'order="3" preferredLabel="http://www.xbrl.org/2003/role/totalLabel"'),
            ("us-gaap_StatementOfCashFlowsAbstract", cash_flow, f'order="4" preferredLabel="{period_label}StartLabel"'),
            ("us-gaap_StatementOfCashFlowsAbstract", cash_flow, f'order="5" preferredLabel="{period_label}EndLabel"')],
            parent_child)
    summation = ("https://xbrl.org/2023/arcrole/summation-item" if calc11
                 else "http://www.xbrl.org/2003/arcrole/summation-item")
    is_arcs = [("us-gaap_CostsAndExpenses", "us-gaap_CostOfRevenue", 'weight="1" order="1"'),
               ("us-gaap_CostsAndExpenses", "t_FulfillmentExpense", 'weight="1" order="2"'),
               ("us-gaap_OperatingIncomeLoss", "us-gaap_Revenues", 'weight="1" order="1"'),
               ("us-gaap_OperatingIncomeLoss", "us-gaap_CostsAndExpenses", 'weight="-1" order="2"')]
    if prohibit:
        # A wrong arc, then the same arc prohibited at a higher priority: no relationship is left.
        is_arcs += [("us-gaap_OperatingIncomeLoss", "us-gaap_Cash", 'weight="1" order="3"'),
                    ("us-gaap_OperatingIncomeLoss", "us-gaap_Cash",
                     'weight="1" order="3" use="prohibited" priority="1"')]
    cal = (link("calculation", roles["IS"], is_arcs, summation)
           + link("calculation", roles["BS"], [("us-gaap_Assets", "us-gaap_Cash", 'weight="1" order="1"'),
                                               ("us-gaap_Assets", "t_LeasedAssetsNet", 'weight="1" order="2"')],
                  summation))
    if cash_flow:
        cal += link("calculation", roles["CF"], [
            (change, "us-gaap_NetCashProvidedByUsedInOperatingActivities", 'weight="1" order="1"'),
            (change, fx, 'weight="1" order="2"')], summation)
    labels = {"us-gaap_Revenues": "Net sales", "us-gaap_CostOfRevenue": "Cost of sales",
              "t_FulfillmentExpense": "Fulfillment", "us-gaap_CostsAndExpenses": "Costs and expenses",
              "us-gaap_OperatingIncomeLoss": "Operating income", "us-gaap_Cash": "Cash",
              "t_LeasedAssetsNet": "Vehicles, net", "us-gaap_Assets": "Total assets"}
    concept_label = "http://www.xbrl.org/2003/arcrole/concept-label"

    def label(element, key, role, text):
        return (f'<link:label xlink:type="resource" xlink:label="{key}" xml:lang="en-US" '
                f'xlink:role="http://www.xbrl.org/2003/role/{role}">{text}</link:label>'
                f'<link:labelArc xlink:type="arc" xlink:arcrole="{concept_label}" '
                f'xlink:from="{element}" xlink:to="{key}"/>')
    lab_body = "".join(loc(e) + label(e, f"l{i}", "label", t) for i, (e, t) in enumerate(labels.items()))
    lab_body += label("us-gaap_CostsAndExpenses", "l1t", "totalLabel", "Total costs and expenses")
    lab_body += label("t_FulfillmentExpense", "l2d", "documentation", "Costs of fulfillment centers.")
    if cash_flow:
        lab_body += (loc(cash_flow) if cash_flow != "us-gaap_Cash" else "")             + label(cash_flow, "lcs", "periodStartLabel", "Cash, beginning of year")             + label(cash_flow, "lce", "periodEndLabel", "Cash, end of year")
    lab = (f'<link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">'
           f'{lab_body}</link:labelLink>')
    definition = link("definition", roles["IS"], [("us-gaap_CostsAndExpenses", "t_FulfillmentExpense")],
                      "http://www.xbrl.org/2003/arcrole/general-special")

    def linkbase(body):
        return f'<link:linkbase {ns}>{body}</link:linkbase>'.encode()

    role_types = "".join(f'<link:roleType roleURI="{roles[k]}" id="{k}"><link:definition>{t}</link:definition>'
                         f'<link:usedOn>link:presentationLink</link:usedOn></link:roleType>'
                         for k, t in titles.items())
    xbrli_ns = 'xmlns:xbrli="http://www.xbrl.org/2003/instance"'
    elements = ('<xs:element id="t_FulfillmentExpense" name="FulfillmentExpense" type="xbrli:monetaryItemType" '
                'substitutionGroup="xbrli:item" abstract="false" xbrli:periodType="duration" xbrli:balance="debit"/>'
                '<xs:element id="t_LeasedAssetsNet" name="LeasedAssetsNet" type="xbrli:monetaryItemType" '
                'substitutionGroup="xbrli:item" abstract="false" xbrli:periodType="instant" xbrli:balance="debit"/>'
                '<xs:element id="t_VehiclesMember" name="VehiclesMember" type="dtr-types:domainItemType" '
                'substitutionGroup="xbrli:item" abstract="true" xbrli:periodType="duration"/>')
    if embedded:
        inside = (f"<xs:annotation><xs:appinfo>{role_types}<link:linkbase>{pre}</link:linkbase>"
                  f"<link:linkbase>{cal}</link:linkbase><link:linkbase>{lab}</link:linkbase>"
                  f"<link:linkbase>{definition}</link:linkbase></xs:appinfo></xs:annotation>")
    else:
        refs = "".join(f'<link:linkbaseRef xlink:type="simple" '
                       f'xlink:role="http://www.xbrl.org/2003/role/{k}LinkbaseRef" '
                       f'xlink:href="t-20251231_{s}.xml"/>' for k, s in
                       (("presentation", "pre"), ("calculation", "cal"), ("label", "lab"), ("definition", "def")))
        inside = f"<xs:annotation><xs:appinfo>{refs}{role_types}</xs:appinfo></xs:annotation>"
    xsd = (f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" {ns} {xbrli_ns} '
           f'targetNamespace="http://t.example/2025">{inside}{elements}</xs:schema>').encode()

    def ctx(cid, period, members=()):
        seg = "".join(f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>' for d, m in members)
        seg = f"<xbrli:segment>{seg}</xbrli:segment>" if seg else ""
        when = (f"<xbrli:startDate>{period[0]}</xbrli:startDate><xbrli:endDate>{period[1]}</xbrli:endDate>"
                if isinstance(period, tuple) else f"<xbrli:instant>{period}</xbrli:instant>")
        return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
                f"0000000001</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{when}</xbrli:period>"
                f"</xbrli:context>")
    veh = [("us-gaap:PropertyPlantAndEquipmentByTypeAxis", "t:VehiclesMember")]
    contexts = (ctx("D25", ("2025-01-01", "2025-12-31")) + ctx("D24", ("2024-01-01", "2024-12-31"))
                + ctx("I25", "2025-12-31") + ctx("I24", "2024-12-31") + ctx("V25", "2025-12-31", veh)
                + ctx("V24", "2024-12-31", veh))
    facts = [("us-gaap:Revenues", "D25", revenue), ("us-gaap:Revenues", "D24", 90),
             ("us-gaap:CostOfRevenue", "D25", 50), ("us-gaap:CostOfRevenue", "D24", 45),
             ("t:FulfillmentExpense", "D25", 20), ("t:FulfillmentExpense", "D24", 15),
             ("us-gaap:CostsAndExpenses", "D25", 70), ("us-gaap:CostsAndExpenses", "D24", 60),
             ("us-gaap:OperatingIncomeLoss", "D25", 30), ("us-gaap:OperatingIncomeLoss", "D24", 30),
             ("us-gaap:Cash", "I25", 10), ("us-gaap:Cash", "I24", 8),
             ("t:LeasedAssetsNet", "V25", 5), ("t:LeasedAssetsNet", "V24", 4),
             ("us-gaap:Assets", "I25", 15), ("us-gaap:Assets", "I24", 12)]
    if member_total:
        facts.append(("us-gaap:Assets", "V25", 5))
    if missing_child:
        # Fulfillment untagged for 2025, and costs filed as cost of sales alone: the sum agrees, the lines are not all there.
        facts = [f for f in facts if f[:2] != ("t:FulfillmentExpense", "D25")]
        facts = [(c, x, 50 if (c, x) == ("us-gaap:CostsAndExpenses", "D25") else
                  50 if (c, x) == ("us-gaap:OperatingIncomeLoss", "D25") else v) for c, x, v in facts]
    if cash_flow:
        # Operating cash 1 and 2, exchange rates +1 and -1: 8 + 2 = 10 in 2025, 7 + 1 = 8 in 2024.
        cash = cash_flow.replace("_", ":", 1)
        facts += [("us-gaap:NetCashProvidedByUsedInOperatingActivities", "D25", 1),
                  ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "D24", 2),
                  (fx.replace("_", ":", 1), "D25", 1), (fx.replace("_", ":", 1), "D24", -1),
                  (change.replace("_", ":", 1), "D25", 2), (change.replace("_", ":", 1), "D24", 1),
                  (cash, "I23", opening_2024)]
        if cash != "us-gaap:Cash":
            facts += [(cash, "I25", 10), (cash, "I24", 8)]
        contexts += ctx("I23", "2023-12-31")
    if duplicate is not None:
        facts.append(("us-gaap:Revenues", "D25", duplicate))
    body = "".join(f'<{c} contextRef="{x}" unitRef="usd" decimals="-6">{v * 1_000_000}</{c}>' for c, x, v in facts)
    body += ('<dei:DocumentType contextRef="D25">10-K</dei:DocumentType>'
             '<dei:DocumentPeriodEndDate contextRef="D25">2025-12-31</dei:DocumentPeriodEndDate>'
             '<dei:Security12bTitle contextRef="S1">Common Stock</dei:Security12bTitle>'
             '<dei:TradingSymbol contextRef="S1">TT</dei:TradingSymbol>'
             '<dei:Security12bTitle contextRef="S2">Mortgage Bonds due 2066</dei:Security12bTitle>'
             '<dei:TradingSymbol contextRef="S2">TTB</dei:TradingSymbol>'
             '<dei:EntityCentralIndexKey contextRef="E2">0000000002</dei:EntityCentralIndexKey>'
             '<dei:EntityCommonStockSharesOutstanding contextRef="SA" unitRef="shares" decimals="INF">700</dei:EntityCommonStockSharesOutstanding>'
             '<dei:EntityCommonStockSharesOutstanding contextRef="SB" unitRef="shares" decimals="INF">300</dei:EntityCommonStockSharesOutstanding>')
    if lease_list:
        tag = "us-gaap:OperatingLeaseLiabilityNoncurrentStatementOfFinancialPositionExtensibleList"
        body += f'<{tag} contextRef="I25">{lease_list}</{tag}>' * 2       # tagged twice, as a note and a table do
    contexts += (ctx("S1", ("2025-01-01", "2025-12-31"), [("us-gaap:StatementClassOfStockAxis", "us-gaap:CommonStockMember")])
                 + ctx("S2", ("2025-01-01", "2025-12-31"), [("dei:EntityAxis", "t:SubMember"),
                                                            ("us-gaap:StatementClassOfStockAxis", "t:BondsMember")])
                 + ctx("E2", ("2025-01-01", "2025-12-31"), [("dei:EntityAxis", "t:SubMember")])
                 + ctx("SA", "2026-02-01", [("us-gaap:StatementClassOfStockAxis", "t:ClassAMember")])
                 + ctx("SB", "2026-02-01", [("us-gaap:StatementClassOfStockAxis", "t:ClassBMember")]))
    instance_xml = (f'<xbrli:xbrl {xbrli_ns} xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
                    f'xmlns:us-gaap="{US_GAAP}" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
                    'xmlns:t="http://t.example/2025" xmlns:dei="http://xbrl.sec.gov/dei/2024">'
                    f'{contexts}<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
                    '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
                    f"{body}</xbrli:xbrl>").encode()
    instance_name = "t-20251231_htm.xml" if inline else "t-20251231.xml"
    listed = (['<File doctype="10-K" original="t-20251231.htm">t-20251231.htm</File>'] if inline
              else ["<File>t-20251231.xml</File>"]) + ["<File>t-20251231.xsd</File>"]
    if not embedded:
        listed += [f"<File>t-20251231_{s}.xml</File>" for s in ("cal", "def", "lab", "pre")]
    reports = "".join(
        f"<Report><Role>{roles[k]}</Role><HtmlFileName>R{i}.htm</HtmlFileName>"
        + (f"<MenuCategory>{'Statements' if k != 'N1' else 'Policies'}</MenuCategory>" if summary_menu else "")
        + "</Report>" for i, k in enumerate(roles, 2))
    summary = (f"<FilingSummary><MyReports>{reports}</MyReports>"
               f"<InputFiles>{''.join(listed)}</InputFiles></FilingSummary>")
    folder = {"FilingSummary.xml": summary.encode(), "t-20251231.xsd": xsd, instance_name: instance_xml,
              "0000000001-26-000001-index-headers.html":
                  f"<SEC-HEADER><ACCEPTANCE-DATETIME>{header}</SEC-HEADER>".encode()}
    if not embedded:
        folder.update({"t-20251231_pre.xml": linkbase(pre), "t-20251231_cal.xml": linkbase(cal),
                       "t-20251231_lab.xml": linkbase(lab), "t-20251231_def.xml": linkbase(definition)})
    return folder


def statements(folder, rows=None, **kwargs):
    """get_filed_statements on one made-up folder; returns (result, code)."""
    rows = rows or [{"accession_number": "0000000001-26-000001", "form": "10-K", "filing_date": "2026-07-02",
                     "acceptance_datetime": "2026-07-02T14:01:02.000Z", "reporting_date": "2025-12-31",
                     "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/t-20251231.htm"}]
    real = (server._resolve, server._filings.get_filing_metadata, server.fetch_response)
    server._resolve = lambda s: {"cik": "0000000001", "matched_on": "cik"}
    server._filings.get_filing_metadata = lambda *a, **k: ([dict(r) for r in rows], [], {})

    def fetch(url, **k):
        name = url.rsplit("/", 1)[1]
        if name not in folder:
            raise sec_client.SecNotFound(f"nothing at {url}")
        return sec_client.Response(folder[name], "application/xml", "2026-07-03T00:00:00Z")
    server.fetch_response = fetch
    try:
        return call(server.get_filed_statements, cik_or_ticker="1", **kwargs)
    finally:
        server._resolve, server._filings.get_filing_metadata, server.fetch_response = real


def xbrl_call(folder, **kwargs):
    """get_filing_xbrl on one made-up folder; returns (result, code)."""
    record = {"accessionNumber": "0000000001-26-000001", "filingDate": "2026-07-02", "reportDate": "2025-12-31",
              "acceptanceDateTime": "2026-07-02T14:01:02.000Z", "form": "10-K",
              "primaryDocument": "t-20251231.htm"}
    real = (server._resolve, server._filings.iter_filing_records, server.fetch_response)
    server._resolve = lambda s: {"cik": "0000000001", "matched_on": "cik"}

    def rows(cik, sources, warnings, older=True):
        sources.append({"source_url": "https://data.sec.gov/submissions/CIK0000000001.json",
                        "retrieved_at": "2026-07-03T00:00:00Z", "frozen": False, "cache_hit": False, "sha256": None})
        yield {"record": dict(record), "source": {"file": "CIK0000000001.json#filings.recent", "index": 0}}
    server._filings.iter_filing_records = rows

    def fetch(url, **k):
        name = url.rsplit("/", 1)[1]
        if name not in folder:
            raise sec_client.SecNotFound(f"nothing at {url}")
        return sec_client.Response(folder[name], "application/xml", "2026-07-03T00:00:00Z")
    server.fetch_response = fetch
    server._archive_cache.clear()
    try:
        return call(server.get_filing_xbrl, cik_or_ticker="1", **kwargs)
    finally:
        server._resolve, server._filings.iter_filing_records, server.fetch_response = real
        server._archive_cache.clear()


def face(result, kind):
    return next(s for s in result["statements"] if s["kind"] == kind and not s["parenthetical"])


def line(result, kind, element):
    return next(l for l in face(result, kind)["lines"] if l["element"] == element)


def filed_cases():
    r, code = statements(filed())
    check("filed: reads the statements of a filing", code is None, code)
    if code:
        return
    kinds = sorted((s["kind"], s["parenthetical"]) for s in r["statements"])
    check("filed: statements by the summary's menu, notes left out, parenthetical marked",
          kinds == [("balance_sheet", False), ("balance_sheet", True), ("income_statement", False)], str(kinds))
    own = line(r, "income_statement", "t:FulfillmentExpense")
    check("filed: an own element comes with its label, definition and documentation",
          own["extension"] and own["label"] == "Fulfillment" and own["definition"]["balance"] == "debit"
          and own["documentation"] == "Costs of fulfillment centers.", str(own))
    check("filed: an own element names the total it adds into",
          own["calculation_parent"] == "us-gaap:CostsAndExpenses" and own["weight"] == 1, str(own))
    check("filed: definition links of an own element are kept",
          bool(own.get("related")) and own["related"][0]["arcrole"].endswith("general-special"),
          str(own.get("related")))
    total = line(r, "income_statement", "us-gaap:CostsAndExpenses")
    check("filed: a preferred label is used", total["label"] == "Total costs and expenses"
          and total["label_role"] == "totalLabel", str(total))
    check("filed: values are exact and newest first",
          [v["value"] for v in total["values"]] == [70_000_000, 60_000_000], str(total["values"]))
    order = [l["element"] for l in face(r, "income_statement")["lines"] if not l.get("structure")]
    check("filed: lines keep the company's order", order ==
          ["us-gaap:Revenues", "us-gaap:CostOfRevenue", "t:FulfillmentExpense", "us-gaap:CostsAndExpenses",
           "us-gaap:OperatingIncomeLoss"], str(order))
    every = [c for s in r["statements"] for c in s["calculation_checks"]]
    check("filed: every total ties", bool(every) and all(c["consistent"] for c in every), str(every))
    vehicles = line(r, "balance_sheet", "t:LeasedAssetsNet")
    check("filed: a face line tagged only under a member takes the member's value",
          bool(vehicles.get("values_from_member"))
          and [v["value"] for v in vehicles["values"]] == [5_000_000, 4_000_000], str(vehicles))
    check("filed: the acceptance time is the header's, in UTC, and a feed that disagrees is flagged",
          r["filing"]["acceptance"] == {"header_eastern": "20260702060102", "utc": "2026-07-02T10:01:02Z",
                                        "submissions_feed": "2026-07-02T14:01:02.000Z", "agree": False},
          str(r["filing"]["acceptance"]))
    check("filed: the cover facts are read", r["document_facts"].get("DocumentType") == "10-K",
          str(r["document_facts"]))
    sec = sorted((s.get("trading_symbol"), s.get("title"), s.get("registrant_cik")) for s in r["registered_securities"])
    check("filed: the cover's registered securities are read, a combined filer's with the registrant's CIK",
          sec == [("TT", "Common Stock", None), ("TTB", "Mortgage Bonds due 2066", "2")], str(sec))
    shares = sorted((c["share_class"], c["value"], c["date"]) for c in r["cover_shares"])
    check("filed: the cover's shares outstanding are read per class",
          shares == [("t:ClassAMember", 700, "2026-02-01"), ("t:ClassBMember", 300, "2026-02-01")], str(shares))
    dei = "http://xbrl.sec.gov/dei/2025"
    cls = lambda m: {"us-gaap:StatementClassOfStockAxis": m}
    fact = lambda v, dims, unit="shares": {"namespace": dei, "local": "EntityCommonStockSharesOutstanding", "value": v,
                                           "period": "2026-02-01", "dimensions": dims, "unit": unit}
    rows = filed_statements.cover_shares({"facts": [fact(95, cls("t:ClassAMember")), fact(5, cls("t:ClassBMember")),
                                                    fact(5, cls("t:ClassBMember"))]})
    check("filed: a cover share fact tagged twice alike is one row",
          sorted((c["share_class"], c["value"]) for c in rows) == [("t:ClassAMember", 95), ("t:ClassBMember", 5)], str(rows))
    rows = filed_statements.cover_shares({"facts": [fact(95, cls("t:ClassAMember")), fact(5, cls("t:ClassBMember")),
                                                    fact(6, cls("t:ClassBMember"))]})
    b = [c for c in rows if c["share_class"] == "t:ClassBMember"]
    check("filed: a cover share fact tagged twice with two values is one row with no value and both figures",
          len(b) == 1 and b[0]["value"] is None and b[0]["conflicting_values"] == [5, 6], str(rows))
    rows = filed_statements.cover_shares({"facts": [fact(129_563_390, cls("us-gaap:CommonStockMember")),
                                                    fact(149_425, cls("us-gaap:PreferredStockMember"))]})
    check("filed: a preferred class on the cover is kept as tagged, for the reader to classify",
          sorted(c["share_class"] for c in rows) == ["us-gaap:CommonStockMember", "us-gaap:PreferredStockMember"], str(rows))
    check("filed: every file read is named with its hash", all(s.get("sha256") for s in r["sources"])
          and any(s["url"].endswith("t-20251231_pre.xml") for s in r["sources"]), str(r["sources"]))
    check("filed: the result says it is the filer's data", r["data_kind"].startswith("filer data"))

    r, _ = statements(filed(member_total=True))
    check("filed: a member with its own total is a sub-statement, not the line's value",
          not line(r, "balance_sheet", "t:LeasedAssetsNet")["values"])
    bs = face(r, "balance_sheet")["calculation_checks"]
    check("filed: then the gap is not checkable, not inconsistent",
          bool(bs) and all(c["consistent"] is None for c in bs), str(bs))

    # Mutations: a wrong figure must show.
    r, _ = statements(filed(revenue=1000))
    off = [c for c in face(r, "income_statement")["calculation_checks"] if c["consistent"] is False]
    check("filed: a wrong figure (x10) breaks its total",
          len(off) == 1 and off[0]["total"] == "us-gaap:OperatingIncomeLoss", str(off))
    r, _ = statements(filed(duplicate=103))
    check("filed: a figure tagged twice with two values is a conflict", len(r["conflicts"]) == 1, str(r["conflicts"]))
    r, _ = statements(filed(duplicate=100))
    check("filed: a figure tagged twice alike is one figure", not r["conflicts"]
          and len(line(r, "income_statement", "us-gaap:Revenues")["values"]) == 2, str(r["conflicts"]))
    r, _ = statements(filed(prohibit=True))
    checks = face(r, "income_statement")["calculation_checks"]
    check("filed: a prohibited arc removes the relationship", all(c["consistent"] for c in checks)
          and not any("us-gaap:Cash" in c["missing"] for c in checks), str(checks))

    r, _ = statements(filed(missing_child=True))
    costs = [c for c in face(r, "income_statement")["calculation_checks"]
             if c["total"] == "us-gaap:CostsAndExpenses" and c["period"] == "2025-01-01/2025-12-31"]
    check("filed: a sum that agrees with a line missing is consistent but not complete",
          len(costs) == 1 and costs[0]["consistent"] is True and costs[0]["complete"] is False
          and costs[0]["missing"] == ["t:FulfillmentExpense"] and costs[0]["missing_in_every_column"] == [], str(costs))
    r, _ = statements(filed())
    every = [c for s in r["statements"] for c in s["calculation_checks"] if s["kind"] == "income_statement"]
    check("filed: a sum with every line is complete", bool(every) and all(c["complete"] for c in every), str(every))
    r, _ = statements(filed(duplicate=103))
    cell = line(r, "income_statement", "us-gaap:Revenues")["values"][0]
    check("filed: a conflicting figure carries the conflict on its cell", cell.get("conflict") == [100_000_000, 103_000_000],
          str(cell))

    # Opening and closing balances, per column.
    r, code = statements(filed(cash_flow="us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"))
    check("filed: a cash flow statement is read", code is None, code)
    if code:
        return
    cf = face(r, "cash_flow")
    check("filed: the cash flow statement's columns are its durations", cf["columns"] ==
          ["2025-01-01/2025-12-31", "2024-01-01/2024-12-31"], str(cf["columns"]))
    bounds = [(l["label_role"], [(v["period"][:4], v["instant"], v["value"]) for v in l["values"]])
              for l in cf["lines"] if l.get("label_role", "").startswith("period")]
    check("filed: the opening line holds each column's opening balance and the closing line its closing one",
          bounds == [("periodStartLabel", [("2025", "2024-12-31", 8_000_000), ("2024", "2023-12-31", 7_000_000)]),
                     ("periodEndLabel", [("2025", "2025-12-31", 10_000_000), ("2024", "2024-12-31", 8_000_000)])],
          str(bounds))
    rolls = cf["roll_forward_checks"]
    check("filed: opening cash plus the change is closing cash, each year",
          len(rolls) == 2 and all(x["consistent"] for x in rolls) and rolls[0]["exchange_rate_element"] is None,
          str(rolls))
    check("filed: cash of another scope than the balance sheet's is not tied to it",
          all(x["closing_on_balance_sheet"] is None for x in rolls), str(rolls))
    check("filed: the opening and closing lines are left out of the sum checks",
          all(c["total"].endswith("IncludingExchangeRateEffect") for c in cf["calculation_checks"])
          and all(c["consistent"] and c["complete"] for c in cf["calculation_checks"]), str(cf["calculation_checks"]))
    r, _ = statements(filed(cash_flow="us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                            opening_2024=4))
    off = [x for x in face(r, "cash_flow")["roll_forward_checks"] if x["consistent"] is False]
    check("filed: a wrong opening balance breaks the roll-forward",
          [x["period"] for x in off] == ["2024-01-01/2024-12-31"] and off[0]["difference"] == 3_000_000, str(off))
    r, _ = statements(filed(cash_flow="us-gaap_Cash"))
    rolls = face(r, "cash_flow")["roll_forward_checks"]
    check("filed: closing cash the balance sheet shows is tied to it",
          len(rolls) == 2 and all(x["consistent"] and x["closing_on_balance_sheet"] is True for x in rolls), str(rolls))

    # Replaced in 1.0 (step 4): the `facts` parameter of get_filed_statements and its `requested_facts` are
    # gone (they chose whole-company values, split member values out and turned element identities into
    # prefixed names). get_filing_xbrl part="instance" with `concepts` returns the facts as written instead.
    r, code = xbrl_call(filed(), part="instance", concepts=["us-gaap:Cash", "LeasedAssetsNet", "us-gaap:Nothing"])
    facts = [i for i in (r or {}).get("items", []) if i["node"] == "fact"]
    check("xbrl: a concept asked for by name returns its facts with the text as written",
          code is None and [(f["contextRef"], f["text"]) for f in facts if f["concept"]["local_name"] == "Cash"]
          == [("I25", "10000000"), ("I24", "8000000")], code or str(facts)[:200])
    matches = {m["name"]: m["matches"] for m in r["concept_matches"]}
    check("xbrl: a name without its prefix matches the local name by namespace and local name",
          matches["LeasedAssetsNet"] == [{"namespace": "http://t.example/2025", "local_name": "LeasedAssetsNet"}],
          str(matches["LeasedAssetsNet"]))
    leased = [f for f in facts if f["concept"]["local_name"] == "LeasedAssetsNet"]
    members = {i["id"]: i["segment"]["explicit_members"] for i in r["items"] if i["node"] == "context" and i["segment"]}
    check("xbrl: a fact tagged only under a member is returned with its context's member, nothing promoted",
          [f["contextRef"] for f in leased] == ["V25", "V24"]
          and all(members[f["contextRef"]][0]["member"]["local_name"] == "VehiclesMember" for f in leased),
          str(leased)[:200])
    check("xbrl: a concept the filing does not tag matches nothing and is not unresolved",
          matches["us-gaap:Nothing"] == [] and not r["unresolved"], str(r["unresolved"]))
    listed = f"{US_GAAP}#OtherLiabilitiesNoncurrent http://t.example/2025#LeaseLiabilities"
    r, code = xbrl_call(filed(lease_list=listed), part="instance",
                        concepts=["us-gaap:OperatingLeaseLiabilityNoncurrentStatementOfFinancialPositionExtensibleList"])
    facts = [i for i in (r or {}).get("items", []) if i["node"] == "fact"]
    check("xbrl: a text fact tagged twice is returned twice, as written, with no names derived from it",
          code is None and [f["text"] for f in facts] == [listed, listed] and all("elements" not in f for f in facts),
          code or str(facts)[:200])
    r, code = xbrl_call(filed(duplicate=95), part="instance", concepts=["us-gaap:Revenues"])
    d25 = [f["text"] for f in (r or {}).get("items", []) if f["node"] == "fact" and f["contextRef"] == "D25"]
    check("xbrl: conflicting duplicates are both returned, none chosen", d25 == ["100000000", "95000000"],
          code or str(d25))
    _, code = xbrl_call(filed(), part="instance", concepts=["not a name"])
    check("xbrl: a malformed concept name is INVALID_INPUT", code == "INVALID_INPUT", code)
    r, code = xbrl_call(filed(inline=False))
    check("xbrl: without a doctype entry, the one <name>-<YYYYMMDD>.xml in InputFiles is the instance",
          code is None and r["files"]["instance"]["name"] == "t-20251231.xml"
          and "InputFiles" in r["files"]["instance"]["found_by"], code or str(r["files"]["instance"]))
    check("xbrl: the header acceptance time is returned as written",
          r["acceptance"]["header_acceptance_datetime"] == "20260702060102"
          and r["acceptance"]["submissions_acceptanceDateTime"] == "2026-07-02T14:01:02.000Z", str(r["acceptance"]))

    r, code = statements(filed(calc11=True))
    check("filed: Calculation 1.1 arcs are read", code is None and r["calculation_versions"] == ["1.1"]
          and bool(face(r, "income_statement")["calculation_checks"]), code or str(r["calculation_versions"]))
    r, code = statements(filed(embedded=True))
    check("filed: linkbases inside the schema are read", code is None and
          line(r, "income_statement", "t:FulfillmentExpense")["calculation_parent"] == "us-gaap:CostsAndExpenses",
          code)
    r, code = statements(filed(inline=False))
    check("filed: an instance from before inline XBRL is read", code is None and not r["filing"]["inline_xbrl"]
          and bool(line(r, "income_statement", "us-gaap:Revenues")["values"]), code)
    r, code = statements(filed(summary_menu=False))
    check("filed: without the summary's menu the schema's role category decides",
          code is None and len([s for s in r["statements"] if not s["parenthetical"]]) == 2, code)
    r, _ = statements(filed(header="20260115093000"))
    check("filed: winter acceptance is Eastern standard time",
          r["filing"]["acceptance"]["utc"] == "2026-01-15T14:30:00Z", str(r["filing"]["acceptance"]))

    _, code = statements(filed(), as_of="2026-07-02T09:00:00Z")
    check("filed: as_of with a time leaves out a filing accepted later", code == "NOT_FOUND", code)
    _, code = statements(filed(), as_of="2026-07-02T10:30:00Z")
    check("filed: as_of with a time keeps a filing accepted before", code is None, code)
    _, code = statements({})
    check("filed: a filing without XBRL is UNSUPPORTED_DOCUMENT", code == "UNSUPPORTED_DOCUMENT", code)
    _, code = statements(filed(), accession_number="12-34")
    check("filed: a malformed accession number is INVALID_INPUT", code == "INVALID_INPUT", code)
    broken = filed()
    broken["t-20251231_pre.xml"] = b"<link:linkbase"
    _, code = statements(broken)
    check("filed: an unreadable linkbase is UNSUPPORTED_DOCUMENT", code == "UNSUPPORTED_DOCUMENT", code)


def text_cases():
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/frozen-test.htm"
    real = server.fetch_response
    server.fetch_response = lambda u, **k: sec_client.Response(b"<p>Net sales rose.</p>", "text/html",
                                                               "2026-02-02T00:00:00Z", True)
    try:
        first, _ = call(server.get_filing_text, url=url)
        server.fetch_response = lambda u, **k: (_ for _ in ()).throw(AssertionError("read twice"))
        second, _ = call(server.get_filing_text, url=url, find="net sales")
        check("text: a frozen document says so", first["frozen"] is True and first["text"] == "Net sales rose.")
        check("text: and still says so when served from memory",
              second["cache_hit"] and second["frozen"] is True and second["retrieved_at"] == "2026-02-02T00:00:00Z")
        check("text: the answer says the text is the filer's, not instructions",
              "not instructions" in first["content"] and "not instructions" in second["content"])
    finally:
        server.fetch_response = real


# ── company index: files, precedence, builder ──────────────────────────────

def index_cases():
    real = (company_classifier.COMPANY_INDEX, company_classifier.USER_INDEX_FILE)
    folder = tempfile.mkdtemp(prefix="edgar_index_test_")
    try:
        company_classifier.COMPANY_INDEX, company_classifier.USER_INDEX_FILE = None, os.path.join(folder, "none.json")
        companies, meta = company_classifier.load_index_with_meta()
        check("index: with nothing built, the packaged snapshot is read",
              meta["origin"] == "snapshot in the package" and len(companies) > 1000 and "schema_version" not in companies)
        check("index: the snapshot does not claim a build date it has no record of", meta["built"] is None)

        legacy = os.path.join(folder, "legacy.json")
        json.dump({"1": {"name": "Old Co", "sic": "1000"}}, open(legacy, "w", encoding="utf-8"))
        company_classifier.COMPANY_INDEX = legacy
        companies, meta = company_classifier.load_index_with_meta()
        check("index: an older index is read, its provenance unknown",
              list(companies) == ["1"] and meta["built"] is None and meta["origin"] == "EDGAR_COMPANY_INDEX")
        os.utime(legacy, (0, 0))
        check("index: a file's modification time does not set the build date",
              company_classifier.load_index_meta()["built"] is None)

        broken = os.path.join(folder, "broken.json")
        open(broken, "w").write("{not json")
        company_classifier.COMPANY_INDEX = broken
        try:
            company_classifier.load_index_with_meta()
            check("index: a named index that cannot be read is an error", False)
        except company_classifier.IndexUnavailable:
            check("index: a named index that cannot be read is an error", True)
        real_index, server._classifier_index = server._classifier_index, None
        try:
            _, code = call(server.search_companies, sic="1000")
            check("index: search_companies reports it as INDEX_UNAVAILABLE", code == "INDEX_UNAVAILABLE", code)
        finally:
            server._classifier_index = real_index

        # The builder: two quarters; the newer filing has no geographic split.
        data = os.path.join(folder, "sets")
        sub_head = "adsh\tcik\tname\tsic\tcountryinc\tcountryba\tstprinc\tstprba\tform\tperiod\n"
        num_head = "adsh\ttag\tversion\tddate\tqtrs\tuom\tsegments\tvalue\n"
        quarters = {
            "2025q1": (sub_head + "a1\t7\tOLD NAME\t1000\tUS\tUS\tDE\tDE\t10-K\t20231231\n",
                       num_head + "a1\tRevenues\tus-gaap/2023\t20231231\t4\tUSD\t\t100\n"
                       "a1\tRevenues\tus-gaap/2023\t20231231\t4\tUSD\tGeographical=US\t80\n"),
            "2025q2": (sub_head + "a2\t7\tNEW NAME\t2000\tUS\tUS\tNV\tNV\t10-K\t20241231\n",
                       num_head + "a2\tRevenues\tus-gaap/2024\t20241231\t4\tUSD\t\t120\n"),
            "2025q3": (sub_head, num_head),
        }
        for q, (sub, num) in quarters.items():
            os.makedirs(os.path.join(data, q))
            open(os.path.join(data, q, "sub.txt"), "w", encoding="utf-8").write(sub)
            open(os.path.join(data, q, "num.txt"), "w", encoding="utf-8").write(num)
        built, used = company_classifier.build_index(max_quarters=2, data_dir=data)
        check("builder: the quarters actually read are the ones recorded", used == ["2025q3", "2025q2"], str(used))
        built, used = company_classifier.build_index(max_quarters=3, data_dir=data)
        entry = built["7"]
        check("builder: name, SIC and state come from the latest filing",
              (entry["name"], entry["sic"], entry["state_inc"]) == ("NEW NAME", "2000", "NV"), str(entry))
        check("builder: the revenue split may come from an older one, and says so",
              entry["revenue_country"] == "US" and entry["revenue_period"] == "20231231", str(entry))

        snapshot = company_classifier.resources.files("edgar_mcp").joinpath("data", "company_index.json")
        before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        output = os.path.join(folder, "built.json")
        company_classifier.save_index(built, used, output)
        company_classifier.COMPANY_INDEX, company_classifier.USER_INDEX_FILE = None, output
        companies, meta = company_classifier.load_index_with_meta()
        check("index: a built index is preferred to the snapshot, with its record",
              meta["origin"] == "built by the user" and meta["quarters"] == used and list(companies) == ["7"])
        check("index: building leaves the packaged snapshot unchanged",
              hashlib.sha256(snapshot.read_bytes()).hexdigest() == before)
        company_classifier.COMPANY_INDEX = legacy
        check("index: EDGAR_COMPANY_INDEX comes before a built index",
              company_classifier.load_index_meta()["origin"] == "EDGAR_COMPANY_INDEX")
    finally:
        company_classifier.COMPANY_INDEX, company_classifier.USER_INDEX_FILE = real


def path_cases():
    home = str(settings.Path.home())
    real_sys, saved = settings.sys, {k: os.environ.get(k) for k in ("LOCALAPPDATA", "XDG_CACHE_HOME", "XDG_DATA_HOME")}

    def where(platform, kind, **env):
        settings.sys = types.SimpleNamespace(platform=platform)
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        return str(settings._user_dir(kind))
    try:
        sep = os.sep
        check("paths: Windows uses LOCALAPPDATA",
              where("win32", "cache", LOCALAPPDATA=os.path.join(home, "L")).endswith(f"L{sep}edgar-search-mcp{sep}Cache"))
        check("paths: Windows without LOCALAPPDATA falls back to the home folder",
              where("win32", "cache").startswith(home))
        check("paths: macOS uses Library/Caches", f"Library{sep}Caches" in where("darwin", "cache"))
        absolute = os.path.join(home, "xdg")
        check("paths: Linux uses an absolute XDG_CACHE_HOME",
              where("linux", "cache", XDG_CACHE_HOME=absolute).startswith(absolute))
        check("paths: a relative XDG_CACHE_HOME is ignored",
              where("linux", "cache", XDG_CACHE_HOME="rel").startswith(os.path.join(home, ".cache")))
        check("paths: data is kept apart from the cache",
              where("linux", "data") != where("linux", "cache"))
    finally:
        settings.sys = real_sys
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    check("paths: EDGAR_CACHE_DIR still comes first", settings.CACHE_DIR == os.environ["EDGAR_CACHE_DIR"])


if __name__ == "__main__":
    search_cases()
    client_cases()
    lookup_cases()
    history_cases()
    segment_cases()
    filed_cases()
    text_cases()
    index_cases()
    path_cases()
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
