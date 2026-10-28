"""get_filing_document's extra addresses (plan v5, change 1): the exchange
ticker file and the submissions files, byte for byte, and nothing else.

No network: every request goes to a stand-in transport. Run from the repository root:
    python -X utf8 tests/test_urls.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
os.environ["EDGAR_RATE_FILE"] = os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "state")
os.environ.pop("EDGAR_FROZEN_DIR", None)
os.environ.pop("EDGAR_RECORD_DIR", None)

import httpx  # noqa: E402

from edgar_mcp import server  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def call(tool, **kwargs):
    try:
        return asyncio.run(tool(**kwargs)), None
    except Exception as e:
        text = str(e)
        return None, text.split(":", 1)[0] if ":" in text else type(e).__name__


sec_client.USER_AGENT = "test test@example.com"
seen = []
BODIES = {
    "https://www.sec.gov/files/company_tickers_exchange.json": b'{"fields":["cik","name","ticker","exchange"],"data":['
    + b",".join(b'[%d,"Co %d \xc3\xa9","T%d","NYSE"]' % (i, i, i) for i in range(120)) + b"]}",
    "https://data.sec.gov/submissions/CIK0000047111.json": b'{"cik":"47111","name":"x","pad":"' + b"a" * 2500 + b'"}',
    "https://data.sec.gov/submissions/CIK0000047111-submissions-001.json": b'{"accessionNumber":["' + b"0" * 2600 + b'"]}',
}


def handler(request):
    seen.append(str(request.url))
    if str(request.url) == "https://data.sec.gov/submissions/CIK0000000301.json":
        return httpx.Response(301, headers={"Location": "https://data.sec.gov/submissions/CIK0000000302.json"})
    body = BODIES.get(str(request.url))
    if body is None:
        return httpx.Response(404)
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


sec_client._client = httpx.Client(transport=httpx.MockTransport(handler))

# T1: each form, paged at 1,000 bytes, reassembles to the bytes and hash it reports.
for url, body in BODIES.items():
    pages, offset, first, error = [], 0, None, None
    while offset is not None and len(pages) < 20:
        result, error = call(server.get_filing_document, url=url, offset=offset, max_bytes=1000)
        if error:
            break
        first = first or result
        pages.append(base64.b64decode(result["data_base64"]))
        offset = result["next_offset"]
    data = b"".join(pages)
    check(f"T1 {url.rsplit('/', 1)[1]} is returned byte for byte in pages of 1,000",
          not error and data == body and len(pages) == -(-len(body) // 1000), error or f"{len(pages)} pages")
    check(f"T1 {url.rsplit('/', 1)[1]} reports the whole file's sha256 and size",
          not error and first["sha256"] == hashlib.sha256(body).hexdigest() == hashlib.sha256(data).hexdigest()
          and first["total_bytes"] == len(body) and first["url"] == url, error or "")

# T2: every other form is refused before any request.
REFUSED = [
    "http://www.sec.gov/files/company_tickers_exchange.json",
    "https://sec.gov/files/company_tickers_exchange.json",
    "https://www.sec.gov.evil.example/files/company_tickers_exchange.json",
    "https://www.sec.gov@evil.example/files/company_tickers_exchange.json",
    "https://user@www.sec.gov/files/company_tickers_exchange.json",
    "https://www.sec.gov:443/files/company_tickers_exchange.json",
    "https://data.sec.gov:8443/submissions/CIK0000047111.json",
    "https://www.sec.gov/files/company_tickers_exchange.json?x=1",
    "https://www.sec.gov/files/company_tickers_exchange.json#frag",
    "https://data.sec.gov/submissions/%2e%2e/CIK0000047111.json",
    "https://data.sec.gov/submissions/CIK%30000047111.json",
    "https://data.sec.gov/submissions/cik0000047111.json",
    "https://data.sec.gov/submissions/CIK0000047111.json/",
    "https://www.sec.gov/files/company_tickers_exchange.json/",
    "https://data.sec.gov/submissions/CIK123.json",
    "https://data.sec.gov/submissions/CIK0000047111-submissions-1.json",
    "https://www.sec.gov/files/company_tickers.json",
    "https://WWW.SEC.GOV/files/company_tickers_exchange.json",
    "HTTPS://www.sec.gov/files/company_tickers_exchange.json",
    "https://www.sec.gov/files/COMPANY_TICKERS_EXCHANGE.json",
    "https://data.sec.gov/submissions/CIK0000047111.JSON",
    "https://www.sec.gov/submissions/CIK0000047111.json",
    "https://data.sec.gov/files/company_tickers_exchange.json",
    " https://data.sec.gov/submissions/CIK0000047111.json",
    "https://data.sec.gov/submissions/CIK0000047111.json\n",
]
before = len(seen)
for url in REFUSED:
    _, error = call(server.get_filing_document, url=url)
    check(f"T2 refused as INVALID_INPUT: {url!r}", error == "INVALID_INPUT", str(error))
check("T2 no refused address reached the transport", len(seen) == before, str(seen[before:]))

# T2: a redirect from an allowed address is an error and is not followed.
before = len(seen)
_, error = call(server.get_filing_document, url="https://data.sec.gov/submissions/CIK0000000301.json")
check("T2 a 301 from an allowed address is an error", error == "UPSTREAM_UNAVAILABLE", str(error))
check("T2 the 301 is not followed", seen[before:] == ["https://data.sec.gov/submissions/CIK0000000301.json"],
      str(seen[before:]))

print(f"problems: {len(problems)}")
sys.exit(1 if problems else 0)
