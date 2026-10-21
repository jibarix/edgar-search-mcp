"""Record the SEC answers the offline tests replay, and the 0.8.0 tool outputs.

Not a test: run once, by hand, from the repository root, with
EDGAR_USER_AGENT set. It makes about thirty live requests to the SEC through
the server's own client (rate limit and User-Agent included):
    python -X utf8 tests/capture_fixtures.py

Every response is saved as a frozen copy under tests/fixtures/sec (the
EDGAR_RECORD_DIR machinery of sec_client). Each tool output is saved as
JSON under tests/fixtures/outputs_0.8.0, so the behaviour before the 1.0
contract stays on record. tests/test_frozen.py replays both offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC_DIR = os.path.join(ROOT, "tests", "fixtures", "sec")
OUT_DIR = os.path.join(ROOT, "tests", "fixtures", "outputs_0.8.0")

# The calls, by name. Winmark Corporation (CIK 908315) is small enough to keep in the repository.
CIK = "908315"
CONCEPT = ("us-gaap", "Revenues")
FRAME = "https://data.sec.gov/api/xbrl/frames/us-gaap/SalesTypeLeaseSellingProfitLoss/USD/CY2023.json"
# No such concept: the SEC answers 404, kept as a frozen "not found".
MISSING_FRAME = "https://data.sec.gov/api/xbrl/frames/us-gaap/FranchiseRevenue/USD/CY2023.json"


def main():
    if not os.environ.get("EDGAR_USER_AGENT"):
        sys.exit("set EDGAR_USER_AGENT first")
    os.environ["EDGAR_RECORD_DIR"] = SEC_DIR
    os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_capture_")
    os.environ.pop("EDGAR_FROZEN_DIR", None)
    sys.path.insert(0, ROOT)
    from edgar_mcp import server
    from edgar_mcp.utils import sec_client
    os.makedirs(OUT_DIR, exist_ok=True)

    def run(name, tool, **kwargs):
        try:
            result = asyncio.run(tool(**kwargs))
        except Exception as e:  # an error is an output too
            result = {"error": str(e)}
        with open(os.path.join(OUT_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump({"tool": tool.__name__, "arguments": kwargs, "result": result}, f, indent=1, sort_keys=True)
        print(name, "error" if "error" in result else "ok")
        return result

    run("lookup_company_ticker", server.lookup_company, query="WINA")
    run("lookup_company_name", server.lookup_company, query="Winmark Corp")
    run("get_financial_statement_is", server.get_financial_statement, cik_or_ticker=CIK, statement_type="IS",
        period_type="annual", num_periods=3)
    run("get_concept", server.get_concept, cik_or_ticker=CIK, taxonomy=CONCEPT[0], concept=CONCEPT[1], limit=20)
    filings = run("list_filings_10k", server.list_filings, cik_or_ticker=CIK, form="10-K", limit=3)
    main_doc = filings["filings"][0]["url"]
    documents = run("list_filing_documents", server.list_filing_documents, url=main_doc)
    accession = filings["filings"][0]["accession_number"]
    run("get_filed_statements", server.get_filed_statements, cik_or_ticker=CIK, form="10-K",
        accession_number=accession, facts=["us-gaap:Revenues", "dei:EntityCommonStockSharesOutstanding"])
    run("get_segment_revenue", server.get_segment_revenue, cik_or_ticker=CIK)
    exhibit = next(d["url"] for d in documents["documents"]
                   if d["readable"] and (d["type"] or "").startswith("EX-") and (d["size_bytes"] or 0) < 200_000)
    run("get_filing_text_page", server.get_filing_text, url=exhibit, max_chars=2000)
    run("get_filing_text_find", server.get_filing_text, url=exhibit, find="Winmark")
    run("search_filings", server.search_filings, query='"Winmark"', forms="10-K",
        filed_from="2024-01-01", filed_to="2024-12-31", limit=5)
    run("search_companies_sic", server.search_companies, sic="6794", limit=5)
    # The raw answers the 1.0 raw tools read, recorded beside the rest.
    sec_client.fetch_response(f"https://data.sec.gov/submissions/CIK{int(CIK):010d}.json")
    sec_client.fetch_response(FRAME)
    try:
        sec_client.fetch_response(MISSING_FRAME)
    except sec_client.SecNotFound:
        pass
    with open(os.path.join(OUT_DIR, "_about.json"), "w", encoding="utf-8") as f:
        json.dump({"version": server.__version__, "cik": CIK, "accession_number": accession,
                   "main_document": main_doc, "exhibit": exhibit, "frame": FRAME,
                   "missing_frame": MISSING_FRAME}, f, indent=1)


if __name__ == "__main__":
    main()
