"""
Company classification by industry (SIC), country of incorporation,
and dominant revenue country (50 % rule) using SEC Financial Statement Data Sets.

Usage:
    python -m edgar_mcp.edgar.company_classifier --build [--datasets-dir DIR] [--output FILE]
    python -m edgar_mcp.edgar.company_classifier --query US  # list US-revenue-dominant companies

The index is read from EDGAR_COMPANY_INDEX if set, else from the one built
into the user's data folder, else from the snapshot shipped in the package.
"""

import csv
import json
import logging
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from importlib import resources

from edgar_mcp.config.settings import COMPANY_INDEX, USER_DATA_DIR
from edgar_mcp.config.sic_codes import sic_to_subindustry

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
# The data sets the builder reads, unless --datasets-dir names another folder.
DATA_DIR = os.path.join(USER_DATA_DIR, "sec_datasets")
# Where a built index goes, unless --output names another file.
USER_INDEX_FILE = os.path.join(USER_DATA_DIR, "company_index.json")
# The file holds {"schema_version", "metadata", "companies"}. The metadata
# says when and from which quarters it was built: a file's modification
# time is no guide, since a checkout or an install sets it.
INDEX_SCHEMA_VERSION = 1


class IndexUnavailable(Exception):
    """A company index that was named, or found, cannot be read."""


# Revenue concept names we look for (us-gaap, no namespace prefix in num.txt),
# in the order tried when a filing reports more than one.
REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenueFromRelatedParties",
    "InterestAndDividendIncomeOperating",
)
_REVENUE_TAG_SET = frozenset(REVENUE_TAGS)

# Annual report form types
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}

# ISO 3166-1 alpha-2 country codes (subset that appears in SEC data)
_ISO2 = {
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT",
    "AU", "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI",
    "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY",
    "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN",
    "CO", "CR", "CU", "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM",
    "DO", "DZ", "EC", "EE", "EG", "EH", "ER", "ES", "ET", "FI", "FJ", "FK",
    "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL",
    "GM", "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM",
    "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR",
    "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN",
    "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS",
    "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK",
    "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW",
    "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP",
    "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM",
    "PN", "PR", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW",
    "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
    "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF",
    "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW",
    "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE", "VG", "VI",
    "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW",
}

# SIC code → broad industry name
SIC_DIVISIONS = [
    (100, 999, "Agriculture, Forestry & Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation & Public Utilities"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9729, "Public Administration"),
]


def sic_to_industry(sic_code):
    """Map a 4-digit SIC code to a broad industry name."""
    try:
        sic = int(sic_code)
    except (ValueError, TypeError):
        return "Unknown"
    for lo, hi, name in SIC_DIVISIONS:
        if lo <= sic <= hi:
            return name
    return "Unknown"


def _is_iso_country(code):
    """Return True if *code* looks like an ISO 3166-1 alpha-2 country code."""
    return code in _ISO2


def _extract_geo_value(segments_field):
    """Parse the Geographical=XX value out of the segments column.

    Only returns a value when Geographical is the *sole* dimension —
    compound segments like ``ConsolidationItems=…;Geographical=US``
    mix geography with other axes and produce overlapping/duplicate values.

    Returns the geographic label (e.g. 'US', 'CN', 'Europe') or None.
    """
    if not segments_field:
        return None
    parts = [p.strip() for p in segments_field.split(";") if p.strip()]
    # Only accept rows where the only dimension is Geographical
    if len(parts) != 1:
        return None
    if parts[0].startswith("Geographical="):
        return parts[0].split("=", 1)[1]
    return None


# ── Core builder ─────────────────────────────────────────────────────────────

def _available_quarters(data_dir=DATA_DIR):
    """Return quarter folder names in newest-first order."""
    quarters = []
    if not os.path.isdir(data_dir):
        return quarters
    for name in os.listdir(data_dir):
        dpath = os.path.join(data_dir, name)
        if os.path.isdir(dpath) and os.path.isfile(os.path.join(dpath, "num.txt")):
            quarters.append(name)
    return sorted(quarters, reverse=True)


