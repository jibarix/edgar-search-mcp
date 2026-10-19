"""Start the MCP server over stdio and call every tool against live EDGAR.

Run from the repository root with EDGAR_USER_AGENT set:
    python -X utf8 tests/test_mcp_stdio.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from edgar_mcp import __version__  # noqa: E402


def payload(result):
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) else data
    return json.loads(result.content[0].text)


async def main():
    if not os.environ.get("EDGAR_USER_AGENT"):
        print('set EDGAR_USER_AGENT to your name and email first, e.g. "Jane Doe jane@example.com"')
        return 1
    # An MCP client passes only a few variables to the server unless told otherwise.
    server = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT,
                                   env=dict(os.environ))
    problems = []

    def check(label, ok, detail=""):
        print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
        if not ok:
            problems.append(label)

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            started = await session.initialize()
            check("server reports its version", started.server_info.version == __version__,
                  f"{started.server_info.name} {started.server_info.version}")

            async def call(tool, **arguments):
                return payload(await session.call_tool(tool, arguments))

            async def refused(label, code, tool, **arguments):
                result = await session.call_tool(tool, arguments)
                text = result.content[0].text if result.content else ""
                check(label, result.is_error and code in text, text[:110])

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            check("tools", names == ["get_concept", "get_filed_statements", "get_filing_text", "get_financial_statement", "get_segment_revenue",
                                     "list_filing_documents", "list_filings", "lookup_company", "search_companies",
                                     "search_filings"], str(names))
            check("tools are marked read-only", all(t.annotations and t.annotations.read_only_hint for t in tools))

            found = await call("lookup_company", query="AAPL")
            check("lookup_company", found["match_type"] == "ticker" and found["matches"][0]["cik"] == "0000320193",
                  found["matches"][0]["name"])
            loose = await call("lookup_company", query="Apple Hosp")
            check("lookup_company marks a loose match", loose["match_type"] in ("prefix", "fuzzy", "none"),
                  loose["match_type"])
            await refused("a loose name is not resolved to a company", "AMBIGUOUS_COMPANY",
                          "get_financial_statement", cik_or_ticker="American", statement_type="IS")
            await refused("an unknown CIK is NOT_FOUND", "NOT_FOUND",
                          "get_financial_statement", cik_or_ticker="9999999999", statement_type="IS")

            now = await call("get_financial_statement", cik_or_ticker="AAPL", statement_type="IS",
                             period_type="annual", num_periods=2)
            then = await call("get_financial_statement", cik_or_ticker="AAPL", statement_type="IS",
                              period_type="annual", num_periods=2, as_of="2023-06-30")
            check("get_financial_statement", bool(now.get("periods")) and now["source_url"].endswith(".json")
                  and bool(now.get("retrieved_at")), str(now.get("periods")))
            check("as_of", then["periods"][0] == "2022-09-24", str(then["periods"]))
            late = [(key, period) for key, metric in then["metrics"].items()
                    for period, src in metric["sources"].items()
                    for filed in [src["filed"]] + [src[part]["filed"] for part in ("cumulative", "minus") if part in src]
                    if not filed or filed > "2023-06-30"]
            check("as_of holds for every source", not late, str(late[:3]))
            await refused("a malformed as_of is refused", "INVALID_INPUT", "get_financial_statement",
                          cik_or_ticker="AAPL", statement_type="IS", as_of="2023-6-30")
            await refused("num_periods is bounded", "INVALID_INPUT", "get_financial_statement",
                          cik_or_ticker="AAPL", statement_type="IS", num_periods=0)

            interim = await call("get_financial_statement", cik_or_ticker="AAPL", statement_type="CF",
                                 period_type="quarterly", num_periods=4)
            derived = [src for metric in interim["metrics"].values() for src in metric["sources"].values()
                       if src["derived"]]
            check("derived cash-flow quarters carry both figures",
                  bool(derived) and all("cumulative" in s and "minus" in s for s in derived), f"{len(derived)} derived")

            shares = await call("get_concept", cik_or_ticker="AAPL", concept="EntityCommonStockSharesOutstanding",
                                taxonomy="dei", as_of="2023-12-31", limit=3)
            check("get_concept", shares["unit"] == "shares" and len(shares["facts"]) == 3 and shares["truncated"]
                  and all(row["filed"] <= "2023-12-31" for row in shares["facts"]),
                  f"{shares['facts'][0]['end']} {shares['facts'][0]['val']:,}")
            await refused("an unknown concept is NOT_FOUND", "NOT_FOUND", "get_concept",
                          cik_or_ticker="AAPL", concept="NoSuchConceptAtAll")

            listed = await call("list_filings", cik_or_ticker="AAPL", form="10-K", as_of="2023-12-31", limit=2)
            first = listed["filings"][0]
            check("list_filings", listed["complete"] and len(listed["filings"]) == 2
                  and all(row["filing_date"] <= "2023-12-31" for row in listed["filings"])
                  and first["url"].endswith(".htm"), f"{first['filing_date']} {first['url']}")
            old = await call("list_filings", cik_or_ticker="AAPL", form="10-K", as_of="2005-12-31", limit=2)
            check("list_filings reaches the older files", old["count"] == 2 and old["filings"][0]["filing_date"] <= "2005-12-31",
                  str([row["filing_date"] for row in old["filings"]]))
            await refused("limit is bounded", "INVALID_INPUT", "list_filings", cik_or_ticker="AAPL", limit=0)

            hits = await call("get_filing_text", url=first["url"], find="geographic")
            check("get_filing_text find", hits["match_count"] > 0 and len(hits["sha256"]) == 64,
                  f"{hits['match_count']} matches in {hits['total_chars']} chars")
            page = await call("get_filing_text", url=first["url"], start=hits["matches"][0]["start"], max_chars=300)
            check("get_filing_text page", "eographic" in page["text"], repr(page["text"][:80]))
            await refused("get_filing_text refuses other hosts", "INVALID_INPUT",
                          "get_filing_text", url="https://example.com/x.htm")
            await refused("get_filing_text refuses a path out of the archive", "INVALID_INPUT", "get_filing_text",
                          url="https://www.sec.gov/Archives/edgar/data/../../../../outside.htm")
            await refused("a missing document is NOT_FOUND", "NOT_FOUND", "get_filing_text",
                          url="https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/nosuchfile.htm")

            dealers = await call("search_companies", sic="5500", limit=5)
            check("search_companies", dealers["count"] > 0 and "revenue_country" not in dealers["results"][0]
                  and bool(dealers["index_built"]), f"{dealers['count']} rows, index built {dealers['index_built']}")

            found = await call("search_filings", query='"Coca-Cola" "PepsiCo" competitors', forms="10-K",
                               filed_from="2025-01-01", filed_to="2025-12-31", limit=5)
            hit = found["hits"][0] if found["hits"] else {}
            check("search_filings", found["total_hits"] > 0 and found["count"] == 5 and hit.get("readable")
                  and hit["url"].startswith("https://www.sec.gov/Archives/edgar/data/") and hit["companies"][0]["cik"],
                  f"{found['total_hits']} hits, first {hit.get('companies')} {hit.get('url')}")
            text = await call("get_filing_text", url=hit["url"], find="PepsiCo")
            check("search_filings hit is readable by get_filing_text", text["match_count"] > 0, str(text.get("match_count")))
            await refused("search_filings needs both dates", "INVALID_INPUT", "search_filings", query="competitors",
                          filed_from="2025-01-01")

            parts = await call("get_segment_revenue", cik_or_ticker="MSFT", as_of="2025-12-31")
            rows = parts["axes"]["business_segments"]
            check("get_segment_revenue", parts["fiscal_year"]["end"] == "2025-06-30" and len(rows) == 3
                  and abs(sum(r["value"] for r in rows) - parts["total_revenue"]["value"]) < 1,
                  f"{[(r['label'], r['value']) for r in rows]} total {parts['total_revenue']}")
            current = await call("list_filings", cik_or_ticker="MSFT", form="8-K", limit=5)
            check("list_filings gives the items of a current report", all(f["items"] for f in current["filings"]),
                  str([f["items"] for f in current["filings"]]))

            # Deere's earnings 8-K of 2026-08-20: a press release and a presentation as exhibits 99.1 and 99.2.
            earnings = "https://www.sec.gov/Archives/edgar/data/315189/000110465926098904/de-20260820x8k.htm"
            docs = await call("list_filing_documents", url=earnings)
            by_type = {d["type"]: d for d in docs["documents"]}
            check("list_filing_documents", docs["accession_number"] == "0001104659-26-098904" and docs["form"] == "8-K"
                  and docs["filing_date"] == "2026-08-20" and {"8-K", "EX-99.1", "EX-99.2"} <= set(by_type)
                  and by_type["8-K"]["url"] == earnings and not docs["warnings"],
                  f"{docs['count']} documents: {sorted(by_type, key=str)}")
            check("images are listed but not readable", all(not d["readable"] for d in docs["documents"]
                                                          if d["type"] == "GRAPHIC"))
            release = await call("get_filing_text", url=by_type["EX-99.1"]["url"], find="net income")
            check("an exhibit is read with get_filing_text", release["match_count"] > 0,
                  f"{release['match_count']} matches in {release['total_chars']} chars")
            await refused("list_filing_documents refuses other hosts", "INVALID_INPUT",
                          "list_filing_documents", url="https://example.com/Archives/edgar/data/1/000000000000000000/x.htm")
            await refused("list_filing_documents needs a filing folder", "INVALID_INPUT",
                          "list_filing_documents", url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany")

    # A server started without EDGAR_USER_AGENT sends nothing to the SEC.
    anonymous = {k: v for k, v in os.environ.items() if k != "EDGAR_USER_AGENT"}
    anonymous["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
    server = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT,
                                   env=anonymous)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_filings", {"cik_or_ticker": "0000320193"})
            text = result.content[0].text if result.content else ""
            check("without EDGAR_USER_AGENT the SEC is not called", result.is_error and "NO_USER_AGENT" in text,
                  text[:110])

    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
