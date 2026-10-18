"""Company lookup functionality for finding SEC CIK numbers."""
from __future__ import annotations

import re
from difflib import get_close_matches

from config.constants import COMPANY_TICKERS_URL
from utils.cache import Cache
from utils.sec_client import get_json

# Initialize cache for company lookup
company_cache = Cache("company_lookup")


def format_cik(cik):
    """
    Format CIK to 10-digit format with leading zeros.
    
    Args:
        cik (str or int): The CIK number
        
    Returns:
        str: Formatted 10-digit CIK
    """
    return str(cik).zfill(10)


def get_company_tickers():
    """Retrieve the SEC company tickers feed indexed for lookup.

    Returns a dict with two sub-indexes so callers can rank ticker and
    name hits independently:
        {
            "by_name":   normalized lowercase name -> company entry,
            "by_ticker": uppercase ticker          -> company entry,
            "shared":    normalized lowercase name -> entries, for a name
                         used by more than one CIK,
        }
    Each entry has the shape {"cik", "ticker", "name"}.
    """
    cached_data = company_cache.get("company_tickers_v4")
    if cached_data:
        return cached_data

    # A failed download raises SecError; it is never reported as "no match".
    data, _ = get_json(COMPANY_TICKERS_URL)

    by_name: dict[str, dict] = {}
    by_ticker: dict[str, dict] = {}
    by_cik: dict[str, dict[str, dict]] = {}
    for _, company_info in data.items():
        cik = company_info["cik_str"]
        ticker = company_info["ticker"]
        title = company_info["title"]
        normalized_title = re.sub(r'[^\w\s]', '', title.lower()).strip()

        entry = {
            "cik": format_cik(cik),
            "ticker": ticker,
            "name": title,
        }
        # One company listed under several tickers keeps its first entry.
        by_name.setdefault(normalized_title, entry)
        by_cik.setdefault(normalized_title, {}).setdefault(entry["cik"], entry)
        by_ticker[ticker.upper()] = entry

    # A name used by more than one CIK identifies none of them.
    shared = {name: list(entries.values()) for name, entries in by_cik.items() if len(entries) > 1}
    result = {"by_name": by_name, "by_ticker": by_ticker, "shared": shared}
    company_cache.set("company_tickers_v4", result)
    return result


# Common corporate suffixes we strip / try-appending when resolving names.
_CORP_SUFFIXES = ("inc", "corp", "corporation", "company", "co", "ltd",
                  "limited", "lp", "llc", "plc", "holdings", "group")


def find_company(query):
    """Search for a company by name or ticker. Returns (match_type, matches).

    Ranking order (first non-empty rule wins):
        "ticker"      exact ticker (case-insensitive)          -> 1 result
        "name"        exact normalized name                    -> 1 result
        "shared_name" that name belongs to more than one CIK   -> all of them
        "name_suffix" name == query + one corporate suffix     -> 1 result
                      (catches "Apple" -> "Apple Inc"); if several
                      suffixes match, all are returned as "prefix"
        "prefix"      names starting with query, shortest first
        "fuzzy"       close names only — tickers excluded so short
                      tickers like APLE don't outrank longer names
        "none"        nothing close

    Only "ticker", "name" and "name_suffix" identify one company. The feed lists current
    tickers and names, not those in use at an earlier date.
    """
    q = query.strip()
    q_norm = re.sub(r'[^\w\s]', '', q.lower()).strip()
    if not q_norm:
        return "none", []

    data = get_company_tickers()
    by_name = data["by_name"]
    by_ticker = data["by_ticker"]

    if q.upper() in by_ticker:
        return "ticker", [by_ticker[q.upper()]]

    shared = data.get("shared", {})
    if q_norm in shared:
        return "shared_name", shared[q_norm]
    if q_norm in by_name:
        return "name", [by_name[q_norm]]

    suffixed = [f"{q_norm} {suffix}" for suffix in _CORP_SUFFIXES if f"{q_norm} {suffix}" in by_name]
    if len(suffixed) == 1:
        if suffixed[0] in shared:
            return "shared_name", shared[suffixed[0]]
        return "name_suffix", [by_name[suffixed[0]]]

    prefix_token = q_norm + " "
    prefix_hits = [
        entry for norm, entry in by_name.items()
        if norm.startswith(prefix_token)
    ]
    if prefix_hits:
        prefix_hits.sort(key=lambda e: len(e["name"]))
        return "prefix", prefix_hits[:5]

    fuzzy = get_close_matches(q_norm, list(by_name.keys()), n=5, cutoff=0.6)
    return ("fuzzy" if fuzzy else "none"), [by_name[m] for m in fuzzy]