def build_index(max_quarters=5, data_dir=DATA_DIR):
    """Build the company classification index from SEC data sets.

    Scans num.txt + sub.txt from the most recent *max_quarters* quarters in
    *data_dir*. Name, SIC and country come from each CIK's latest annual
    filing; revenue from the latest one with revenue data.

    Returns:
        (dict, list): {cik_str: {name, sic, industry, country_inc, state_inc,
        period, revenue_country, revenue_pct, revenue_period, geo_breakdown}}
        and the quarters read.
    """
    quarters = _available_quarters(data_dir)[:max_quarters]
    if not quarters:
        logger.error("No SEC dataset quarters found in %s", data_dir)
        return {}, []

    logger.info("Building company index from quarters: %s", quarters)

    # Step 1: Load sub.txt to get filing metadata (adsh → company info)
    # We only care about annual filings (10-K, 20-F, 40-F)
    filings = {}  # adsh → {cik, name, sic, countryinc, form, period}
    for qtr in quarters:
        sub_path = os.path.join(data_dir, qtr, "sub.txt")
        with open(sub_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                form = (row.get("form") or "").strip()
                if form not in ANNUAL_FORMS:
                    continue
                adsh = row["adsh"]
                filings[adsh] = {
                    "cik": str(int(row["cik"])),  # strip leading zeros
                    "name": row.get("name", ""),
                    "sic": row.get("sic", ""),
                    "countryinc": row.get("countryinc", "") or row.get("countryba", ""),
                    "stprinc": row.get("stprinc", "") or row.get("stprba", ""),
                    "form": form,
                    "period": row.get("period", ""),
                }

    logger.info("Loaded %d annual filings from sub.txt", len(filings))

    # Step 2: Scan num.txt for revenue rows (qtrs=4) with and without geo segments
    # revenue_total[adsh] = {tag: value}  (no segment)
    # revenue_geo[adsh] = {tag: {geo_label: value}}
    revenue_total = defaultdict(dict)  # adsh → tag → value
    revenue_geo = defaultdict(lambda: defaultdict(dict))  # adsh → tag → geo → value

    for qtr in quarters:
        num_path = os.path.join(data_dir, qtr, "num.txt")
        logger.info("Scanning %s ...", num_path)
        with open(num_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                tag = row.get("tag", "")
                if tag not in _REVENUE_TAG_SET:
                    continue
                if row.get("qtrs") != "4":
                    continue
                adsh = row["adsh"]
                if adsh not in filings:
                    continue
                uom = row.get("uom", "")
                if uom != "USD":
                    continue

                val_str = row.get("value", "")
                try:
                    val = float(val_str)
                except (ValueError, TypeError):
                    continue

                segments = row.get("segments", "") or ""
                geo = _extract_geo_value(segments)

                if geo is None and not segments.strip():
                    # Total (unsegmented) revenue
                    revenue_total[adsh][tag] = val
                elif geo is not None:
                    revenue_geo[adsh][tag][geo] = val

    logger.info("Found revenue totals for %d filings, geo segments for %d filings",
                len(revenue_total), len(revenue_geo))

    # Step 3: For each CIK, pick the latest annual filing that has data
    # Group filings by CIK
    cik_filings = defaultdict(list)
    for adsh, info in filings.items():
        cik_filings[info["cik"]].append((info["period"], adsh, info))

    index = {}
    for cik, filing_list in cik_filings.items():
        # Sort by period descending: the latest says who the company is now.
        filing_list.sort(key=lambda x: (x[0], x[1]), reverse=True)
        latest = filing_list[0][2]
        best_adsh = None
        best_info = None
        for period, adsh, info in filing_list:
            # Prefer filings that have both total and geo revenue
            if adsh in revenue_geo and adsh in revenue_total:
                best_adsh = adsh
                best_info = info
                break
            if adsh in revenue_total and best_adsh is None:
                best_adsh = adsh
                best_info = info
        if best_info is None:
            # No revenue data, still record basic info from latest filing
            _, adsh, info = filing_list[0]
            best_adsh = adsh
            best_info = info

        # Get total revenue (pick the first available revenue tag)
        total_rev = None
        chosen_tag = None
        if best_adsh in revenue_total:
            for t in REVENUE_TAGS:
                if t in revenue_total[best_adsh]:
                    total_rev = revenue_total[best_adsh][t]
                    chosen_tag = t
                    break

        # Get geographic breakdown using the same tag if possible
        geo_breakdown = {}
        if best_adsh in revenue_geo:
            geo_data = revenue_geo[best_adsh]
            if chosen_tag and chosen_tag in geo_data:
                geo_breakdown = geo_data[chosen_tag]
            else:
                # Use whichever tag has the most geo entries
                best_tag = max(geo_data.keys(), key=lambda t: len(geo_data[t]))
                geo_breakdown = geo_data[best_tag]

        # Compute percentages and apply 50% rule
        revenue_country = None
        revenue_pct = None
        pct_breakdown = {}
        if geo_breakdown:
            # Prefer total revenue as denominator; fall back to sum of geo segments
            denominator = total_rev if (total_rev and total_rev > 0) else None
            if denominator is None or denominator <= 0:
                # Only sum ISO country-level values to avoid double-counting aggregates
                iso_sum = sum(v for g, v in geo_breakdown.items() if _is_iso_country(g) and v > 0)
                denominator = iso_sum if iso_sum > 0 else sum(v for v in geo_breakdown.values() if v > 0)
            if denominator and denominator > 0:
                for geo_label, val in geo_breakdown.items():
                    pct = min(val / denominator * 100, 100.0)
                    pct_breakdown[geo_label] = round(pct, 1)
                    # Only consider ISO country codes for the 50% rule
                    if _is_iso_country(geo_label) and pct >= 50.0:
                        if revenue_country is None or pct > (revenue_pct or 0):
                            revenue_country = geo_label
                            revenue_pct = round(pct, 1)

        # Classification from the latest filing, even when an older one has the revenue split.
        sic = latest.get("sic", "")
        countryinc = latest.get("countryinc", "")
        stprinc = latest.get("stprinc", "")

        # Normalize country of incorporation
        # SEC uses US state codes (e.g., DE, CA) when countryinc=US
        inc_country = countryinc if countryinc != "US" else "US"

        index[cik] = {
            "name": latest["name"],
            "sic": sic,
            "industry": sic_to_industry(sic),
            "subindustry": sic_to_subindustry(sic),
            "country_inc": inc_country,
            "state_inc": stprinc if countryinc == "US" else "",
            "revenue_country": revenue_country,
            "revenue_pct": revenue_pct,
            "period": latest["period"],
            "revenue_period": best_info["period"] if best_adsh in revenue_total or best_adsh in revenue_geo else None,
            "geo_breakdown": pct_breakdown,
        }

    logger.info("Built index with %d companies (%d with revenue country)",
                len(index), sum(1 for v in index.values() if v["revenue_country"]))
    return index, quarters


def save_index(index, quarters, output=USER_INDEX_FILE):
    """Write the index and its build record to `output`, whole or not at all."""
    periods = [v.get("period", "") for v in index.values() if v.get("period")]
    latest = max(periods) if periods else None
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "metadata": {
            "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "SEC Financial Statement Data Sets",
            "quarters": list(quarters),
            "latest_period": f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}" if latest else None,
            "companies": len(index),
        },
        "companies": index,
    }
    folder = os.path.dirname(os.path.abspath(output))
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)
    os.replace(temp, output)
    logger.info("Saved company index to %s (%d companies)", output, len(index))
    return document


