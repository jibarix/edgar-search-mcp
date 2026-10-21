# Moving a consumer from 0.8 to 1.0

Version 1.0 returns what the SEC and the filer published and leaves every
choice of meaning to the caller. This page lists each tool, field and Python
name that 0.8 had and 1.0 removed or changed, and how to rebuild it from the
1.0 answers. Where 0.8 made a choice (a statement type, a revenue concept, a
name match), the recipe says that the choice is now the consumer's, to be made
and recorded in the consumer's own code.

The call sites named below are those of the valuation app
(`agentic_valuation`, read on 2026-10-21; paths relative to its root). They
were read, not changed: the app still expects 0.8 and needs the edits below
before it can use 1.0.

The answer shapes are defined in [RESPONSE_CONTRACT.md](RESPONSE_CONTRACT.md).

## Every answer

| 0.8 | 1.0 |
|-----|-----|
| top-level `source_url`, `retrieved_at`, `frozen`, `cache_hit` | `sources[]`, one entry per SEC response read: `url`, `retrieved_at`, `frozen`, `cache_hit`, `sha256` |
| no version field | `contract_version` ("1.0"), `representation` (`source`, `source_records`, `parsed_source`, `transformed`), `returned_at` |
| error codes | unchanged, except `INDEX_UNAVAILABLE` (removed) and `NO_FROZEN_COPY` (new) |

How results reach the caller did not change: over MCP the answer is the tool's
dict (`structured_content`, or the JSON text), and an error is an MCP error
whose text starts with the code (`valuation/tools.py:73-80`,
`valuation/statements.py:983-994`; `peer_count.py:120` still finds
`RATE_LIMITED` in it). Called in-process, each tool still has `__wrapped__`
(`functools.wraps`), takes the same first positional argument, and raises
instead of returning an error.

`statements._Session.call` logs `data.get("source_url") or data.get("url")`
(`valuation/statements.py:983-994`). In 1.0 use `data["sources"][0]["url"]`;
`url` is still top-level only in `get_filing_text`, `get_filing_document` and
on each row of `list_filings` and `search_filings`.

## Python names

### `company_lookup.get_company_tickers()` (removed)

0.8 returned `{"by_ticker", "by_name", "shared", "source"}`, each entry
`{"cik", "ticker", "name"}`, with the name key lower-cased and stripped of
punctuation. 1.0 has `company_tickers()`, which returns `(rows, meta)`: the
rows of `company_tickers.json` in the feed's order, each as published
(`cik_str`, `ticker`, `title`), and the retrieval record.

Used at `valuation/app.py:84-85`, `valuation/build.py:223-228`,
`valuation/companies.py:33,345,517`, `valuation/peer_count.py:31,90` and
`notebooks/03_peer_similarity_pilot.ipynb:139`. To rebuild in the consumer:

```python
from edgar_mcp.edgar.company_lookup import company_tickers, format_cik

rows, meta = company_tickers()
by_ticker = {}
for r in rows:                      # feed order: a company's first row comes first
    entry = {"cik": format_cik(r["cik_str"]), "ticker": r["ticker"], "name": r["title"]}
    by_ticker[r["ticker"].upper()] = entry          # 0.8: a later row with the same ticker replaced the entry
```

`by_name` used a normalisation the SEC does not publish (lower case,
punctuation removed, first row kept). If a consumer still wants it, it is the
consumer's rule and belongs in the consumer's code. `companies.py:345` relies
on dict order being the feed's order; `rows` keeps that order.

### `company_lookup.find_company(q)` (changed)

0.8 returned `(match, hits)` with `match` one of `ticker`, `name`,
`name_suffix`, `shared_name`, `prefix`, `fuzzy`, `none`, and each hit
`{"cik", "ticker", "name"}`. 1.0 returns `(status, matches, meta)`. `status` is
`resolved`, `ambiguous` or `not_found`. Each match is `{"cik", "matched_on",
"record"}`, where `matched_on` is `cik`, `ticker` or `title` and `record` is the
feed row as published.

