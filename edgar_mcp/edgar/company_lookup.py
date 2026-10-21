"""Company identifiers from the SEC's company_tickers.json, matched exactly.

The feed is returned as the SEC publishes it. A query matches a row only by
equality: a CIK (digits) with the row's `cik_str`, a ticker with its `ticker`
(case aside), or a title with its `title` (case aside, runs of whitespace read
as one). There is no suffix, prefix or fuzzy matching, and nothing is ranked.
"""
from __future__ import annotations

from edgar_mcp.config.constants import COMPANY_TICKERS_URL
from edgar_mcp.utils.cache import Cache
from edgar_mcp.utils.sec_client import SecError, get_json, paths_under

company_cache = Cache("company_lookup")


def format_cik(cik):
    """A CIK as the 10 digits SEC addresses use."""
    return str(cik).zfill(10)


def _feed():
    data, meta = get_json(COMPANY_TICKERS_URL, company_cache, "company_tickers_v6")
    if not isinstance(data, dict):
        raise SecError(f"{COMPANY_TICKERS_URL} is not an object of rows")
    return data, meta


def company_tickers():
    """(rows, meta): the rows of company_tickers.json in the feed's order, each
    exactly as published, and the record of where and when the feed was read
    (its `numbers_as_text` lists, by the feed's own keys, any number kept as
    text)."""
    data, meta = _feed()
    return list(data.values()), meta


def _title_key(text) -> str:
    return " ".join(str(text).split()).casefold()


def find_company(query):
    """(status, matches, meta) for an exact identifier.

    `matches` lists every row equal to the query, in the feed's order, each as
    {"cik", "matched_on", "record", "numbers_as_text"} (the paths from the
    record of its numbers kept as text); a row equal on its ticker is
    "ticker", else "title". `status` is "resolved" when the matches share one CIK,
    "ambiguous" when they name several, "not_found" when there are none.
    """
    q = str(query).strip()
    if not q:
        raise ValueError("query is empty")
    digits = q.isascii() and q.isdigit()
    if digits and len(q) > 10:
        raise ValueError(f"CIK must be up to 10 digits, got {q!r}")
    data, meta = _feed()
    paths = meta.get("numbers_as_text") or []
    matches = []
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        cik = format_cik(row.get("cik_str", ""))
        ticker, title = row.get("ticker"), row.get("title")
        if digits:
            on = "cik" if cik == format_cik(q) else None
        elif isinstance(ticker, str) and ticker.upper() == q.upper():
            on = "ticker"
        elif isinstance(title, str) and _title_key(title) == _title_key(q):
            on = "title"
        else:
            on = None
        if on:
            matches.append({"cik": cik, "matched_on": on, "record": row,
                            "numbers_as_text": paths_under(paths, [key]) if paths else []})
    ciks = {m["cik"] for m in matches}
    status = "not_found" if not ciks else "resolved" if len(ciks) == 1 else "ambiguous"
    return status, matches, meta
