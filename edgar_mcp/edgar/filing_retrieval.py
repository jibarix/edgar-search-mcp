"""Module for retrieving SEC EDGAR filings and data through SEC APIs.

Every method returns (data, meta), where meta names the SEC address the data
came from and when it was retrieved. A failed request raises
utils.sec_client.SecError; an empty result always means the SEC had nothing.
"""
from __future__ import annotations

import logging
import re

from jsonschema import validate, ValidationError

from edgar_mcp.config.constants import SUBMISSIONS_URL, XBRL_INSTANCE_URL
from edgar_mcp.utils.cache import Cache
from edgar_mcp.utils.helpers import iso_date
from edgar_mcp.utils.sec_client import SecError, get_json

# Initialize logger
logger = logging.getLogger(__name__)

# Initialize cache for filing data
filing_cache = Cache("filing_data", expiry=3600)

# Define a basic JSON schema for validating EDGAR submissions.
EDGAR_SUBMISSIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "filings": {
            "type": "object",
            "properties": {
                "recent": {"type": "object"},
                "files": {"type": "array"}
            },
            "required": ["recent", "files"]
        }
    },
    "required": ["filings"]
}

_HISTORY_FILE = re.compile(r"CIK\d{10}-submissions-\d+\.json")


def _format_cik(cik):
    """A CIK as the 10 digits the SEC addresses use; anything else is refused."""
    digits = str(cik).strip()
    if not (digits.isascii() and digits.isdigit() and len(digits) <= 10):
        raise ValueError(f"CIK must be up to 10 digits, got {cik!r}")
    return digits.zfill(10)