| 0.8 `match` | 1.0 |
|-------------|-----|
| `ticker` | `status == "resolved"` and `matches[0]["matched_on"] == "ticker"` |
| `name` | `status == "resolved"` and `matched_on == "title"`. The title must equal the query apart from case and runs of whitespace; punctuation is no longer removed, so "Apple Inc" does not match "Apple Inc." |
| `name_suffix` | removed: no corporate suffix is added. A consumer that wants it must try its own variants and record that it did |
| `shared_name` | `status == "ambiguous"` (every candidate is listed) |
| `prefix`, `fuzzy` | removed: `not_found` |
| `none` | `not_found` |

Two things also changed. A query made of digits now matches `cik_str`. A query
equal to one company's ticker and another's title is `ambiguous`; in 0.8 the
ticker won.

Every call site reads only `hits[0]["cik"]`, `["ticker"]` and `["name"]`. In
1.0 these are `matches[0]["cik"]`, `matches[0]["record"]["ticker"]` and
`matches[0]["record"]["title"]`. The sites are `valuation/evaluate.py:52-56`,
`valuation/peer_count.py:324-327,341-347,483-488`,
`valuation/peer_search.py:414,425-427,530-537` and
`valuation/workflow.py:441-445`. The checks
`match in ("ticker", "name", "name_suffix") and len(hit) == 1` become
`status == "resolved"`. If a company has several rows, `resolved` may list
several matches that share one CIK, so test the status, not `len(matches)`.

### `edgar_mcp.edgar.xbrl_parser.XBRLParser` (removed)

`parse_company_facts(facts_json, statement_type, period_type, num_periods,
as_of)` picked statement members with the tag classifier, chose units and
periods, and returned `{"periods", "metrics", "metadata"}`. It is used at
`valuation/facts.py:17,342-458` and `sp_aggregates/payouts.py:20-21,86-94`.

Read the companyfacts document instead, with `get_companyfacts` (or
`sec_client.get_json` on
`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`). Its
`facts[taxonomy][concept]["units"][unit]` is a list of
`{start?, end, val, accn, fy, fp, form, filed, frame?}` as the SEC publishes
it. Everything `XBRLParser` did is now the consumer's choice:

- which concepts make up which statement;
- which unit to use;
- annual or quarterly, which it read from `fp`, `form` and the length of
  `start`/`end`;
- which of several values for one period to keep;
- the `as_of` cut on `filed`.

`metrics[key]["sources"][period]` (`accn`, `form`, `filed`) maps directly to
those keys of the chosen row.

### `edgar_mcp.utils.sec_client` (unchanged)

`fetch(url, max_bytes=)` still returns `(body, content_type)`, `get_json(url)`
still returns `(data, meta)` with `meta["source_url"]` and
`meta["retrieved_at"]`, and `SecNotFound` and `SecError` are unchanged. New:
`fetch_response(url, max_bytes=)` returns the response with its retrieval time.
Callers: `valuation/facts.py:18`, `valuation/companies.py:34`,
`valuation/peer_universe.py:487-495`, `sp_aggregates/identity.py:24,146-166`,
`sp_aggregates/nport.py:17,42,73`, `sp_aggregates/raw.py:9,30`.

`edgar_mcp.config.constants.USER_AGENT`, `HTTP_HEADERS` and
`sec_client.USER_AGENT` remain, so `valuation/paths.py:78-82` still works.

### Version and build checks

`edgar_mcp.__version__` is `1.0.0rc1`. `valuation/build.py:84-107` (version,
path, wheel and the `requirements.lock` pin) and
`valuation/filed_facts.py:59-69` (which requires a `facts` parameter on
`get_filed_statements`) need the new version and, for the second, the
rebuild below.

## Tools

### Removed tools

