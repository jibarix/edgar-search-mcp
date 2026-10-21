"""The tools against made-up SEC answers: search, company lookup, filing rows, filing XBRL,
retries and frozen copies, filing text and the user folders.

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

from urllib.parse import quote  # noqa: E402

import httpx  # noqa: E402

from edgar_mcp import server  # noqa: E402
from edgar_mcp.config import settings  # noqa: E402
from edgar_mcp.edgar import company_lookup, filing_retrieval  # noqa: E402
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


def meta(url):
    return {"source_url": url, "retrieved_at": "2026-01-01T00:00:00Z", "cache_hit": False, "frozen": False,
            "sha256": "0" * 64}


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
            "a hit with ciks that is not a list": page(1, [{"_id": "x:y.htm", "_source": {"ciks": "1"}}]),
        }
        for label, answer in bad.items():
            server.get_json = lambda url, *a, answer=answer, **k: (answer, meta(url))
            _, code = call(server.search_filings, query="x")
            check(f"search: {label} is an upstream failure", code == "UPSTREAM_UNAVAILABLE", code)

        for relation in ("eq", "gte"):
            server.get_json = lambda url, *a, relation=relation, **k: (page(0, [], relation), meta(url))
            result, code = call(server.search_filings, query="x")
            check(f"search: no hits ({relation}) is a valid empty answer, the relation as the index gives it",
                  code is None and result["count"] == 0 and result["total_relation"] == relation, code)

        odd = {"_id": "0000000001-24-000001:a.htm", "_score": 3.5, "extra": [1],
               "_source": {"ciks": ["0000000001", "0000000002"], "display_names": [5], "sics": ["x"]}}
        server.get_json = lambda url, *a, **k: (page(1, [odd]), meta(url))
        result, code = call(server.search_filings, query="x")
        check("search: a hit is returned verbatim, names and codes as given, nothing parsed",
              code is None and result["hits"][0]["record"] == odd, str(result and result["hits"]))
        check("search: the answer names the address it read",
              result["sources"][0]["url"].startswith(server._SEARCH_URL))
        for label, broken in [("an _id with no file", "0000000001-24-000001"),
                              ("an _id that is not an accession", "abc:a.htm")]:
            server.get_json = lambda url, *a, broken=broken, **k: (page(1, [{**odd, "_id": broken}]), meta(url))
            result, code = call(server.search_filings, query="x")
            check(f"search: {label} keeps its record and has no url",
                  code is None and result["hits"][0]["url"] is None and result["hits"][0].get("urls") == []
                  and result["hits"][0]["record"]["_id"] == broken)

        # 10,000 hits: continuation runs through the last upstream page and stops at the ceiling.
        asked = []

        def pages(url, *a, **k):
            start = int(url.split("&from=")[1]) if "&from=" in url else 0
            asked.append(start)
            return page(10000, [hit(start + i) for i in range(100)], relation="gte"), meta(url)
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

        server.get_json = lambda url, *a, **k: (page(30, [hit(i) for i in range(30)]), meta(url))
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
    feed = {"0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"},
            "1": {"cik_str": 1, "ticker": "AAB", "title": "Alpha Inc"}}
    body = json.dumps(feed).encode("utf-8")
    asked = []

    def answer(request):
        asked.append(str(request.url))
        if str(request.url) == company_lookup.COMPANY_TICKERS_URL:
            return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
        return unexpected(request)
    company_lookup.company_cache.delete("company_tickers_v6")
    transport(answer)
    try:
        first, code = call(server.lookup_company, query="AAA")
        second, code2 = call(server.lookup_company, query="alpha inc")
        src1, src2 = first["sources"][0], second["sources"][0]
        check("lookup_company names the feed, when it was read and the hash of its bytes",
              code is None and src1["url"] == company_lookup.COMPANY_TICKERS_URL and src1["retrieved_at"]
              and src1["sha256"] == hashlib.sha256(body).hexdigest() and src1["cache_hit"] is False, str(src1))
        check("a cached lookup keeps the download time and the hash, and says it was cached",
              code2 is None and src2["cache_hit"] is True and src2["retrieved_at"] == src1["retrieved_at"]
              and src2["sha256"] == src1["sha256"] and len(asked) == 1, f"{src2} {len(asked)}")
        check("a company under two tickers: the ticker matches one row, the title both, one CIK",
              first["status"] == second["status"] == "resolved" and len(first["matches"]) == 1
              and [m["record"] for m in second["matches"]] == list(feed.values()))
        third, _ = call(server.lookup_company, query="Alpha")
        check("a partial title is not found, and nothing is unresolved", third["status"] == "not_found"
              and third["matches"] == [] and third["unresolved"] == [])
    finally:
        company_lookup.company_cache.delete("company_tickers_v6")
        sec_client._client = httpx.Client(transport=httpx.MockTransport(unexpected))


# ── list_filings over the submissions tables ────────────────────────────────

def table(*rows):
    """A submissions column table from (accession, form, filingDate) rows."""
    return {"accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
            "filingDate": [r[2] for r in rows], "primaryDocument": [f"d{i}.htm" for i in range(len(rows))]}


def history_cases():
    real = filing_retrieval.get_json
    recent = table(("0000000001-25-000002", "10-K/A", "2025-03-02"), ("0000000001-25-000001", "10-K", "2025-03-01"),
                   ("0000000001-24-000009", "10-KT", "2024-09-01"), ("0000000001-24-000008", "10-K405", "2024-08-01"),
                   ("0000000001-24-000007", "10-k", "2024-07-01"))
    files = [{"name": "CIK0000000001-submissions-001.json"}, {"name": "CIK0000000001-submissions-002.json"}]
    read = []

    def answers(older):
        def get(url, *a, **k):
            name = url.rsplit("/", 1)[1]
            read.append(name)
            got = ({"filings": {"recent": recent, "files": files}} if name == "CIK0000000001.json"
                   else older.get(name))
            if isinstance(got, Exception):
                raise got
            return got, meta(url)
        return get

    def listed(older, **kwargs):
        read.clear()
        filing_retrieval.get_json = answers(older)
        return call(server.list_filings, cik_or_ticker="1", **kwargs)
    fail = sec_client.SecError("HTTP 503")
    try:
        first = table(("0000000001-20-000001", "10-K", "2020-03-01"), ("0000000001-19-000001", "10-K", ""))
        second = table(("0000000001-15-000001", "10-K", "2015-03-01"))
        both = {"CIK0000000001-submissions-001.json": first, "CIK0000000001-submissions-002.json": second}
        result, code = listed({"CIK0000000001-submissions-001.json": first,
                               "CIK0000000001-submissions-002.json": fail}, form="10-K", limit=1)
        check("list_filings: the form is compared literally (not 10-K/A, 10-KT, 10-K405 or 10-k)",
              code is None and [f["record"]["accessionNumber"] for f in result["filings"]] == ["0000000001-25-000001"])
        check("list_filings: an older file is not read while the recent table fills the limit",
              read == ["CIK0000000001.json"] and result["limit_reached"] and result["complete"], str(read))
        result, code = listed({"CIK0000000001-submissions-001.json": first,
                               "CIK0000000001-submissions-002.json": fail}, form="10-K", limit=2)
        check("list_filings: older files are read in the order listed, only as far as the limit needs",
              code is None and read == ["CIK0000000001.json", "CIK0000000001-submissions-001.json"]
              and [f["source"]["file"] for f in result["filings"]]
              == ["CIK0000000001.json#filings.recent", "CIK0000000001-submissions-001.json"], str(read))
        _, code = listed({"CIK0000000001-submissions-001.json": sec_client.SecNoFrozenCopy("no copy"),
                          "CIK0000000001-submissions-002.json": second}, form="10-K", limit=5)
        check("list_filings: an unreadable older file is NO_FROZEN_COPY, never a shorter list",
              code == "NO_FROZEN_COPY", code)
        _, code = listed({"CIK0000000001-submissions-001.json": fail}, form="10-K", limit=5)
        check("list_filings: an older file the SEC fails to serve is UPSTREAM_UNAVAILABLE",
              code == "UPSTREAM_UNAVAILABLE", code)
        result, code = listed(both, form="10-K", limit=10, include_amendments=True)
        check("list_filings: amendments come when asked, in the SEC's order, and every file is read",
              code is None and [f["record"]["form"] for f in result["filings"]]
              == ["10-K/A", "10-K", "10-K", "10-K", "10-K"] and not result["limit_reached"] and result["complete"])
        result, code = listed(both, form="10-K", as_of="2024-12-31")
        check("list_filings: as_of keeps rows filed on or before it; a row without a date is left out and named",
              code is None and [f["record"]["filingDate"] for f in result["filings"]] == ["2020-03-01", "2015-03-01"]
              and len(result["warnings"]) == 1 and "0000000001-19-000001" in result["warnings"][0]
              and result["complete"] is False, str(result and result["warnings"]))
        result, code = listed(both, form=None, limit=6)
        check("list_filings: form=None keeps every form, as written",
              code is None and [f["record"]["form"] for f in result["filings"]]
              == ["10-K/A", "10-K", "10-KT", "10-K405", "10-k", "10-K"])
        check("list_filings: each file read is a source", [s["url"].rsplit("/", 1)[1] for s in result["sources"]]
              == ["CIK0000000001.json", "CIK0000000001-submissions-001.json"])
        for bad in [{"form": "10-K; drop"}, {"as_of": "2024/12/31"}, {"limit": 1001}]:
            _, code = listed({}, **bad)
            check(f"list_filings: refused {bad}", code == "INVALID_INPUT", code)
    finally:
        filing_retrieval.get_json = real


US_GAAP = "http://fasb.org/us-gaap/2024"


# ── get_filing_xbrl on made-up filings ──────────────────────────────────────

def filed(embedded=False, inline=True, revenue=100, summary_menu=True, calc11=False, prohibit=False,
          duplicate=None, member_total=False, header="20260702060102", cash_flow=None, opening_2024=7,
          missing_child=False, lease_list=None, index_rows=None):
    """A made-up filing folder: name -> bytes. Statements: an income statement
    with an own element that adds into costs, a balance sheet with a line
    tagged only under a member, and a parenthetical page. `cash_flow` adds a
    cash flow statement whose opening and closing balance is that element
    ("us-gaap_Cash", on the balance sheet too, or one of restricted cash);
    `opening_2024` is its balance at the end of 2023. `lease_list` is the text of
    the extensible list naming the lines operating leases are in, at the end of 2025. The folder has the
    SEC's filing index page; `index_rows` replaces its rows, as (sequence, description, document, type)."""
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
    instance_xml = (f'<xbrli:xbrl {xbrli_ns} {ns} xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
                    f'xmlns:us-gaap="{US_GAAP}" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
                    'xmlns:t="http://t.example/2025" xmlns:dei="http://xbrl.sec.gov/dei/2024">'
                    '<link:schemaRef xlink:type="simple" xlink:href="t-20251231.xsd"/>'
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
    if index_rows is None:
        index_rows = [("1", "10-K", "t-20251231.htm", "10-K"),
                      ("2", "XBRL INSTANCE DOCUMENT", "t-20251231.xml", "EX-101.INS")] if not inline else [
                      ("1", "10-K", "t-20251231.htm", "10-K")]
        index_rows += [("3", "XBRL TAXONOMY EXTENSION SCHEMA DOCUMENT", "t-20251231.xsd", "EX-101.SCH")]
        if not embedded:
            index_rows += [(str(4 + i), f"XBRL TAXONOMY EXTENSION {k} LINKBASE DOCUMENT", f"t-20251231_{s}.xml",
                            f"EX-101.{s.upper()}") for i, (k, s) in
                           enumerate((("CALCULATION", "cal"), ("DEFINITION", "def"), ("LABEL", "lab"),
                                      ("PRESENTATION", "pre")))]
        if inline:
            index_rows += [("9", "<b>EXTRACTED</b> XBRL INSTANCE DOCUMENT", "t-20251231_htm.xml", "XML")]
    folder = {"FilingSummary.xml": summary.encode(), "t-20251231.xsd": xsd, instance_name: instance_xml,
              "0000000001-26-000001-index-headers.html":
                  f"<SEC-HEADER><ACCEPTANCE-DATETIME>{header}</SEC-HEADER>".encode(),
              "0000000001-26-000001-index.htm": index_page(index_rows)}
    if not embedded:
        folder.update({"t-20251231_pre.xml": linkbase(pre), "t-20251231_cal.xml": linkbase(cal),
                       "t-20251231_lab.xml": linkbase(lab), "t-20251231_def.xml": linkbase(definition)})
    return folder


RECORD = {"accessionNumber": "0000000001-26-000001", "filingDate": "2026-07-02", "reportDate": "2025-12-31",
          "acceptanceDateTime": "2026-07-02T14:01:02.000Z", "form": "10-K", "primaryDocument": "t-20251231.htm"}


def index_page(rows):
    """The SEC's filing index page with these (sequence, description, document, type) rows."""
    body = "".join(f'<tr><td scope="row">{q}</td><td scope="row">{d}</td><td scope="row">'
                   f'<a href="/Archives/edgar/data/1/000000000126000001/{f}">{f}</a></td>'
                   f'<td scope="row">{t}</td><td scope="row">100</td></tr>' for q, d, f, t in rows)
    return f'<html><body><table class="tableFile">{body}</table></body></html>'.encode()


def xbrl_call(folder, records=None, **kwargs):
    """get_filing_xbrl on one made-up folder; `records` are the submissions rows offered, in order
    (by default one 10-K row). Returns (result, code)."""
    records = [RECORD] if records is None else records
    real = (server._resolve, server._filings.iter_filing_records, server.fetch_response)
    server._resolve = lambda s: {"cik": "0000000001", "matched_on": "cik"}

    def rows(cik, sources, warnings, older=True):
        sources.append({"source_url": "https://data.sec.gov/submissions/CIK0000000001.json",
                        "retrieved_at": "2026-07-03T00:00:00Z", "frozen": False, "cache_hit": False, "sha256": None})
        for i, record in enumerate(records):
            yield {"record": dict(record), "source": {"file": "CIK0000000001.json#filings.recent", "index": i}}
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


XL = "{http://www.w3.org/1999/xlink}"
XBRLI = "{http://www.xbrl.org/2003/instance}"
FOLDER_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/"


def nodes(result, node):
    return [i for i in (result or {}).get("items", []) if i["node"] == node]


def arcs_between(result, parent, child):
    return [a for a in nodes(result, "arc")
            if a["attributes"].get(f"{XL}from") == parent and a["attributes"].get(f"{XL}to") == child]


def instance_cases():
    # Replaced in 1.0 (step 4): the `facts` parameter of get_filed_statements and its `requested_facts` are
    # gone (they chose whole-company values, split member values out and turned element identities into
    # prefixed names). get_filing_xbrl part="instance" with `concepts` returns the facts as written instead.
    r, code = xbrl_call(filed(), part="instance", concepts=["us-gaap:Cash", "LeasedAssetsNet", "us-gaap:Nothing"])
    facts = nodes(r, "fact")
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
    facts = nodes(r, "fact")
    check("xbrl: a text fact tagged twice is returned twice, as written, with no names derived from it",
          code is None and [f["text"] for f in facts] == [listed, listed] and all("elements" not in f for f in facts),
          code or str(facts)[:200])
    r, code = xbrl_call(filed(duplicate=95), part="instance", concepts=["us-gaap:Revenues"])
    d25 = [f["text"] for f in nodes(r, "fact") if f["contextRef"] == "D25"]
    check("xbrl: conflicting duplicates are both returned, none chosen", d25 == ["100000000", "95000000"],
          code or str(d25))
    r, code = xbrl_call(filed(duplicate=100), part="instance", concepts=["us-gaap:Revenues"])
    d25 = [f["text"] for f in nodes(r, "fact") if f["contextRef"] == "D25"]
    check("xbrl: identical duplicates are both returned, none merged", d25 == ["100000000", "100000000"],
          code or str(d25))
    _, code = xbrl_call(filed(), part="instance", concepts=["not a name"])
    check("xbrl: a malformed concept name is INVALID_INPUT", code == "INVALID_INPUT", code)
    _, code = xbrl_call(filed(), part="schema", concepts=["us-gaap:Cash"])
    check("xbrl: `concepts` with another part than the instance is INVALID_INPUT", code == "INVALID_INPUT", code)
    r, code = xbrl_call(filed(inline=False))
    check("xbrl: the instance is the one document the filing index lists with type EX-101.INS",
          code is None and r["files"]["instance"]["name"] == "t-20251231.xml"
          and "EX-101.INS" in r["files"]["instance"]["found_by"], code or str(r and r["files"]["instance"]))
    check("xbrl: the header acceptance time is returned as written",
          r["acceptance"]["header_acceptance_datetime"] == "20260702060102"
          and r["acceptance"]["submissions_acceptanceDateTime"] == "2026-07-02T14:01:02.000Z", str(r["acceptance"]))
    r, _ = xbrl_call(filed(header="20260115093000"))
    check("xbrl: the header's Eastern time is also given in UTC, winter time as standard time",
          r["acceptance"]["header_utc"] == "2026-01-15T14:30:00Z", str(r["acceptance"]))


def xbrl_link_cases():
    # Replaced in 1.0 (step 5): get_filed_statements is gone. Its tests asserted statement kinds from the
    # FilingSummary menu and role titles, parenthetical flags, chosen English labels, values promoted from
    # members, opening and closing balances reassigned per column, calculation and roll-forward checks,
    # prohibited arcs applied, and the reshaped cover (document_facts, registered_securities, cover_shares).
    # get_filing_xbrl returns the schema and every linkbase as written instead; these tests say so.
    r, code = xbrl_call(filed(), part="schema")
    check("xbrl schema: the company schema is read", code is None, code)
    if code:
        return
    elements = {i["name"]: i for i in nodes(r, "element")}
    own = elements.get("FulfillmentExpense", {})
    check("xbrl schema: an own element keeps every declared attribute as written",
          own.get("attributes", {}).get(f"{XBRLI}balance") == "debit"
          and own["attributes"].get(f"{XBRLI}periodType") == "duration" and own["attributes"].get("abstract") == "false"
          and own["namespace"] == "http://t.example/2025" and own["id"] == "t_FulfillmentExpense", str(own))
    check("xbrl schema: an element's type is resolved where it is written",
          own["type"] == {"qname": "xbrli:monetaryItemType", "resolved": True,
                          "namespace": "http://www.xbrl.org/2003/instance", "local_name": "monetaryItemType"},
          str(own.get("type")))
    vehicles = elements.get("VehiclesMember", {})
    check("xbrl schema: a type whose prefix the schema does not declare is unresolved, not guessed",
          vehicles.get("type", {}).get("resolved") is False
          and any(u.get("element") == "VehiclesMember" for u in r["unresolved"]), str(r["unresolved"]))
    roles = [(i["attributes"]["roleURI"], i["definition"]) for i in nodes(r, "roleType")]
    check("xbrl schema: role definitions are returned as written, a parenthetical title included, none classified",
          ("http://t.example/role/BalanceParenthetical",
           "0000004 - Statement - CONSOLIDATED BALANCE SHEETS (Parenthetical)") in roles
          and all("kind" not in i and "parenthetical" not in i for i in r["items"]), str(roles))
    check("xbrl schema: usedOn is a resolved QName", nodes(r, "roleType")[0]["usedOn"][0]["local_name"]
          == "presentationLink", str(nodes(r, "roleType")[0]))
    check("xbrl schema: the linkbase references are in document order",
          [i["href"] for i in nodes(r, "linkbaseRef")]
          == ["t-20251231_pre.xml", "t-20251231_cal.xml", "t-20251231_lab.xml", "t-20251231_def.xml"],
          str(nodes(r, "linkbaseRef")))

    r, code = xbrl_call(filed(), part="presentation")
    check("xbrl presentation: read from the referenced linkbase", code is None
          and [(f["name"], f["read"], f["extended_links"]) for f in r["linkbases"]] ==
          [("t-20251231_pre.xml", True, 4), ("t-20251231_cal.xml", True, 0), ("t-20251231_lab.xml", True, 0),
           ("t-20251231_def.xml", True, 0)], code or str(r["linkbases"]))
    links = [i["attributes"][f"{XL}role"] for i in nodes(r, "extendedLink")]
    check("xbrl presentation: every extended link in document order, the parenthetical one as any other",
          links == ["http://t.example/role/Income", "http://t.example/role/Balance",
                    "http://t.example/role/BalanceParenthetical", "http://t.example/role/Policies"], str(links))
    total = arcs_between(r, "us-gaap_CostsAndExpensesAbstract", "us-gaap_CostsAndExpenses")
    check("xbrl presentation: an arc keeps order and preferredLabel as written",
          len(total) == 1 and total[0]["attributes"]["order"] == "3"
          and total[0]["attributes"]["preferredLabel"] == "http://www.xbrl.org/2003/role/totalLabel", str(total))
    own = [l for l in nodes(r, "loc") if l["href"] == "t-20251231.xsd#t_FulfillmentExpense"]
    check("xbrl presentation: a locator into the company schema resolves to its element",
          own and own[0]["target"] == {"resolved": True, "file": "t-20251231.xsd", "namespace": "http://t.example/2025",
                                       "local_name": "FulfillmentExpense", "schema_seq": own[0]["target"]["schema_seq"]},
          str(own))
    gaap = [l for l in nodes(r, "loc") if l["href"].endswith("#us-gaap_Revenues")]
    check("xbrl presentation: a locator into a schema outside the filing is unresolved, href and fragment kept",
          gaap and all(l["target"]["resolved"] is False and l["target"]["fragment"] == "us-gaap_Revenues"
                       for l in gaap), str(gaap))
    external = [u for u in r["unresolved"] if u.get("schema", "").endswith("us-gaap-2024.xsd")]
    check("xbrl presentation: the outside schema is counted once in `unresolved`",
          len(external) == 1 and external[0]["locators"] == len([l for l in nodes(r, "loc")
                                                                 if "us-gaap-2024.xsd" in l["href"]]), str(external))
    check("xbrl presentation: no calculation or label item comes with it",
          {i["node"] for i in r["items"]} <= {"roleRef", "arcroleRef", "extendedLink", "loc", "arc"}
          and all(i["file"] == "t-20251231_pre.xml" for i in r["items"]), str({i["file"] for i in r["items"]}))
    check("xbrl presentation: every file read is named with its hash",
          all(s.get("sha256") for s in r["sources"] if s["url"].startswith(FOLDER_URL))
          and any(s["url"].endswith("t-20251231_pre.xml") for s in r["sources"]), str(r["sources"]))

    r, _ = xbrl_call(filed(cash_flow="us-gaap_Cash"), part="presentation")
    twice = arcs_between(r, "us-gaap_StatementOfCashFlowsAbstract", "us-gaap_Cash")
    check("xbrl presentation: two arcs to one element are two items, labels as written, no balance reassigned",
          [a["attributes"]["preferredLabel"].rsplit("/", 1)[1] for a in twice] == ["periodStartLabel", "periodEndLabel"]
          and all("columns" not in i and "values" not in i for i in r["items"]), str(twice))

    r, code = xbrl_call(filed(prohibit=True), part="calculation")
    wrong = arcs_between(r, "us-gaap_OperatingIncomeLoss", "us-gaap_Cash")
    check("xbrl calculation: a prohibiting arc and the arc it prohibits are both returned, nothing applied",
          code is None and len(wrong) == 2 and wrong[1]["attributes"].get("use") == "prohibited"
          and wrong[1]["attributes"].get("priority") == "1" and "use" not in wrong[0]["attributes"], code or str(wrong))
    weights = [a["attributes"]["weight"] for a in arcs_between(r, "us-gaap_OperatingIncomeLoss", "us-gaap_CostsAndExpenses")]
    check("xbrl calculation: weights as written", weights == ["-1"], str(weights))
    check("xbrl calculation: no check is computed", all("consistent" not in i for i in r["items"])
          and "calculation_checks" not in r)
    r, _ = xbrl_call(filed(calc11=True), part="calculation")
    arcroles = {a["attributes"][f"{XL}arcrole"] for a in nodes(r, "arc")}
    check("xbrl calculation: a Calculation 1.1 arcrole is returned as written",
          arcroles == {"https://xbrl.org/2023/arcrole/summation-item"}, str(arcroles))

    r, _ = xbrl_call(filed(), part="definition")
    special = arcs_between(r, "us-gaap_CostsAndExpenses", "t_FulfillmentExpense")
    check("xbrl definition: every definition arc is returned, whatever its arcrole",
          len(special) == 1 and special[0]["attributes"][f"{XL}arcrole"].endswith("general-special"), str(special))

    folder = filed()
    folder["t-20251231_lab.xml"] = folder["t-20251231_lab.xml"].replace(
        b"</link:labelLink>",
        b'<link:label xlink:type="resource" xlink:label="l0" xml:lang="fr" '
        b'xlink:role="http://www.xbrl.org/2003/role/label">Ventes nettes</link:label></link:labelLink>')
    r, code = xbrl_call(folder, part="label")
    labels = nodes(r, "resource")
    check("xbrl label: every label in every language and role, in document order, none preferred",
          code is None and [(x["attributes"]["{http://www.w3.org/XML/1998/namespace}lang"], x["text"])
                            for x in labels if x["attributes"][f"{XL}label"] == "l0"]
          == [("en-US", "Net sales"), ("fr", "Ventes nettes")], code or str(labels)[:300])
    roles = {x["attributes"][f"{XL}role"].rsplit("/", 1)[1] for x in labels}
    check("xbrl label: label roles as written", roles == {"label", "totalLabel", "documentation"}, str(roles))
    check("xbrl label: no label chosen or attached to a concept", all("label" not in i for i in r["items"]))

    r, code = xbrl_call(filed(embedded=True), part="calculation")
    check("xbrl calculation: linkbases written inside the schema are read",
          code is None and r["linkbases"][1]["embedded_in"] == "t-20251231.xsd"
          and len(arcs_between(r, "us-gaap_CostsAndExpenses", "t_FulfillmentExpense")) == 1, code or str(r["linkbases"]))
    r, code = xbrl_call(filed(), part="reference")
    check("xbrl reference: a filing without reference links returns no items", code is None and r["items"] == []
          and r["page"]["total"] == 0, code or str(r["page"]))

    folder = filed()
    del folder["t-20251231_def.xml"]
    r, code = xbrl_call(folder, part="definition")
    check("xbrl definition: a referenced linkbase the filing lacks is unresolved, the others still read",
          code is None and [f["read"] for f in r["linkbases"]] == [True, True, True, False]
          and any(u.get("href") == "t-20251231_def.xml" for u in r["unresolved"]), code or str(r["linkbases"]))
    broken = filed()
    broken["t-20251231_pre.xml"] = b"<link:linkbase"
    _, code = xbrl_call(broken, part="presentation")
    check("xbrl presentation: an unreadable linkbase is UNSUPPORTED_DOCUMENT", code == "UNSUPPORTED_DOCUMENT", code)
    _, code = xbrl_call({}, part="schema")
    check("xbrl schema: a filing without XBRL is UNSUPPORTED_DOCUMENT", code == "UNSUPPORTED_DOCUMENT", code)
    _, code = xbrl_call(filed(), part="schema", accession_number="12-34")
    check("xbrl schema: a malformed accession number is INVALID_INPUT", code == "INVALID_INPUT", code)
    _, code = xbrl_call(filed(), part="balance_sheet")
    check("xbrl: an unknown part is INVALID_INPUT", code == "INVALID_INPUT", code)


def text_cases():
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/frozen-test.htm"
    real = server.fetch_response
    server.fetch_response = lambda u, **k: sec_client.Response(b"<p>Net sales rose.</p>", "text/html",
                                                               "2026-02-02T00:00:00Z", True)
    try:
        first, _ = call(server.get_filing_text, url=url)
        server.fetch_response = lambda u, **k: (_ for _ in ()).throw(AssertionError("read twice"))
        second, _ = call(server.get_filing_text, url=url, find="net sales")
        src1, src2 = first["sources"][0], second["sources"][0]
        check("text: a frozen document says so", src1["frozen"] is True and first["text"] == "Net sales rose.")
        check("text: and still says so when served from memory, with the same hash",
              src2["cache_hit"] and src2["frozen"] is True and src2["retrieved_at"] == "2026-02-02T00:00:00Z"
              and src2["sha256"] == src1["sha256"] == hashlib.sha256(b"<p>Net sales rose.</p>").hexdigest())
        check("text: the extraction is marked transformed and points at the original",
              first["representation"] == "transformed" and first["original"]["sha256"] == src1["sha256"])
        check("text: the answer says the text is the filer's, not instructions",
              "not instructions" in first["content"] and "not instructions" in second["content"])
    finally:
        server.fetch_response = real


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


# ── Round 2, blocker 1: no implicit filter or substitution ────────────────

def no_implicit_filter_cases():
    real = server.get_json
    asked = []

    def answer(url, *a, **k):
        asked.append(url)
        return page(1, [{"_id": "0000000001-24-000001:a.htm",
                         "_source": {"ciks": ["0000000001", "0000000002"]}}]), meta(url)
    server.get_json = answer
    try:
        result, code = call(server.search_filings, query="x")
        check("b1 search: without forms every form is searched (no forms= in the address, filters.forms null)",
              code is None and "forms=" not in asked[-1] and result["filters"]["forms"] is None, str(asked[-1:]))
        result, code = call(server.search_filings, query="x", forms="10-K, 8-K")
        check("b1 search: forms asked for are sent as given", code is None and "&forms=10-K%2C8-K" in asked[-1]
              and result["filters"]["forms"] == ["10-K", "8-K"], str(asked[-1:]))
        result, code = call(server.search_filings, query='  "peer   group"\tx ')
        check("b1 search: the query is sent and echoed exactly as given, whitespace included",
              code is None and asked[-1].split("?q=", 1)[1].split("&", 1)[0] == quote('  "peer   group"\tx ')
              and result["filters"]["query"] == '  "peer   group"\tx ', str(asked[-1:]))
        _, code = call(server.search_filings, query="   ")
        check("b1 search: a query of whitespace only is INVALID_INPUT", code == "INVALID_INPUT", code)
        result, code = call(server.search_filings, query="x")
        h = result["hits"][0]
        check("b1 search: a hit naming two CIKs gets no single url, and the address under each CIK, in order",
              h["url"] is None and h.get("urls") == [
                  "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/a.htm",
                  "https://www.sec.gov/Archives/edgar/data/2/000000000124000001/a.htm"], str(h))
        check("b1 search: and the answer says the hit's address is ambiguous",
              any(u.get("_id") == "0000000001-24-000001:a.htm" and u.get("ciks") == ["0000000001", "0000000002"]
                  for u in result.get("unresolved", [])), str(result.get("unresolved")))
    finally:
        server.get_json = real

    real = filing_retrieval.get_json
    recent = table(("0000000001-25-000003", "8-K", "2025-04-01"), ("0000000001-25-000001", "10-K", "2025-03-01"))
    filing_retrieval.get_json = lambda url, *a, **k: ({"filings": {"recent": recent, "files": []}}, meta(url))
    try:
        result, code = call(server.list_filings, cik_or_ticker="1")
        check("b1 list_filings: without form every form is kept",
              code is None and [f["record"]["form"] for f in result["filings"]] == ["8-K", "10-K"]
              and result["filters"]["form"] is None, code or str(result["filings"]))
    finally:
        filing_retrieval.get_json = real

    eight_k = {**RECORD, "accessionNumber": "0000000001-26-000009", "form": "8-K"}
    r, code = xbrl_call(filed(), records=[eight_k, RECORD])
    check("b1 xbrl: without form or accession number the first row of any form is the filing",
          code is None and r["filing"]["accessionNumber"] == "0000000001-26-000009", code or str(r["filing"]))
    undated = {**RECORD, "accessionNumber": "0000000001-26-000005", "filingDate": ""}
    r, code = xbrl_call(filed(), records=[undated, RECORD], form="10-K", as_of="2026-12-31")
    check("b1 xbrl: a row with no filingDate never counts as on or before as_of; it is skipped and named",
          code is None and r["filing"]["accessionNumber"] == "0000000001-26-000001"
          and any("0000000001-26-000005" in w for w in r["warnings"]), code or str(r and r["warnings"]))
    _, code = xbrl_call(filed(), records=[undated], accession_number="0000000001-26-000005", as_of="2026-12-31")
    check("b1 xbrl: an accession number with no filingDate cannot be placed against as_of and is refused",
          code == "NOT_FOUND", code)
    r, code = xbrl_call(filed(), records=[undated], accession_number="0000000001-26-000005")
    check("b1 xbrl: without as_of the same accession number is returned", code is None, code)


# ── Round 2, blocker 2: documents found only from published references ─────

def discovery_cases():
    folder = filed()
    folder["t-extracted.xml"] = folder.pop("t-20251231_htm.xml")
    folder["0000000001-26-000001-index.htm"] = index_page(
        [("1", "10-K", "t-20251231.htm", "10-K"), ("3", "SCHEMA", "t-20251231.xsd", "EX-101.SCH"),
         ("9", "<b>EXTRACTED</b> XBRL INSTANCE DOCUMENT", "t-extracted.xml", "XML")])
    r, code = xbrl_call(folder)
    check("b2: the instance is the document the index lists as the extracted instance, no name is derived",
          code is None and r["files"]["instance"].get("name") == "t-extracted.xml"
          and r["files"]["instance"].get("index_row", {}).get("type") == "XML",
          code or str(r["files"]["instance"]))
    r, code = xbrl_call(folder, part="instance", concepts=["us-gaap:Cash"])
    check("b2: and it is the file read for the instance", code is None and r["file"]["name"] == "t-extracted.xml",
          code)

    folder = filed()
    folder["FilingSummary.xml"] = folder["FilingSummary.xml"].replace(
        b"<File>t-20251231.xsd</File>", b"<File>t-20251231.xsd</File><File>t-other.xsd</File>")
    r, code = xbrl_call(folder)
    check("b2: with two schemas in InputFiles the schema is the instance's schemaRef",
          code is None and r["files"]["schema"].get("name") == "t-20251231.xsd"
          and "schemaRef" in r["files"]["schema"].get("found_by", ""), code or str(r and r["files"]["schema"]))

    folder = filed()
    folder["0000000001-26-000001-index.htm"] = index_page(
        [("1", "10-K", "t-20251231.htm", "10-K"), ("3", "SCHEMA", "t-20251231.xsd", "EX-101.SCH"),
         ("8", "EXTRACTED XBRL INSTANCE DOCUMENT", "t-20251231_htm.xml", "XML"),
         ("9", "EXTRACTED XBRL INSTANCE DOCUMENT", "t-b_htm.xml", "XML")])
    r, code = xbrl_call(folder)
    instance = r["files"]["instance"] if r else {}
    check("b2: two documents the index lists as the instance leave it unresolved, both named, none chosen",
          code is None and instance.get("resolved") is False
          and instance.get("candidates") == ["t-20251231_htm.xml", "t-b_htm.xml"]
          and any(u.get("file") == "instance" for u in r["unresolved"]), code or str(instance))
    check("b2: and no folder listing is read to settle it",
          r is not None and not any(s["url"].endswith("index.json") for s in r["sources"]), str(r and r["sources"]))
    _, code = xbrl_call(folder, part="instance")
    check("b2: the instance part of such a filing is refused, not guessed", code == "UNSUPPORTED_DOCUMENT", code)
    r, code = xbrl_call(folder, part="instance", instance_file="t-20251231_htm.xml", concepts=["us-gaap:Cash"])
    check("b2: the caller can name the instance file", code is None and r["file"]["name"] == "t-20251231_htm.xml"
          and r["file"]["found_by"].startswith("named by the caller"), code or str(r and r["file"]))
    _, code = xbrl_call(folder, part="instance", instance_file="../x.xml")
    check("b2: a named file must be a plain file name of the folder", code == "INVALID_INPUT", code)

    folder = filed(inline=False)
    folder["0000000001-26-000001-index.htm"] = index_page([("1", "10-K", "t-20251231.htm", "10-K")])
    r, code = xbrl_call(folder)
    check("b2: a file named like an instance is not picked by its name when the index lists none",
          code is None and r["files"]["instance"]["resolved"] is False
          and r["files"]["instance"].get("candidates") == [], code or str(r and r["files"]["instance"]))
    check("b2: nor is the one .xsd in InputFiles picked by its suffix when nothing names it",
          r is not None and r["files"]["schema"]["resolved"] is False, str(r and r["files"]["schema"]))

    ref = b'<link:schemaRef xlink:type="simple" xlink:href="t-20251231.xsd"/>'
    folder = filed()
    folder["t-20251231_htm.xml"] = folder["t-20251231_htm.xml"].replace(
        ref, ref + b'<link:schemaRef xlink:type="simple" xlink:href="t-other.xsd"/>')
    r, code = xbrl_call(folder)
    check("b2: two schemaRefs in the instance leave the schema unresolved, both named",
          code is None and r["files"]["schema"]["resolved"] is False and len(r["files"]["schema"]["candidates"]) == 2
          and any(u.get("file") == "schema" for u in r["unresolved"]), code or str(r and r["files"]["schema"]))
    r, code = xbrl_call(folder, part="schema", schema_file="t-20251231.xsd")
    check("b2: the caller can name the schema file", code is None and r["file"]["name"] == "t-20251231.xsd"
          and r["file"]["found_by"] == "named by the caller (schema_file)", code or str(r and r["file"]))
    _, code = xbrl_call(folder, part="schema", schema_file="t-missing.xsd")
    check("b2: a named file the folder does not have is NOT_FOUND", code == "NOT_FOUND", code)

    folder = filed()
    folder["t-20251231_htm.xml"] = folder["t-20251231_htm.xml"].replace(
        b"<xbrli:xbrl ", b'<xbrli:xbrl xml:base="https://example.com/taxonomy/" ', 1)
    r, code = xbrl_call(folder)
    check("b2: the schemaRef is resolved against the inherited xml:base, so here it is outside the filing",
          code is None and r["files"]["schema"]["resolved"] is False
          and r["files"]["schema"]["candidates"] == ["https://example.com/taxonomy/t-20251231.xsd"],
          code or str(r and r["files"]["schema"]))

    folder = filed()
    del folder["FilingSummary.xml"]
    r, code = xbrl_call(folder)
    check("b2: a filing without FilingSummary.xml still answers, and says exactly that file is missing",
          code is None and r["filing_summary"] is None
          and any(u.get("file") == "FilingSummary.xml" and "no XBRL" not in u["reason"] for u in r["unresolved"]),
          code or str(r and r["unresolved"]))
    r, code = xbrl_call(folder, part="instance", concepts=["us-gaap:Cash"])
    check("b2: and its instance is still found from the index", code is None
          and r["file"]["name"] == "t-20251231_htm.xml", code)


# ── Round 2, blocker 3: every dependency followed, or named ──────────────────

LB_NS = ('xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"')
SCHEMA_OPEN = b'targetNamespace="http://t.example/2025">'


def label_linkbase(href, text):
    """A label linkbase with one label for the element at `href`."""
    return (f'<link:linkbase {LB_NS}><link:labelLink xlink:type="extended" '
            'xlink:role="http://www.xbrl.org/2003/role/link">'
            f'<link:loc xlink:type="locator" xlink:href="{href}" xlink:label="c"/>'
            '<link:label xlink:type="resource" xlink:label="r" xml:lang="en-US" '
            f'xlink:role="http://www.xbrl.org/2003/role/label">{text}</link:label>'
            '<link:labelArc xlink:type="arc" xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label" '
            'xlink:from="c" xlink:to="r"/></link:labelLink></link:linkbase>').encode()


def with_include(folder, name="t-extra.xsd"):
    folder["t-20251231.xsd"] = folder["t-20251231.xsd"].replace(
        SCHEMA_OPEN, SCHEMA_OPEN + f'<xs:include schemaLocation="{name}"/>'.encode(), 1)
    return folder


def dependency_cases():
    folder = with_include(filed())
    folder["t-extra.xsd"] = (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" {LB_NS} '
        'xmlns:xbrli="http://www.xbrl.org/2003/instance" targetNamespace="http://t.example/2025">'
        '<xs:include schemaLocation="t-20251231.xsd"/>'
        '<xs:annotation><xs:appinfo><link:linkbaseRef xlink:type="simple" xlink:href="t-extra_lab.xml" '
        'xlink:arcrole="http://www.w3.org/1999/xlink/properties/linkbase"/></xs:appinfo></xs:annotation>'
        '<xs:element id="t_Extra" name="Extra" type="xbrli:monetaryItemType" substitutionGroup="xbrli:item" '
        'xbrli:periodType="instant"/></xs:schema>').encode()
    folder["t-extra_lab.xml"] = label_linkbase("t-extra.xsd#t_Extra", "Extra line")
    r, code = xbrl_call(folder, part="schema")
    extra = [i for i in (r["items"] if r else []) if i.get("file") == "t-extra.xsd" and i["node"] == "element"]
    check("b3: the schema part follows a local xs:include and returns its declarations, by file",
          code is None and [i["name"] for i in extra] == ["Extra"], code or str(r and r["items"][-3:]))
    check("b3: each schema is read once though they include each other",
          r is not None and [x["name"] for x in r.get("schemas", [])] == ["t-20251231.xsd", "t-extra.xsd"],
          str(r and r.get("schemas")))
    r, code = xbrl_call(folder, part="label")
    mine = [i for i in (r["items"] if r else []) if i.get("file") == "t-extra_lab.xml"]
    loc = [i for i in mine if i["node"] == "loc"]
    check("b3: a label part reads the linkbase an included schema references",
          code is None and loc and loc[0]["target"].get("resolved") is True
          and loc[0]["target"].get("local_name") == "Extra" and loc[0]["target"].get("file") == "t-extra.xsd"
          and any(i["node"] == "resource" and i["text"] == "Extra line" for i in mine), code or str(mine))
    check("b3: and says which file referenced it",
          r is not None and any(f.get("name") == "t-extra_lab.xml" and f["discovered_by"]["file"] == "t-extra.xsd"
                                for f in r["linkbases"]), str(r and r["linkbases"]))

    folder = filed()
    ref = b'<link:schemaRef xlink:type="simple" xlink:href="t-20251231.xsd"/>'
    folder["t-20251231_htm.xml"] = folder["t-20251231_htm.xml"].replace(
        ref, ref + b'<link:linkbaseRef xlink:type="simple" xlink:href="t-inst_lab.xml" '
                   b'xlink:arcrole="http://www.w3.org/1999/xlink/properties/linkbase"/>')
    folder["t-inst_lab.xml"] = label_linkbase("t-20251231.xsd#t_LeasedAssetsNet", "Vehicles (instance linkbase)")
    r, code = xbrl_call(folder, part="label")
    check("b3: a linkbase the instance references (link:linkbaseRef) is read",
          code is None and any(i.get("file") == "t-inst_lab.xml" and i.get("text") == "Vehicles (instance linkbase)"
                               for i in r["items"]), code)

    folder = with_include(filed(), "t-gone.xsd")
    for part in ("schema", "label"):
        r, code = xbrl_call(folder, part=part)
        check(f"b3: a local schema that is missing is named in unresolved ({part})",
              code is None and any(str(u.get("url", "")).endswith("/t-gone.xsd") for u in r["unresolved"]),
              code or str(r and r["unresolved"]))

    folder = filed()
    folder["t-20251231.xsd"] = folder["t-20251231.xsd"].replace(SCHEMA_OPEN, SCHEMA_OPEN + (
        b'<xs:import namespace="http://fasb.org/us-gaap/2024" '
        b'schemaLocation="https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd"/>'), 1)
    r, code = xbrl_call(folder, part="presentation")
    check("b3: a linkbase part names the imported schemas it did not read",
          code is None and any(u.get("schemaLocation") == "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd"
                               for u in r["unresolved"]), code or str(r and r["unresolved"]))

    folder = filed()
    folder["0000000001-26-000001-index.htm"] = index_page([("1", "10-K", "t-20251231.htm", "10-K")])
    r, code = xbrl_call(folder, part="definition", schema_file="t-20251231.xsd")
    check("b3: with the instance unresolved, its linkbase references are named as not read",
          code is None and any("linkbaseRef" in u.get("reason", "") for u in r["unresolved"]),
          code or str(r and r["unresolved"]))

    folder = filed()
    folder["t-20251231.xsd"] = folder["t-20251231.xsd"].replace(b't-20251231_lab.xml', b't-missing_lab.xml')
    r, code = xbrl_call(folder, part="label")
    check("b3: a label part whose only label linkbase is missing returns no items, and never an empty unresolved",
          code is None and r["items"] == [] and r["unresolved"], code or str(r and r["unresolved"]))


# ── Round 2, blocker 4: locators against the effective base, no first-wins ──

def locator_cases():
    folder = filed()
    folder["t-20251231_pre.xml"] = folder["t-20251231_pre.xml"].replace(
        b"<link:linkbase ", b'<link:linkbase xml:base="https://example.com/elsewhere/" ', 1)
    r, code = xbrl_call(folder, part="presentation")
    own = [l for l in (nodes(r, "loc") if r else []) if l["href"] == "t-20251231.xsd#t_FulfillmentExpense"]
    check("b4: a locator is resolved against the linkbase's xml:base, not the file's URL",
          code is None and own and own[0]["target"]["resolved"] is False
          and own[0]["target"].get("schema") == "https://example.com/elsewhere/t-20251231.xsd"
          and own[0].get("url") == "https://example.com/elsewhere/t-20251231.xsd#t_FulfillmentExpense",
          code or str(own))

    folder = filed()
    folder["t-20251231_cal.xml"] = folder["t-20251231_cal.xml"].replace(
        b"<link:calculationLink ", f'<link:calculationLink xml:base="{FOLDER_URL}" '.encode(), 1).replace(
        b"<link:linkbase ", b'<link:linkbase xml:base="https://example.com/ignored/" ', 1)
    r, code = xbrl_call(folder, part="calculation")
    first_link = [l for l in (nodes(r, "loc") if r else []) if l["href"] == "t-20251231.xsd#t_FulfillmentExpense"]
    check("b4: the innermost xml:base wins over the outer one (inherited, then overridden)",
          code is None and first_link and first_link[0]["target"]["resolved"] is True, code or str(first_link))

    folder = filed()
    folder["t-20251231.xsd"] = folder["t-20251231.xsd"].replace(b"</xs:schema>", (
        b'<xs:element id="t_FulfillmentExpense" name="FulfillmentExpenseAgain" type="xbrli:monetaryItemType" '
        b'substitutionGroup="xbrli:item" xbrli:periodType="duration"/></xs:schema>'), 1)
    r, code = xbrl_call(folder, part="presentation")
    own = [l for l in (nodes(r, "loc") if r else []) if l["href"] == "t-20251231.xsd#t_FulfillmentExpense"]
    target = own[0]["target"] if own else {}
    check("b4: a locator to an id two schema elements share is ambiguous: resolved false, both named",
          code is None and target.get("resolved") is False and target.get("ambiguous") is True
          and [c["local_name"] for c in target.get("candidates", [])] == ["FulfillmentExpense",
                                                                           "FulfillmentExpenseAgain"],
          code or str(target))
    check("b4: and the shared id is in unresolved", r is not None
          and any(u.get("id") == "t_FulfillmentExpense" for u in r["unresolved"]), str(r and r["unresolved"]))


# ── Round 2, blocker 5: parsed responses keep what was written, or say what they leave out ──

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
LINK_NS = "{http://www.xbrl.org/2003/linkbase}"


def lossless_cases():
    inst = "t-20251231_htm.xml"
    folder = filed()
    folder[inst] = (folder[inst]
                    .replace(b'xmlns:dei="http://xbrl.sec.gov/dei/2024">',
                             b'xmlns:dei="http://xbrl.sec.gov/dei/2024" xml:lang="en-US" id="root1">', 1)
                    .replace(b'<xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">',
                             b'<xbrli:entity id="ent"><xbrli:identifier scheme="http://www.sec.gov/CIK" id="ident">', 1)
                    .replace(b'<xbrli:period><xbrli:startDate>', b'<xbrli:period id="per"><xbrli:startDate id="sd">', 1)
                    .replace(b'</xbrli:period></xbrli:context>',
                             b'</xbrli:period><xbrli:scenario id="scen"><t:Note>x</t:Note></xbrli:scenario>'
                             b'</xbrli:context>', 1)
                    .replace(b"<xbrli:segment>", b'<xbrli:segment id="seg">', 1))
    r, code = xbrl_call(folder, part="instance")
    items = (r or {}).get("items", [])
    d25 = next((i for i in items if i.get("id") == "D25"), {})
    v25 = next((i for i in items if i.get("id") == "V25"), {})
    doc_type = next((i for i in items if i["node"] == "fact" and i["concept"]["local_name"] == "DocumentType"), {})
    check("b5: the instance response gives the document element's attributes",
          code is None and (r.get("root") or {}).get("attributes") == {XML_LANG: "en-US", "id": "root1"},
          code or str(r.get("root")))
    check("b5: a fact keeps the xml:lang inherited from the document element",
          doc_type.get("lang") == {"value": "en-US", "seq": 0}, str(doc_type))
    entity = d25.get("entity") or {}
    check("b5: every attribute of the context's children is in the response",
          entity.get("attributes") == {"id": "ent"} and (entity.get("identifier_attributes") or {}).get("id") == "ident"
          and (d25.get("period_element") or {}).get("attributes") == {"id": "per"}
          and (d25.get("period") or [{}])[0].get("attributes") == {"id": "sd"}
          and (d25.get("scenario") or {}).get("attributes") == {"id": "scen"}
          and "<t:Note>x</t:Note>" in ((d25.get("scenario") or {}).get("other") or [{"xml": ""}])[0]["xml"]
          and (v25.get("segment") or {}).get("attributes") == {"id": "seg"}, str(d25))
    check("b5: the instance response says what it does not return",
          r is not None and any("comment" in n for n in r.get("not_returned") or []), str(r and r.get("not_returned")))

    folder = filed()
    folder["t-20251231.xsd"] = (folder["t-20251231.xsd"]
                                .replace(SCHEMA_OPEN, b'targetNamespace="http://t.example/2025" '
                                         b'elementFormDefault="qualified"><xs:annotation><xs:documentation>'
                                         b'The company schema.</xs:documentation></xs:annotation>', 1)
                                .replace(b'<xs:element id="t_LeasedAssetsNet"',
                                         b'<xs:element xmlns:q="http://q.example" id="t_LeasedAssetsNet"', 1)
                                .replace(b"</xs:appinfo>", b'<link:linkbaseRef xlink:type="simple" '
                                         b'xlink:href="t-roles.xml"/></xs:appinfo>', 1))
    folder["t-roles.xml"] = (f'<link:linkbase {LB_NS}><link:roleRef roleURI="http://t.example/role/Income" '
                             'xlink:type="simple" xlink:href="t-20251231.xsd#IS"/></link:linkbase>').encode()
    folder["t-20251231_lab.xml"] = (folder["t-20251231_lab.xml"]
                                    .replace(b"<link:linkbase ", b'<link:linkbase xml:lang="fr" ', 1)
                                    .replace(b' xml:lang="en-US"', b"", 1)
                                    .replace(b"<link:labelLink ", b'<link:labelLink xmlns:q="http://q.example" ', 1))
    r, code = xbrl_call(folder, part="schema")
    main = ((r or {}).get("schemas") or [{}])[0]
    check("b5: the schema response gives each schema's document element with its attributes",
          code is None and (main.get("root") or {}).get("attributes", {}).get("elementFormDefault") == "qualified",
          code or str(main))
    check("b5: the schema's annotation documentation is returned",
          any(i["node"] == "annotation" and (i.get("documentation") or [{}])[0].get("text") == "The company schema."
              for i in nodes(r, "annotation")), str(nodes(r, "annotation")))
    check("b5: the schema response lists namespace declarations below the document element",
          [d["prefix"] for d in (main.get("namespace_declarations") or []) if d["seq"]] == ["q"], str(main))
    check("b5: the schema response says what it does not return",
          r is not None and bool(r.get("not_returned")), str(r and r.get("not_returned")))

    r, code = xbrl_call(folder, part="label")
    lab = next((e for e in (r or {}).get("linkbases", []) if e.get("name") == "t-20251231_lab.xml"), {})
    first, second = (nodes(r, "resource") + [{}, {}])[:2]
    check("b5: a label without xml:lang carries the language inherited from its linkbase",
          code is None and XML_LANG not in first.get("attributes", {XML_LANG: 1})
          and first.get("lang") == {"value": "fr", "seq": 0}
          and (second.get("lang") or {}).get("value") == "en-US", code or str((first, second)))
    check("b5: a linkbase entry gives its document element and its scoped namespace declarations",
          (lab.get("root") or {}).get("attributes") == {XML_LANG: "fr"}
          and [d["prefix"] for d in (lab.get("namespace_declarations") or []) if d["seq"]] == ["q"], str(lab))
    check("b5: a linkbase holding only roleRefs returns them",
          [(i["file"], i["href"]) for i in nodes(r, "roleRef") if i["file"] == "t-roles.xml"]
          == [("t-roles.xml", "t-20251231.xsd#IS")], str(nodes(r, "roleRef")))
    r, code = xbrl_call(folder, part="presentation")
    lab = next((e for e in (r or {}).get("linkbases", []) if e.get("name") == "t-20251231_lab.xml"), {})
    check("b5: each linkbase entry counts its extended links of every element, so what this part leaves out is named",
          code is None and lab.get("extended_links") == 0
          and lab.get("extended_links_by_element") == {f"{LINK_NS}labelLink": 1}, code or str(lab))
    check("b5: and the linkbase response says what it does not return",
          r is not None and any("other elements" in n for n in r.get("not_returned") or []),
          str(r and r.get("not_returned")))


if __name__ == "__main__":
    no_implicit_filter_cases()
    discovery_cases()
    dependency_cases()
    locator_cases()
    lossless_cases()
    search_cases()
    client_cases()
    lookup_cases()
    history_cases()
    instance_cases()
    xbrl_link_cases()
    text_cases()
    path_cases()
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
