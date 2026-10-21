"""Module for retrieving SEC EDGAR filings and data through SEC APIs.

Every method returns (data, meta), where meta names the SEC address the data
came from and when it was retrieved. A failed request raises
utils.sec_client.SecError; an empty result always means the SEC had nothing.
"""
from __future__ import annotations

import logging
import re

from jsonschema import validate, ValidationError

from edgar_mcp.config.constants import SUBMISSIONS_URL
from edgar_mcp.utils.cache import Cache
from edgar_mcp.utils.sec_client import SecError, get_json, paths_under

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


def _rows(table, file_name, warnings, as_text=()):
    """The rows of one column table ({column: [values]}), each value under its
    column's key. `as_text` lists the numbers of the table kept as text
    (sec_client.decode_json), as paths from the table ([column, index, ...]);
    each row's `numbers_as_text` lists those of its record, as paths from the
    record ([column, ...])."""
    if not isinstance(table, dict):
        raise SecError(f"{file_name} is not a table of filings")
    columns = {k: v for k, v in table.items() if isinstance(v, list)}
    lengths = {len(v) for v in columns.values()}
    if len(lengths) > 1:
        warnings.append(f"the columns of {file_name} have different lengths {sorted(lengths)}; "
                        "a row lacks the columns that stop short of it")
    by_row = {}
    for path in as_text:
        if len(path) >= 2 and path[0] in columns and isinstance(path[1], int):
            by_row.setdefault(path[1], []).append([path[0], *path[2:]])
    for i in range(max(lengths, default=0)):
        yield {"record": {k: v[i] for k, v in columns.items() if i < len(v)},
               "source": {"file": file_name, "index": i}, "numbers_as_text": by_row.get(i, [])}


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

    def older_file_names(self, submissions, cik):
        """The names `filings.files` lists, in its order. A name that is not
        CIK##########-submissions-NNN.json is refused when it is reached."""
        for entry in submissions["filings"]["files"]:
            name = str(entry.get("name", "")) if isinstance(entry, dict) else repr(entry)
            if not _HISTORY_FILE.fullmatch(name):
                raise SecError(f"older filings file {name!r} has an unexpected name; "
                               f"the filings of CIK {_format_cik(cik)} cannot all be read")
            yield name

    def get_older_submissions(self, name):
        """(data, meta): one older submissions file, as the SEC publishes it."""
        if not _HISTORY_FILE.fullmatch(str(name)):
            raise ValueError(f"not an older submissions file name: {name!r}")
        return get_json(f"https://data.sec.gov/submissions/{name}", filing_cache, f"historical_{name}")

    def iter_filing_records(self, cik, sources, warnings, older=True):
        """Every row of the submissions filing tables, in the SEC's order: the
        recent table, then each file `filings.files` names, in the order it
        names them. Each row is {"record", "source", "numbers_as_text"}:
        `record` maps every column, under the SEC's key, to the row's value;
        `source` is the file and the row's index in it; `numbers_as_text`
        lists the numbers of `record` kept as text, as paths from it. Older files are read only when the caller
        goes on past the recent table, and a file that cannot be read raises:
        the rows are never silently cut short. The meta of every file read is
        appended to `sources`; a column shorter than the others is reported in
        `warnings` and left out of the rows it does not reach.
        """
        formatted_cik = _format_cik(cik)
        submissions, meta = self.get_company_submissions(formatted_cik)
        sources.append(meta)
        name = f"CIK{formatted_cik}.json"
        yield from _rows(submissions["filings"]["recent"], f"{name}#filings.recent", warnings,
                         paths_under(meta.get("numbers_as_text"), ["filings", "recent"]))
        if not older:
            return
        for file_name in self.older_file_names(submissions, formatted_cik):
            data, older_meta = self.get_older_submissions(file_name)
            sources.append(older_meta)
            yield from _rows(data, file_name, warnings, older_meta.get("numbers_as_text") or [])

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

    def get_frame(self, taxonomy, concept, unit, period):
        """The SEC frames document: one fact per company for one concept, unit
        and calendar period. The arguments are checked for syntax only and
        put into the address as given.

        Returns:
            (dict, dict): the frames document and its retrieval record
        """
        if (not re.fullmatch(r"[a-z][a-z0-9-]*", taxonomy) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", concept)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", unit)
                or not re.fullmatch(r"CY\d{4}(Q[1-4]I?)?", period)):
            raise ValueError(f"not a taxonomy, concept, unit and CY period: "
                             f"{taxonomy!r}, {concept!r}, {unit!r}, {period!r}")
        url = f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
        return get_json(url, filing_cache, f"frame_{taxonomy}_{concept}_{unit}_{period}")