def _read_index(text, origin):
    """Companies and metadata of one index file. A file without the envelope
    is an older index: its companies are read, its provenance is unknown."""
    try:
        document = json.loads(text)
    except ValueError as e:
        raise IndexUnavailable(f"the company index at {origin} is not JSON ({e})") from None
    if isinstance(document, dict) and "schema_version" in document:
        companies, metadata = document.get("companies"), document.get("metadata")
        if document["schema_version"] != INDEX_SCHEMA_VERSION or not isinstance(companies, dict) \
                or not isinstance(metadata, dict):
            raise IndexUnavailable(f"the company index at {origin} is not in a layout this version reads")
    elif isinstance(document, dict):
        companies, metadata = document, {"built": None, "note": "an older index with no build record"}
    else:
        raise IndexUnavailable(f"the company index at {origin} is not in a layout this version reads")
    if not all(isinstance(v, dict) for v in companies.values()):
        raise IndexUnavailable(f"the company index at {origin} has entries that are not companies")
    return companies, metadata


def load_index_with_meta():
    """The company index and its build record, read together from one file.

    Taken from EDGAR_COMPANY_INDEX if set (an error if it cannot be read),
    else from the index built into the user's data folder, else from the
    snapshot in the package. The metadata names which (`origin`, `path`).
    """
    if COMPANY_INDEX:
        origin, path = "EDGAR_COMPANY_INDEX", COMPANY_INDEX
    elif os.path.isfile(USER_INDEX_FILE):
        origin, path = "built by the user", USER_INDEX_FILE
    else:
        origin, path = "snapshot in the package", None
    try:
        if path:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        else:
            text = resources.files("edgar_mcp").joinpath("data", "company_index.json").read_text(encoding="utf-8")
    except OSError as e:
        raise IndexUnavailable(f"the company index ({origin}) cannot be read ({e})") from None
    companies, metadata = _read_index(text, path or origin)
    return companies, {**metadata, "origin": origin, "path": path}