class FilingRetrieval:
    """
    Class for retrieving SEC EDGAR filings and data.
    """

    def get_company_submissions(self, cik):
        """
        Get company submissions data from SEC.

        Args:
            cik (str): The company CIK

        Returns:
            (dict, dict): Company submissions data and its retrieval record
        """
        formatted_cik = _format_cik(cik)
        url = SUBMISSIONS_URL.format(cik=formatted_cik)
        data, meta = get_json(url, filing_cache, f"submissions_{formatted_cik}")
        try:
            validate(instance=data, schema=EDGAR_SUBMISSIONS_SCHEMA)
        except ValidationError as e:
            raise SecError(f"{url} is not in the expected submissions layout: {e.message}") from e
        return data, meta

    def get_filing_metadata(self, cik, filing_type="10-K", end_date=None, limit=10,
                            include_amendments=False, strict=False):
        """
        Get metadata for company filings, newest first.

        Args:
            cik (str): The company CIK
            filing_type (str): Form type to retrieve, or "ALL"
            end_date (str): YYYY-MM-DD; filings made after it are left out
            limit (int): Maximum number of filings to return
            include_amendments (bool): also return amendments ("10-K/A" with "10-K")
            strict (bool): raise the SecError of an older filings file that
                cannot be read, instead of warning and going on without it

        Returns:
            (list, list, dict): filings, warnings and the retrieval record. A
            warning means the list may be incomplete and says why. The
            record's "sources" lists every SEC file read, the older filings
            files included, each with its address and retrieval time.
        """
        formatted_cik = _format_cik(cik)
        if end_date:
            iso_date(end_date)
        submissions, meta = self.get_company_submissions(formatted_cik)

        warnings = []
        sources = [meta]
        filings = self._process_filings_data(
            submissions["filings"]["recent"], filing_type, end_date, formatted_cik, include_amendments, warnings)
        # Older filings sit in separate files, read only if the recent ones run
        # out, newest first so the first filings found are the latest ones.
        files = [f if isinstance(f, dict) else {"name": repr(f)} for f in submissions["filings"]["files"]]
        files.sort(key=lambda f: str(f.get("filingTo") or ""), reverse=True)
        for name in (str(f.get("name", "")) for f in files):
            if len(filings) >= limit:
                break
            if not _HISTORY_FILE.fullmatch(name):
                if strict:
                    raise SecError(f"older filings file {name!r} has an unexpected name; "
                                   f"the filings of CIK {formatted_cik} cannot all be read")
                warnings.append(f"older filings file {name!r} has an unexpected name and was not read")
                continue
            try:
                data, older = get_json(f"https://data.sec.gov/submissions/{name}", filing_cache, f"historical_{name}")
                sources.append(older)
            except SecError as e:
                if strict:
                    raise
                warnings.append(f"older filings file {name} could not be read ({e.code}); "
                                "filings from that period are missing")
                continue
            filings.extend(self._process_filings_data(
                data, filing_type, end_date, formatted_cik, include_amendments, warnings))

        unique = {f["accession_number"]: f for f in filings}
        ordered = sorted(unique.values(), key=lambda f: (f["filing_date"], f["accession_number"]), reverse=True)
        # Every file read is named, each with its own retrieval time.
        return ordered[:limit], warnings, {**meta, "sources": sources}

    def _process_filings_data(self, filings_data, filing_type, end_date, cik, include_amendments, warnings):
        """
        Pick the matching filings out of one block of the submissions feed.

        Args:
            filings_data (dict): Filings data from SEC API (parallel lists)
            filing_type (str): Form type to keep, or "ALL"
            end_date (str): YYYY-MM-DD cutoff, or None
            cik (str): The company CIK, used to build each document URL
            include_amendments (bool): keep "<form>/A" as well
            warnings (list): receives a note for every filing left out for want of a date

        Returns:
            list: List of filing metadata
        """
        processed_filings = []

        accession_numbers = filings_data.get("accessionNumber", [])
        form_types = filings_data.get("form", [])
        filing_dates = filings_data.get("filingDate", [])
        documents = filings_data.get("primaryDocument", [])
        reporting_dates = filings_data.get("reportDate", [])
        items = filings_data.get("items", [])
        accepted = filings_data.get("acceptanceDateTime", [])

        wanted = filing_type.upper()
        for i in range(min(len(accession_numbers), len(form_types))):
            form = form_types[i] or ""
            if wanted != "ALL" and form.upper() != wanted and not (
                    include_amendments and form.upper() == f"{wanted}/A"):
                continue

            # A filing with no readable date cannot be placed before or after
            # the cutoff, so it is left out and reported.
            filing_date = filing_dates[i] if i < len(filing_dates) else ""
            try:
                iso_date(filing_date)
            except ValueError:
                warnings.append(f"{accession_numbers[i]} ({form}) has no usable filing date and was left out")
                continue
            if end_date and filing_date > end_date:
                continue

            # The submissions feed carries no document URL; it is built from
            # the CIK, the accession number and the primary document name.
            document = documents[i] if i < len(documents) else ""
            url = ""
            if document:
                folder = accession_numbers[i].replace("-", "")
                url = f"{XBRL_INSTANCE_URL}/{int(cik)}/{folder}/{document}"

            processed_filings.append({
                "accession_number": accession_numbers[i],
                "form": form,
                "filing_date": filing_date,
                "description": document,
                "url": url,
                "reporting_date": reporting_dates[i] if i < len(reporting_dates) else "",
                # When the SEC accepted the filing, in UTC as the feed gives it ("2026-02-20T14:46:32.000Z").
                "acceptance_datetime": accepted[i] if i < len(accepted) else "",
                # The numbered items of a current report (8-K), as the SEC lists them: ["2.01", "9.01"].
                "items": [x for x in str(items[i] if i < len(items) else "").split(",") if x],
            })

        return processed_filings

    def get_company_facts(self, cik):
        """
        Get all XBRL facts for a company using the SEC's Company Facts API.

        Args:
            cik (str): The company CIK

        Returns:
            (dict, dict): Company facts data and its retrieval record
        """
        formatted_cik = _format_cik(cik)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{formatted_cik}.json"
        return get_json(url, filing_cache, f"company_facts_{formatted_cik}")

    def get_company_concept(self, cik, taxonomy, concept):
        """
        Get all values for a specific concept from a company using the SEC's Company Concept API.

        Args:
            cik (str): The company CIK
            taxonomy (str): The taxonomy (e.g., 'us-gaap', 'ifrs-full')
            concept (str): The concept name (e.g., 'Assets', 'Liabilities')

        Returns:
            (dict, dict): Company concept data and its retrieval record
        """
        formatted_cik = _format_cik(cik)
        if not re.fullmatch(r"[a-z][a-z0-9-]*", taxonomy) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", concept):
            raise ValueError(f"not a taxonomy and concept name: {taxonomy!r}, {concept!r}")
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{formatted_cik}/{taxonomy}/{concept}.json"
        return get_json(url, filing_cache, f"company_concept_{formatted_cik}_{taxonomy}_{concept}")
