"""The tools against made-up SEC answers: search, segments, filing history,
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
from edgar_mcp.edgar import company_classifier, company_lookup, filing_retrieval  # noqa: E402
from edgar_mcp.edgar.tag_classifier import TagClassifier  # noqa: E402
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


# ── statement classification ───────────────────────────────────────────────

def classifier_cases():
    tags = ["InterestExpense", "InterestIncome", "DividendsCommonStockCash",
            "StockIssuedDuringPeriodValueRestrictedStockAwardGross", "DebtSecuritiesUnrealizedGainLoss",
            "LeaseCost", "IncomeLossAttributableToParent", "MadeUpDurationThing", "Revenues"]
    c = TagClassifier()
    is_, report = c.classify_with_report(tags, "IS")
    check("classifier: interest expense is not revenue", is_["InterestExpense"]["category"] != "Revenue")
    check("classifier: interest income keeps its place", is_["InterestIncome"]["category"] == "Revenue")
    check("classifier: dividends and restricted stock are not income statement lines",
          "DividendsCommonStockCash" not in is_ and "StockIssuedDuringPeriodValueRestrictedStockAwardGross" not in is_)
    eq, _ = c.classify_with_report(tags, "EQ")
    bs, _ = c.classify_with_report(tags, "BS")
    check("classifier: they are movements in equity, not balances",
          "DividendsCommonStockCash" in eq and "DividendsCommonStockCash" not in bs)
    check("classifier: the unrealized debt gain is excluded with a reason",
          "comprehensive" in report["excluded"].get("DebtSecuritiesUnrealizedGainLoss", ""))
    check("classifier: an unknown tag stays unmapped, not guessed", "MadeUpDurationThing" in report["unmapped"])
    check("classifier: lease cost stays available", "LeaseCost" in is_)
    label = is_["IncomeLossAttributableToParent"]["display_name"].lower()
    check("classifier: the parent, before-tax label keeps both meanings", "parent" in label and "before tax" in label)
    other, _ = c.classify_with_report(["InterestAndOtherIncome"], "IS")
    check("classifier: interest and other income is not called interest income",
          other["InterestAndOtherIncome"]["display_name"] == "Interest and other income")
    check("classifier: dividends are reported as on another statement",
          "DividendsCommonStockCash" in report["other_statements"])


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
    text_cases()
    classifier_cases()
    index_cases()
    path_cases()
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