def load_index():
    """The companies of the company index; see load_index_with_meta."""
    return load_index_with_meta()[0]


def load_index_meta():
    """The build record of the company index; see load_index_with_meta."""
    return load_index_with_meta()[1]


# ── Query helpers ────────────────────────────────────────────────────────────

def query_by_revenue_country(country_code, index=None):
    """Return companies where dominant revenue country matches *country_code*."""
    if index is None:
        index = load_index()
    return {cik: info for cik, info in index.items()
            if info.get("revenue_country") == country_code.upper()}


def query_by_industry(sic_or_name, index=None):
    """Return companies matching a SIC code or industry name substring."""
    if index is None:
        index = load_index()
    try:
        sic_int = int(sic_or_name)
        return {cik: info for cik, info in index.items()
                if info.get("sic") == str(sic_int)}
    except (ValueError, TypeError):
        needle = sic_or_name.lower()
        return {cik: info for cik, info in index.items()
                if needle in info.get("industry", "").lower()}


def query_by_country_inc(country_code, index=None):
    """Return companies incorporated in *country_code*."""
    if index is None:
        index = load_index()
    return {cik: info for cik, info in index.items()
            if info.get("country_inc") == country_code.upper()}


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Build / query company classification index")
    parser.add_argument("--build", action="store_true", help="Rebuild the index from SEC datasets")
    parser.add_argument("--datasets-dir", default=DATA_DIR,
                        help=f"Folder of quarter folders holding sub.txt and num.txt (default: {DATA_DIR})")
    parser.add_argument("--output", default=USER_INDEX_FILE,
                        help=f"Where to write the built index (default: {USER_INDEX_FILE})")
    parser.add_argument("--quarters", type=int, default=5, help="How many recent quarters to read (default: 5)")
    parser.add_argument("--query", metavar="COUNTRY", help="Query companies by dominant revenue country (ISO 2-letter code)")
    parser.add_argument("--industry", metavar="SIC_OR_NAME", help="Query companies by SIC code or industry name")
    parser.add_argument("--inc", metavar="COUNTRY", help="Query companies by country of incorporation")
    parser.add_argument("--limit", type=int, default=20, help="Max results to display (default: 20)")
    args = parser.parse_args()

    if args.build:
        print("Building company classification index...")
        idx, used = build_index(args.quarters, args.datasets_dir)
        if not idx:
            raise SystemExit(f"no SEC data set quarters found in {args.datasets_dir}")
        save_index(idx, used, args.output)
        print(f"Done. {len(idx)} companies indexed from {', '.join(used)}; written to {args.output}.")

        # Summary stats
        with_rev = sum(1 for v in idx.values() if v["revenue_country"])
        top_countries = defaultdict(int)
        for v in idx.values():
            if v["revenue_country"]:
                top_countries[v["revenue_country"]] += 1
        print(f"\nCompanies with revenue country classification: {with_rev}")
        print("Top revenue-dominant countries:")
        for country, count in sorted(top_countries.items(), key=lambda x: -x[1])[:10]:
            print(f"  {country}: {count}")
    else:
        idx = load_index()

    if args.query:
        results = query_by_revenue_country(args.query, idx)
        print(f"\nCompanies with >=50% revenue from {args.query.upper()}: {len(results)}")
        for cik, info in sorted(results.items(), key=lambda x: x[1]["name"])[:args.limit]:
            pct = info["revenue_pct"]
            print(f"  CIK {cik:>10s}  {info['name'][:45]:<45s}  {pct:5.1f}%  SIC:{info['sic']}")

    if args.industry:
        results = query_by_industry(args.industry, idx)
        print(f"\nCompanies matching industry '{args.industry}': {len(results)}")
        for cik, info in sorted(results.items(), key=lambda x: x[1]["name"])[:args.limit]:
            print(f"  CIK {cik:>10s}  {info['name'][:45]:<45s}  SIC:{info['sic']}  {info['industry']}")

    if args.inc:
        results = query_by_country_inc(args.inc, idx)
        print(f"\nCompanies incorporated in {args.inc.upper()}: {len(results)}")
        for cik, info in sorted(results.items(), key=lambda x: x[1]["name"])[:args.limit]:
            print(f"  CIK {cik:>10s}  {info['name'][:45]:<45s}  SIC:{info['sic']}  {info['industry']}")