| 0.8 tool | Rebuild with |
|----------|--------------|
| `get_financial_statement` | `get_companyfacts`; the consumer chooses the concepts of each statement (see `XBRLParser` above) |
| `get_concept` | `get_companyconcept(cik_or_ticker, taxonomy, concept)`: every unit and row as published. 0.8 chose one unit, filtered by form and re-sorted. Not used by the valuation app |
| `get_filed_statements` | `get_filing_xbrl` (below) |
| `get_segment_revenue` | `get_filing_xbrl` with `part="instance"` and `concepts=[...]` (below) |
| `search_companies` | no replacement for a search by industry. Each company's `sic` and `sicDescription` are in `get_submissions` `payload`, and `search_filings` records carry `_source.sics`. Not used by the valuation app |

`EDGAR_COMPANY_INDEX`, the bundled `company_index.json`, `config/sic_codes.py`
and the industry groups are gone. Every SIC value the valuation app uses comes
either from the submissions JSON it fetches itself (`valuation/facts.py:91-94`,
`sp_aggregates/identity.py:153`) or from `search_filings` hits
(`valuation/peer_search.py:292`).

### `lookup_company` (changed)

| 0.8 | 1.0 |
|-----|-----|
| `match_type` | `status` (`resolved`, `ambiguous`, `not_found`) and `matches[].matched_on` |
| `matches[]` `{cik, name, ticker}` | `matches[]` `{cik, matched_on, record}`, where `record` is the feed row (`cik_str`, `ticker`, `title`) |
| `source_url`, `retrieved_at`, `frozen` | `sources[0]` |
| (none) | `unresolved`: an ambiguous query with its CIKs |

The match rules are those of `find_company` above. `valuation/statements.py:1079-1086`
tests `match_type in ("ticker", "name", "name_suffix")`; that becomes
`status == "resolved"`. It stores `matches[0]` as `record["company"]` and reads
`["cik"]` and `.get("name")`; the name is now `matches[0]["record"]["title"]`.

### `list_filings` (changed)

Each row is `{"record", "url", "source"}`. `record` holds every column of the
submissions table under the SEC's key, and `source` gives the file and row
index the record came from.

