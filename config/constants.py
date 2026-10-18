"""
Constants for the EDGAR Financial Tool.
"""

import os

# SEC API endpoints
SEC_BASE_URL = "https://www.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_BASE_URL}/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
XBRL_INSTANCE_URL = f"{SEC_BASE_URL}/Archives/edgar/data"

# HTTP Request Headers. The SEC asks each client to identify its operator with
# a name and contact address, e.g. "Jane Doe jane@example.com". There is no
# default: without EDGAR_USER_AGENT no request is sent to the SEC.
USER_AGENT = (os.environ.get("EDGAR_USER_AGENT") or "").strip()
HTTP_HEADERS = {"User-Agent": USER_AGENT}
