# edgar-search-mcp

An [MCP](https://modelcontextprotocol.io) server that gives an AI agent read-only
access to SEC EDGAR: company lookup, reported financial statements, single XBRL
concepts, filing lists, the documents and exhibits of a filing, filing text,
revenue by segment, and the SEC's full-text search.

It talks to the SEC's own services (`data.sec.gov`, `www.sec.gov`,
`efts.sec.gov`) and nothing else. It needs no API key. Every tool is read-only.

Every answer carries its source: the accession number, form and filing date
for each figure, and a URL, SHA-256 and retrieval time for each document read.
An empty result always means the SEC had nothing. A failed request is reported
as a coded error, never as an empty answer.

## Contents

- [Tools](#tools)
- [Install](#install)
- [Configure](#configure)
- [Connect an MCP client](#connect-an-mcp-client)
- [Tool reference](#tool-reference)
- [Errors](#errors)
- [How the figures are chosen](#how-the-figures-are-chosen)
- [SEC access: rate limit, retries, cache](#sec-access-rate-limit-retries-cache)
- [Frozen copies for reproducible runs](#frozen-copies-for-reproducible-runs)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Limitations](#limitations)
- [License](#license)

## Tools

| Tool | What it returns |
|------|-----------------|
| `lookup_company` | The SEC CIK for a name or ticker, and how the match was made |
| `get_financial_statement` | Balance sheet, income, cash flow, equity or comprehensive income facts, by period |
| `get_concept` | Every filed value of one XBRL concept, with its filing |
| `list_filings` | A company's filings: accession number, dates, 8-K items, main document URL |
| `list_filing_documents` | Every document of one filing: main document, exhibits, data files |
| `get_filing_text` | Plain text of a main document or exhibit, in pages, or where a phrase occurs |
| `get_segment_revenue` | Revenue by business segment, product and geography from the latest annual report's XBRL |
| `search_filings` | Filings whose text contains given words (SEC full-text search, 2001 onward) |
| `search_companies` | Candidate companies from a local index by SIC code, industry and country |

## Install

Requires Python 3.11 or later. It was tested on CPython 3.13 on Windows.

```bash
git clone https://github.com/jibarix/edgar-search-mcp.git
cd edgar-search-mcp
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e .
```

`pyproject.toml` pins the three direct dependencies (`mcp==2.3.0`,
`httpx==0.28.1`, `jsonschema==4.26.0`). `requirements.lock` lists every
package with its version and SHA-256, exactly as tested. On Windows with
CPython 3.13 you can install from it instead:

```bash
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
```

The hashes in the lock are for Windows wheels. On other platforms, use
`pip install -e .`.

Install from the cloned folder (`-e`). The server keeps its cache and the
company index next to the code. A non-editable install would put them inside
`site-packages`.

## Configure

| Variable | Required | Meaning |
|----------|----------|---------|
| `EDGAR_USER_AGENT` | **yes** | Your name and email, e.g. `Jane Doe jane@example.com`. The SEC asks every client to identify itself this way. Without it no request is sent, and tools that need the SEC answer `NO_USER_AGENT`. |
| `EDGAR_CACHE_DIR` | no | Where responses are cached (default `cache/` in the repository). |
| `EDGAR_FROZEN_DIR` | no | Serve saved responses only; the network is never used. See [Frozen copies](#frozen-copies-for-reproducible-runs). |
| `EDGAR_RECORD_DIR` | no | Save every live response there as a frozen copy. |

## Connect an MCP client

The server speaks MCP over stdio. Start it with `python -m edgar_mcp`, or with
the `edgar-search-mcp` command the install creates.

MCP clients pass only a few environment variables to a server. Set
`EDGAR_USER_AGENT` in the client's configuration, not just in your shell.

**Claude Code**

```bash
claude mcp add edgar-search \
  -e EDGAR_USER_AGENT="Jane Doe jane@example.com" \
  -- /path/to/edgar-search-mcp/.venv/bin/python -X utf8 -m edgar_mcp
```

On Windows the interpreter is `.venv\Scripts\python.exe`. Run the command from
the repository folder, or add `-e PYTHONPATH=/path/to/edgar-search-mcp`.

**Claude Desktop and other clients** (JSON configuration):

```json
{
  "mcpServers": {
    "edgar-search": {
      "command": "/path/to/edgar-search-mcp/.venv/bin/python",
      "args": ["-X", "utf8", "-m", "edgar_mcp"],
      "env": {
        "EDGAR_USER_AGENT": "Jane Doe jane@example.com",
        "PYTHONPATH": "/path/to/edgar-search-mcp"
      }
    }
  }
}
```

`-X utf8` keeps filing text intact on Windows consoles.

## Tool reference

Dates are `YYYY-MM-DD`. `cik_or_ticker` accepts a CIK, an exact ticker or an
exact company name. Anything else is refused with the candidates, so the
caller chooses. Most results also carry the SEC address they came from, when
it was retrieved and whether it came from the cache.

### `lookup_company(query)`

Resolves a name or ticker to its 10-digit CIK. `match_type` says how the query
matched:

- `ticker`, `name` and `name_suffix` identify one company.
- `shared_name` lists every company filing under that exact name.
- `prefix` and `fuzzy` give up to five candidates.
- `none` means nothing was close.

The SEC feed has current tickers and names only. For historical work, settle
the CIK first and use it from then on.

### `get_financial_statement(cik_or_ticker, statement_type, period_type="annual", num_periods=4, as_of=None)`

- `statement_type`: `BS`, `IS`, `CF`, `EQ`, `CI` or `ALL`.
- `period_type`:
  - `annual`: fiscal years.
  - `quarterly`: single fiscal quarters. A quarter the company did not report
    on its own is derived as year-to-date minus the prior year-to-date. Its
    entry in `sources` is marked `derived` and gives both figures and their
    filings.
  - `ytd`: fiscal year to date at each quarter end. For trailing twelve
    months, take the last fiscal year, add the current `ytd` and subtract the
    `ytd` one year earlier.
- `num_periods`: 1 to 40.
- `as_of`: ignore anything filed after this date (point-in-time figures).

The figures are the whole-company XBRL facts the SEC publishes (Company Facts),
grouped by statement. They are not the full statement as filed. Facts split by
share class, segment or geography are left out, and so are company-specific
line items; read those with `get_filing_text`. Only USD, USD/share, share and
pure amounts are returned. Anything left out is named in
`metadata.skipped_units`. Every value carries the accession number, form and
filing date in `sources`. Read `metadata.warnings` before using the figures.

### `get_concept(cik_or_ticker, concept, taxonomy="us-gaap", unit=None, as_of=None, limit=100, period_end_from=None, period_end_to=None, offset=0)`

Every filed value of one concept, newest period first. Use it for anything the
statements do not group. For example, the cover-page share count is
`taxonomy="dei"`, `concept="EntityCommonStockSharesOutstanding"`.

- `taxonomy`: `us-gaap`, `ifrs-full` or `dei`.
- `unit`: needed only when the concept was reported in more than one unit.
- Every filing that reported a period contributes a row, so a period can
  appear more than once.
- `period_end_from` and `period_end_to` filter by period end (inclusive).
- Results come in pages (`limit` 1 to 500). When `next_offset` is not null,
  pass it as `offset` for the next page.

### `list_filings(cik_or_ticker, form="10-K", as_of=None, limit=10, include_amendments=False)`

A company's filings, newest first. `form` is an SEC form type (`10-K`, `10-Q`,
`8-K`, `DEF 14A`, `20-F`, ...) or `ALL`. With `include_amendments`, `10-K/A`
comes back with `10-K`. Each row has:

- the accession number;
- the filing date and period of report;
- the 8-K item numbers, for current reports;
- the URL of the main document.

The SEC keeps older filings in separate files; these are read too. If
`complete` is false the list may be missing filings, and `warnings` says which
and why. `limit` is 1 to 100.

### `list_filing_documents(url)`

`url` is any document URL of a filing, as returned by `list_filings` or
`search_filings`. The tool reads the filing's index page. It returns one row
per document with its sequence number, its type as the SEC lists it (`8-K`,
`EX-99.1`, `EX-21.1`, `GRAPHIC`, `XML`, ...), description, size and URL.
`readable` is true for a document `get_filing_text` can read. The type says
what a document is, not whether it was filed or furnished; the filing's own
text says that.

### `get_filing_text(url, find=None, start=0, max_chars=12000)`

Plain text of a main document or exhibit. HTML tags are removed, table cells
are separated by tabs and invisible characters are dropped.

- Without `find`: up to `max_chars` (at most 40,000) characters from `start`.
  Pass `next_start` to read on.
- With `find` (a case-insensitive phrase of up to 200 characters): up to 40
  matches at or after `start`, each with its offset and an excerpt. Then read
  the passage itself with `start`.

Offsets count characters of the extracted text, not of the original file.
`sha256` identifies the file as retrieved, and `extraction` names the
text-extraction rules (now `tags-stripped-v2`). Quote both with any passage
you rely on. Only `https://www.sec.gov/Archives/edgar/data/...` documents ending
in `.htm`, `.html`, `.txt` or `.xml` are accepted. Other hosts, paths that
climb out of the archive, query strings and binary files are refused.

### `get_segment_revenue(cik_or_ticker, as_of=None)`

Revenue by business segment, by product or service and by geography. It is read
from the XBRL data of the latest 10-K (else 20-F), because the Company Facts
feed leaves these splits out. For each axis it lists members with the fiscal
year's revenue and their share of the members listed. It also returns:

- `total_revenue`: the company figure with no split;
- `sum_of_members`, per axis;
- the filing's accession number, URL and SHA-256.

Members of one axis can overlap, and segment figures can include sales between
segments, so they need not add up to the total. The tool warns about this; it
does not remove overlaps.

### `search_filings(query, forms="10-K", filed_from=None, filed_to=None, offset=0, limit=25)`

The SEC's full-text search, which covers filings from 2001.

- `query` uses the SEC syntax: all words must be present, phrases go in double
  quotes, and `OR` joins alternatives.
- `forms` is one form type or several separated by commas. Amendments come
  with their form.
- `filed_from` and `filed_to` are given together or not at all.
- `limit` is 1 to 100. Pass `next_offset` as `offset` for the next page. The
  SEC serves at most 10,000 hits.

A hit says the words are somewhere in the document. It does not say they are
near each other or what they mean. Read the passage with `get_filing_text`
before relying on it.

### `search_companies(sic=None, industry=None, country_inc=None, name_substring=None, limit=25)`

Filters a local index (`data/company_index.json`) of SEC filers by 4-digit SIC
code, industry name, country of incorporation (ISO 3166-1 alpha-2) and name.
All filters are combined with AND. The index is a snapshot (`index_built`) for
finding candidates only. Confirm each company's SIC code and filings with the
other tools.

To rebuild the index, download quarterly
[Financial Statement Data Sets](https://www.sec.gov/dera/data/financial-statement-data-sets)
and unzip each into `data/sec_datasets/<quarter>/`. Then run
`python -m edgar.company_classifier --build`.

## Errors

A tool that cannot do its work returns an MCP error whose message starts with a
code:

| Code | Meaning |
|------|---------|
| `INVALID_INPUT` | An argument is malformed or out of range; the message says which |
| `AMBIGUOUS_COMPANY` | The name fits more than one company; the candidates are listed |
| `NOT_FOUND` | The SEC has nothing at that address, or nothing that fits (with the reason) |
| `RATE_LIMITED` | The SEC refused (HTTP 403/429) or asked for a pause still in force |
| `UPSTREAM_UNAVAILABLE` | The SEC did not answer usably after retries |
| `UNSUPPORTED_DOCUMENT` | The document is too large or not a text type this tool reads |
| `NO_USER_AGENT` | `EDGAR_USER_AGENT` is not set |
| `NO_FROZEN_COPY` | Frozen copies only, and this address has none |

When `get_financial_statement` finds no figures, the message gives the reason:

- `NO_FACTS`: the SEC has no XBRL facts for the company.
- `NO_SUPPORTED_TAXONOMY`: the company reports in neither `us-gaap` nor
  `ifrs-full`.
- `NO_FACTS_FOR_STATEMENT`: none of the statement's concepts were reported.
- `UNSUPPORTED_UNITS`: the figures are in another currency.
- `NOTHING_ON_FILE_AT_CUTOFF`: nothing was filed by `as_of`.
- `INSUFFICIENT_PERIOD_EVIDENCE`: facts exist, but none fit a fiscal period of
  the requested type.

## How the figures are chosen

- **Periods.** The tool infers the fiscal calendar from the dates the company
  reports. That covers 52/53-week years and fiscal years that do not end in
  December. If too few concepts mark a quarter end, it says so in
  `INSUFFICIENT_PERIOD_EVIDENCE`.
- **Vintage.** When a period was reported more than once, the latest periodic
  report (10-K, 10-Q, 20-F, 40-F, 6-K) wins, so restatements replace the
  figures first reported. With `as_of`, "latest" means latest on or before that
  date.
- **Classification.** XBRL tags are mapped to statements and sections by fixed
  rules plus the SEC's tag mapping (`edgar/data/sec_tag_mapping.json`). No
  model is involved, so the same input always gives the same output.

## SEC access: rate limit, retries, cache

- Every request goes through `utils/sec_client.py`, which allows at most 8
  requests a second per process. The SEC's limit is 10 a second per user,
  across all your processes and machines.
- Timeouts, HTTP 429 and HTTP 5xx are retried with exponential back-off, and a
  `Retry-After` from the SEC is honoured. A pause longer than 60 seconds is
  reported as `RATE_LIMITED` instead of holding the call open. HTTP 403 is
  treated as a rate-limit block and not retried.
- Redirects are not followed, and response sizes are capped.
- Filing lists and XBRL facts are cached on disk for one hour, and the SEC's
  ticker list for 24 hours (`EDGAR_CACHE_DIR`). The text of the last eight
  documents read is kept in memory. A cache entry that cannot be read, or that
  was stored under another key, is ignored.

## Frozen copies for reproducible runs

To make a run repeatable, record what the SEC returned and replay it later:

```bash
EDGAR_RECORD_DIR=./frozen python -m edgar_mcp   # live; every response saved, 404s included
EDGAR_FROZEN_DIR=./frozen python -m edgar_mcp   # replay; the network is never used
```

Each response is saved as a `.body` file plus a `.json` description (URL,
status, content type, SHA-256, retrieval time). During replay:

- an address with no copy is refused with `NO_FROZEN_COPY`;
- a copy whose body no longer matches its SHA-256 is refused.

The cache is off while recording or replaying.

## Tests

Four plain scripts, run from the repository root. Each prints one line per
check and ends with `problems: N`. The exit code is non-zero if anything fails.

```bash
export EDGAR_USER_AGENT="Jane Doe jane@example.com"   # PowerShell: $env:EDGAR_USER_AGENT = "..."
python -X utf8 tests/test_cases.py      # no network: parser, lookup, filing list, URL checks, cache
python -X utf8 tests/test_frozen.py     # record two live answers, then replay them offline
python -X utf8 tests/test_mcp_stdio.py  # starts the server over stdio and calls every tool against live EDGAR
python -X utf8 tests/test_periods.py    # fiscal-period checks for 22 companies against their annual reports
```

The live tests depend on current SEC data. A few checks pin a specific filing
(for example, Apple's fiscal 2023 10-K).

## Project layout

```
edgar-search-mcp/
├── edgar_mcp/
│   ├── __main__.py          # python -m edgar_mcp: starts the stdio server
│   └── server.py            # the nine tools
├── edgar/
│   ├── company_lookup.py    # name / ticker / CIK resolution
│   ├── filing_retrieval.py  # submissions, Company Facts, Company Concept
│   ├── xbrl_parser.py       # periods, vintage, statement grouping
│   ├── tag_classifier.py    # XBRL tag -> statement and section
│   ├── company_classifier.py# builds data/company_index.json
│   └── data/sec_tag_mapping.json
├── config/
│   ├── constants.py         # SEC endpoints, User-Agent
│   ├── settings.py          # timeouts, rate limit, cache, frozen copies
│   └── sic_codes.py         # SIC -> industry
├── utils/
│   ├── sec_client.py        # the only code that talks to the SEC
│   ├── cache.py             # disk cache
│   └── helpers.py
├── data/company_index.json  # snapshot used by search_companies
├── tests/
├── pyproject.toml
└── requirements.lock
```

## Limitations

- Only what companies file with the SEC in XBRL. Figures before XBRL
  (roughly 2009–2011, depending on filer size) are not available as facts, but
  the documents can still be read as text.
- Statement figures are whole-company facts in standard taxonomies, not the
  statement as printed. Company-specific line items need `get_filing_text`.
- Non-USD amounts are left out of statements (named in `skipped_units`). Use
  `get_concept` with `unit` to read them.
- Segment members can overlap, and the tool does not reconcile them.
- `search_companies` uses a dated local snapshot.
- PDFs and images inside filings are listed but not read.

## License

MIT. See [LICENSE](LICENSE). Data comes from the U.S. Securities and Exchange
Commission's EDGAR system.