| 0.8 row field | 1.0 |
|---------------|-----|
| `accession_number` | `record["accessionNumber"]` |
| `form` | `record["form"]` |
| `filing_date` | `record["filingDate"]` |
| `reporting_date` | `record["reportDate"]` |
| `acceptance_datetime` | `record["acceptanceDateTime"]` |
| `description` (it held the primary document's file name) | `record["primaryDocument"]`; `record["primaryDocDescription"]` is the SEC's description |
| `items` (a list split at commas) | `record["items"]`, the string as published ("2.01,9.01"); split it if a list is wanted |
| `url` | `url`. Null, not "", when `primaryDocument` is missing or is a rendering path such as `xslF345X06/form4.xml` |

At the top level, `form`, `as_of`, `include_amendments` and `matched_on` moved
into `filters` (and `cik`); `source_url`, `retrieved_at`, `frozen` and
`cache_hit` moved into `sources`; `limit_reached` is new; `count`, `complete`
and `warnings` remain.

Behaviour changes:

- **Order.** Rows come in the SEC's order: the recent table, then each older
  file in the order listed. 0.8 merged rows by accession number and sorted them
  by filing date, newest first. The SEC's recent table is newest first, but
  1.0 does not promise it. A consumer that needs an order sorts the rows
  itself. The valuation app sorts by acceptance time in
  `valuation/statements.py:380`.
- **Form.** `form` is compared literally. 0.8 upper-cased it, defaulted to
  "10-K", and `"ALL"` kept every form. In 1.0 there is no default: without
  `form` every form is kept, and `"ALL"` is an ordinary form name that matches
  nothing. A call that relied on the 0.8 default must now pass `form="10-K"`.
  The valuation app passes literal forms ("10-K", "10-Q", "8-K"); check each
  call site above for one that omits `form`.
- **Older files.** An older file that cannot be read is an error. 0.8 gave a
  warning and a shorter list unless the caller was strict.
- **Limit.** `limit` runs to 1,000.
- **Amendments.** `include_amendments` also keeps `<form>/A`, as before.

Call sites to change: `valuation/narrative.py:82-84`,
`valuation/peer_checks.py:28-35,47-49`, `valuation/peer_count.py:135-139`,
`valuation/peer_universe.py:103-113`, `valuation/tools.py:112-117,133`,
`valuation/statements.py:380-406,1032,1089-1093`,
`valuation/filed_facts.py:129-131`, `valuation/forecast.py:108-117`,
`tests/test_beta_peers.py:251` and `tests/test_forecast.py:49-51`. Several
sites already try `primary_document_url` before `url`. Neither 0.8 nor 1.0 has
that key; `url` is the one to use.

### `list_filing_documents` (changed)

| 0.8 | 1.0 |
|-----|-----|
| `documents[].size_bytes` (an integer) | `documents[].size`, the text the index page shows |
| `documents[].readable` | removed. A file `get_filing_text` reads ends in .htm, .html, .txt or .xml |
| top-level `filing_date`, `form` | removed. Read them from `list_filings` `record` |
| (none) | `representation: "transformed"`, `transformation` (the rules), `original` (index page URL and `sha256`), `cik`, `count`, `warnings` |

`sequence`, `description`, `type`, `document` and `url` are unchanged. Used
only in `notebooks/17_annual_reports.ipynb:224`.

### `get_filing_text` (changed)

The arguments are unchanged (`url`, `find`, `start`, `max_chars`), so the
schema copy in `valuation/peer_search.py:133` still finds `url`. `text`,
`matches[]` (`start`, `excerpt`), `match_count`, `next_start`, `start`,
`total_chars`, `url` and `content` are unchanged.

| 0.8 | 1.0 |
|-----|-----|
| `sha256` | `original["sha256"]` (also `sources[0]["sha256"]`) |
| `extraction` | `transformation["name"]` (still `tags-stripped-v2`); `transformation["rules"]` lists the rules |
| `frozen`, `retrieved_at`, `cache_hit` | `sources[0]` |

`sha256` is read at `valuation/peer_count.py:154-162`,
`valuation/peer_universe.py:133-138`, `valuation/retrieval.py:162-169`,
`valuation/mapping_agent.py:268-289`, `valuation/statements.py:1040-1050` and
`valuation/tools.py:117-133`. `extraction` is read at
`valuation/retrieval.py:162-169`. The test stub at
`tests/test_beta_peers.py:251-260` returns `sha256` at the top level.

### `search_filings` (changed)

The argument names are unchanged, so the schema copy in
`valuation/peer_search.py:246-255` still works, but two behaviours changed:
`forms` has no default (0.8 searched 10-K only; 1.0 searches every form unless
`forms` is given), and `query` is sent exactly as given (0.8 collapsed runs of
whitespace). Each hit is `{"record", "url", "urls"}`, where `record` is the
search index's hit verbatim (`_id`, `_source`, ...).

| 0.8 hit field | 1.0 |
|---------------|-----|
| `accession_number`, `document` | `record["_id"]`, "<accession>:<file>"; split at the first ":" |
| `form` | `record["_source"]["form"]` |
| `document_type` | `record["_source"]["file_type"]` |
| `filing_date` | `record["_source"]["file_date"]` |
| `period_of_report` | `record["_source"]["period_ending"]` |
| `sic` | `record["_source"]["sics"]` (a list, as given) |
| `companies[]` `{name, cik, tickers}` | `record["_source"]["ciks"]` (CIKs as given) and `["display_names"]` (as given, e.g. "Winmark Corp  (WINA)  (CIK 0000908315)"). 0.8 split the name and tickers out of that text with a regex. That parse is now the consumer's, and the CIK should come from `ciks` |
| `readable` | removed; see `list_filing_documents` |
| `url` | `urls`, the address under every CIK in `_source.ciks`, in order; `url` is that address only when the hit names exactly one CIK. 0.8 built `url` from the first CIK; 1.0 sets it to null for a hit naming several and lists the hit in `unresolved` |
| top-level `total_is_exact` | `total_relation` ("eq" or "gte"), as the index gives it |
| top-level `query`, `forms`, `filed_from`, `filed_to` | `filters` |

`total_hits`, `offset`, `count` and `next_offset` are unchanged. Call sites:
`valuation/peer_count.py:382-393,438-449` and
`valuation/peer_search.py:282-300`.

### `get_filed_statements` to `get_filing_xbrl`

0.8 chose which roles were the balance sheet, income statement and cash flow
(`kind`, `parenthetical`). It chose a label per line and a value per cell. It
built `lines` with `depth`, `weight` and `calculation_parent`, and it ran
calculation and roll-forward checks. 1.0 returns the files those choices were
made from, as written, one `part` at a time, paged by `offset`/`limit`. Each
choice below is now the consumer's.

| 0.8 field | Source in 1.0 |
|-----------|---------------|
| `filing.accession_number`, `form`, `filing_date`, `reporting_date` | `part="summary"`: `filing.record` (the submissions row: `accessionNumber`, `form`, `filingDate`, `reportDate`) |
| `filing.document` | `filing.record["primaryDocument"]` within `filing.folder` |
| `filing.acceptance` (`.utc`) | `acceptance.header_utc`, the header time read as US Eastern and converted, with `header_utc_derivation`. `acceptance.header_acceptance_datetime` is the header text and `acceptance.submissions_acceptanceDateTime` is the submissions value. They can disagree, and both are returned without a choice |
| `filing.inline_xbrl` | `files.instance.index_row` (the filing index row: description `EXTRACTED XBRL INSTANCE DOCUMENT` for an instance the SEC extracted from inline XBRL, type `EX-101.INS` for one filed as such) and `files.input_files` (FilingSummary's InputFiles, with each `doctype` attribute as written; null when the filing has no FilingSummary.xml) |
| `statements[].kind`, `title`, `parenthetical` | the consumer classifies. Published inputs: each role's `definition` in `part="schema"` (e.g. "995200100 - Statement - CONSOLIDATED CONDENSED BALANCE SHEETS") and the extended link's `xlink:title`; the SEC's `FilingSummary.xml` reports (`MenuCategory`, `ShortName`, `LongName`, `Role`) in `part="summary"` `filing_summary` |
| `statements[].lines[]` (`element`, `depth`, `structure`) | `part="presentation"`: locators and arcs per extended link, with `order`, `preferredLabel`, `use`, `priority` as written. The consumer builds the tree and resolves prohibited or overriding arcs |
| `lines[].label` | `part="label"`: every label resource with its `xlink:role` and `xml:lang`. The consumer picks one, using the presentation arc's `preferredLabel` if it follows 0.8 |
| `lines[].weight`, `calculation_parent` | `part="calculation"`: summation-item arcs with `weight` and `order` (the 2003 and 2023 arcroles, as written) |
| `lines[].values[]`, `by_member[]`, `columns` | `part="instance"`: facts (`concept`, `contextRef`, `unitRef`, `decimals`, `precision`, `nil`, `text`), contexts (entity, period as written, `segment.explicit_members`, typed members) and units. Every duplicate is returned. 0.8's `conflict` (two values for one cell) is for the consumer to detect |
| `calculation_checks[]`, `roll_forward_checks[]` | the consumer computes them from the calculation arcs and the instance facts |
| `requested_facts` (`facts=`) | `part="instance"` with `concepts=["prefix:Local", "{namespace}Local" or "Local"]` (up to 50); `concept_matches` says what each name matched. `tagged_by_member` and `by_member` come from the contexts' `explicit_members` |
| `cover_shares[]`, `document_facts`, `registered_securities[]` | the cover's dei facts in `part="instance"` (for example `dei:EntityCommonStockSharesOutstanding`, `dei:EntityRegistrantName`, `dei:EntityCentralIndexKey`, `dei:Security12bTitle`, `dei:TradingSymbol`) with their contexts. The share class is the context's member, as tagged |
| `conflicts[]` | the consumer: facts with one concept, context and unit and different text |
| `sources[]` (`file`, `sha256`) | `sources[]` (`url`, `sha256`, ...) and `file` on every part |
| `company_namespace`, `extension` | `part="schema"` `target_namespace` and the namespaces of each element |

What 1.0 leaves unresolved, and says so:

- A locator whose fragment names an id that several elements of a filing
  schema share is `resolved: false` with `ambiguous: true` and every
  candidate. 0.8 and rc1 took the first element; 1.0 does not choose.
- A locator whose schema is outside the filing (the us-gaap or dei taxonomy)
  keeps its `href` and fragment and is counted in `unresolved`. The concept
  name is the fragment as written (e.g. `us-gaap_Assets`), which by the EDGAR
  Filer Manual convention is `<prefix>_<local name>`. Relying on that
  convention is the consumer's choice.
- The instance file is the one the filing index page lists as type
  `EX-101.INS` or as the `EXTRACTED XBRL INSTANCE DOCUMENT`, and the schema is
  the one the instance's `link:schemaRef` names. `found_by` says how. 0.8 (and
  rc1) derived `<document>_htm.xml`, matched `<name>-<YYYYMMDD>.xml` and took
  the one `.xsd`; 1.0 does none of that. If no file or several fit, the entry
  is unresolved with the candidates listed; pass `instance_file` or
  `schema_file` to name it. A filing without FilingSummary.xml is no longer
  refused as "no XBRL financial data": `filing_summary` is null and
  `unresolved` names the missing file.
- A fact with `xsi:nil` keeps `nil` and its text as written. No value is
  invented for it.
- A part's `roleRef` and `arcroleRef` items come from linkbases that hold an
  extended link of that part's kind, and from linkbases that hold no
  extended link any part returns (a roleRefs-only linkbase is in every
  part); rc1 dropped the latter.
- `not_returned` says what a part leaves out of the files it parses.
  Contexts, entities, periods, units, resources and schema items carry more
  keys than rc1 (`attributes`, `identifier_attributes`, `period_element`,
  `other`, `lang`, `xml`, `root`); code that compares whole dicts (for
  example `entity == {"scheme": ..., "identifier": ...}`) must compare the
  keys it needs. `part="schema"` now has an `annotation` item for each
  top-level `xs:annotation`, so item positions shift.

Call sites to change: `valuation/filed_facts.py:4,34,59-69,103,129-131,184-212,570-604,706,743,754,870`,
`valuation/statements.py:6,274,383-406,631,681,946-950,1106-1123`,
`valuation/forecast.py:9,53-57,121,210`, `valuation/workflow.py:414`,
`valuation/facts.py:259-334` (via `cover_shares`),
`valuation/peer_universe.py:455` (via `registered_securities`),
`valuation/agents.py:425`, `valuation/mapping_agent.py:202`,
`tests/test_filed_facts.py` and `tests/test_forecast.py`. The saved answers in
`tests/fixtures/filings/*.json.gz` have the 0.8 shape.

### `get_segment_revenue` to `get_filing_xbrl`

0.8 chose a revenue concept, chose the axes (business segments, products and
services), and kept one currency and one period. It reported `total_revenue`,
`sum_of_members`, `share_of_members_listed` and `skipped`. In 1.0:

1. `get_filing_xbrl(cik, form="10-K", part="instance", concepts=[...])`, where
   the consumer names the revenue concepts it accepts.
2. Each fact's context gives the period and `segment.explicit_members`
   (dimension and member as namespace and local name). A fact whose context
   has no members is the undimensioned total.
3. Member labels come from `part="label"` for the company's own members, or
   from the taxonomy for standard ones.
4. Shares, sums and the choice of axis, unit and period are the consumer's.

Used at `valuation/peer_checks.py:67-76`. It reads `axes`, `fiscal_year.end`,
`form`, `filing_date`, `source_url`, `sha256` and `total_revenue.value`.

## New tools

`get_submissions` (the submissions document, with the older files if asked),
`get_companyfacts`, `get_companyconcept` and `get_frames` return the SEC
documents unchanged. `get_filing_document` returns any file of a filing folder
byte for byte, paged. `get_filing_xbrl` is described above. None has a 0.8
counterpart to migrate from.
