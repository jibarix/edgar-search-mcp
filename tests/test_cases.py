"""Fixed cases for company lookup, the SEC client, filing list, document address check and cache.

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

from edgar_mcp.edgar import company_lookup  # noqa: E402
from edgar_mcp.edgar.filing_retrieval import FilingRetrieval  # noqa: E402
from edgar_mcp.server import _document_url, _resolve  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402
from edgar_mcp.utils.cache import Cache  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


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
        company_lookup.company_cache.delete("company_tickers_v5")


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
    check("reading an expired entry does not remove it", os.path.exists(cache._get_cache_path("old")))
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


def cache_race_cases():
    """A reader holds an expired entry while a writer replaces it: the new entry must survive."""
    import json as json_module
    import threading
    from edgar_mcp.utils import cache as cache_module

    cache = Cache("race")
    cache.set("k", "old", ttl=-1)
    loaded, written = threading.Event(), threading.Event()
    real_loads = json_module.loads
    reader_thread = []

    def paused_loads(text, *a, **k):
        value = real_loads(text, *a, **k)
        if threading.current_thread() in reader_thread:
            loaded.set()                  # the reader has the expired entry in hand
            written.wait(timeout=10)      # and finishes only after the writer is done
        return value

    results = {}

    def reader():
        results["read"] = cache.get_entry("k")

    def writer():
        loaded.wait(timeout=10)
        cache.set("k", "new", ttl=3600)
        written.set()

    cache_module.json.loads = paused_loads
    try:
        r, w = threading.Thread(target=reader), threading.Thread(target=writer)
        reader_thread.append(r)
        r.start()
        w.start()
        r.join(timeout=20)
        w.join(timeout=20)
    finally:
        cache_module.json.loads = real_loads
    check("the interleaving ran: reader read, writer wrote, reader finished",
          loaded.is_set() and written.is_set() and not r.is_alive() and not w.is_alive())
    check("the reader of the expired entry got a miss", results.get("read") is None, str(results.get("read")))
    entry = cache.get_entry("k")
    check("the writer's new entry survives the reader", entry is not None and entry["data"] == "new", str(entry))

    # An interrupted write: the old entry stays whole and no temporary file is left.
    cache = Cache("interrupted")
    cache.set("k", "whole", ttl=3600)
    real_dump = json_module.dump

    def failing_dump(value, handle, *a, **k):
        handle.write('{"key": "k", "expires_at": 9e12, "da')  # half an entry, then the disk fails
        raise OSError("disk full")

    cache_module.json.dump = failing_dump
    try:
        cache.set("k", "replacement", ttl=3600)
    finally:
        cache_module.json.dump = real_dump
    check("an interrupted write leaves the old entry whole", cache.get("k") == "whole", str(cache.get("k")))
    check("an interrupted write leaves no temporary file",
          not [n for n in os.listdir(cache.cache_dir) if n.endswith(".tmp")], str(os.listdir(cache.cache_dir)))
    # A process killed mid-write leaves a temporary file behind; it is never read as an entry.
    with open(os.path.join(cache.cache_dir, "leftover.tmp"), "w", encoding="utf-8") as handle:
        handle.write('{"key": "k", "expires_at": 9e12, "data": "partial')
    check("a temporary file left by a killed writer is never read", cache.get("k") == "whole")
    cache.set("k", "next", ttl=3600)
    check("a write after a killed writer succeeds", cache.get("k") == "next")

    # Unreadable entries are misses, not errors, and the next write replaces them.
    cache = Cache("unreadable")
    for label, content in (("truncated JSON", b'{"key": "k", "expires_at": 9e12, "data": [1, 2'),
                           ("bytes that are not UTF-8", b"\xff\xfe\x00garbage"),
                           ("JSON that is not an entry", b'[1, 2, 3]'),
                           ("an entry with no expiry", b'{"key": "k", "data": 1}'),
                           ("an empty file", b"")):
        os.makedirs(cache.cache_dir, exist_ok=True)
        with open(cache._get_cache_path("k"), "wb") as handle:
            handle.write(content)
        try:
            got = cache.get_entry("k")
            check(f"an unreadable entry ({label}) is a miss", got is None, str(got))
        except Exception as e:
            check(f"an unreadable entry ({label}) is a miss", False, repr(e))
        cache.set("k", "fresh", ttl=3600)
        check(f"a write replaces an unreadable entry ({label})", cache.get("k") == "fresh")
    os.remove(cache._get_cache_path("k"))
    os.makedirs(cache._get_cache_path("k"))  # a folder where the entry should be
    check("an entry path that cannot be opened is a miss", cache.get_entry("k") is None)


if __name__ == "__main__":
    lookup_cases()
    client_cases()
    filing_cases()
    address_cases()
    cache_cases()
    cache_race_cases()
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
