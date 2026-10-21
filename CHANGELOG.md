# Changelog

## 1.0.0rc2

A review of 1.0.0rc1 found places where the server still chose or guessed.
**Breaking** against rc1:

- **No implicit filter**: `list_filings`, `get_filing_xbrl` and
  `search_filings` no longer default to form 10-K. Without a form every form is
  kept (searched, for `search_filings`). The search query is sent exactly as
  given, whitespace included. A search hit naming several CIKs no longer gets a
  `url` built from the first one: `url` is null, `urls` lists the address under
  every CIK, and the hit is in `unresolved`. In `get_filing_xbrl`, a row with no
  `filingDate` never counts as on or before `as_of`: it is skipped and named in
  `warnings`, and an accession number naming one is refused.
- **Document discovery from published references only**: `get_filing_xbrl`
  finds the instance from the filing index page (the one row typed
  `EX-101.INS` or described `EXTRACTED XBRL INSTANCE DOCUMENT`) and the schema
  from the instance's `link:schemaRef`. It no longer derives
  `<document>_htm.xml`, matches `<name>-<YYYYMMDD>.xml`, takes the one `.xsd`
  or reads the folder's `index.json` listing. Zero or several candidates are
  unresolved with the candidates named; new parameters `instance_file` and
  `schema_file` name the file. A filing without `FilingSummary.xml` answers
  with `filing_summary` null and the missing file in `unresolved`, instead of
  `UNSUPPORTED_DOCUMENT` "no XBRL financial data". The summary's `files` adds
  `filing_summary` and `index` with their read status.
- **Every dependency followed or named**: the schema and linkbase parts
  follow local `xs:import` and `xs:include`, every schema's
  `link:linkbaseRef` and embedded linkbases, the instance's
  `link:linkbaseRef`, and locators into other schemas of the filing,
  recursively, reading each file once. Hrefs are resolved against the
  effective base URI (inherited `xml:base`). Anything not read is in
  `unresolved`, including imported schemas outside the filing in the linkbase
  parts. New `schemas` list; schema items carry `file`; locator targets carry
  `file`; `linkbases` entries carry `discovered_by`.
- **Locators**: every locator, `roleRef` and `arcroleRef` href is resolved
  against its effective base URI (inherited `xml:base`) and returned as
  `url` beside the `href` as written. An id that several schema elements
  share no longer resolves to the first: the target is `resolved: false`,
  `ambiguous: true`, with all candidates. In the library, `read_schema`'s
  `ids` maps each id to a list, and `concept_at` and `locator_resolver` do the
  lookup.
- **Parsed responses keep what was written, or say what they do not**: every
  `get_filing_xbrl` part has `not_returned`. Instance, schema and linkbase
  files are given with their document element and its attributes (`root`)
  and their namespace declarations below it (`namespace_declarations`).
  Facts, label and reference resources and schema documentation carry
  `lang`, the `xml:lang` in effect, inherited included. Context children
  keep every attribute (entity, identifier, period and its children,
  segment, scenario), and unknown children are kept as XML. A top-level
  `xs:annotation` is an `annotation` item with its documentation. A linkbase
  that holds only roleRefs, or no link any part returns, gives its roleRefs
  to every part, and other non-link children of a linkbase are `other`
  items. Each `linkbases` entry counts its extended links by element.
- **Tuples**: `concepts` finds facts written inside tuples, at any depth,
  and returns each with `path`, the elements it is nested in. A name that
  matches a tuple (an element with no contextRef) is reported in
  `unsupported` and `unresolved` instead of an empty match. Without
  `concepts`, a tuple's `other` item lists its `nested_facts`.
- **Exact JSON numbers**: SEC JSON is no longer parsed into binary floats
  that change a value (rc1 turned `9007199254740993.0` into
  `9007199254740992.0` and `1e400` into an invalid `Infinity`). A number a
  double carries exactly stays a JSON number; any other is returned as the
  text the SEC wrote, a string, and `get_submissions`, `get_companyfacts`,
  `get_companyconcept` and `get_frames` list where in `numbers_as_text`
  (empty in the usual case). Cache entries written by rc1 are fetched again.
  In the library, `sec_client.decode_json` does the parsing.

## 1.0.0rc1

The server becomes a transport: it returns what the SEC and the filer
published, in the filer's own structure and order, and never classifies.
Every answer follows the response contract
([docs/RESPONSE_CONTRACT.md](docs/RESPONSE_CONTRACT.md), with a JSON Schema),
and [docs/CONSUMER_MIGRATION.md](docs/CONSUMER_MIGRATION.md) says how to
rebuild each removed view from the raw data.

**Breaking.** Every tool's answer changed shape.

- **Envelope**: every answer has `contract_version` ("1.0"),
  `representation` (`source`, `source_records`, `parsed_source` or
  `transformed`), `sources` (each SEC response read, with `url`,
  `retrieved_at`, `frozen`, `cache_hit` and `sha256`) and `returned_at`. The
  top-level `source_url`, `retrieved_at`, `frozen` and `cache_hit` of 0.8 moved
  into `sources`.
- **Removed tools**: `get_financial_statement` (statement grouping by a tag
  classifier), `get_concept` (unit choice, filtering, re-sorting),
  `get_filed_statements` (statement, label and value choices, calculation and
  roll-forward checks), `get_segment_revenue` (revenue concept and axis
  choices), `search_companies` (industry groups from a bundled index).
- **New tools**: `get_submissions`, `get_companyfacts`, `get_companyconcept`
  and `get_frames` return the SEC documents unchanged; `get_filing_xbrl`
  returns one filing's schema, linkbases and instance as written, one part at a
  time; `get_filing_document` returns any file of a filing folder byte for
  byte.
- **`lookup_company`** matches exactly: a CIK, a ticker, or a title equal to
  the query (case and runs of whitespace aside). The suffix, prefix and fuzzy
  matching of 0.8 is gone. Every matching row of `company_tickers.json` comes
  back as published, with `status` `resolved`, `ambiguous` or `not_found`, and
  ambiguous queries in `unresolved`. The same rule resolves `cik_or_ticker`
  in every tool.
- **`list_filings`** returns the submissions rows in the SEC's order with every
  column under the SEC's key and the file and row each came from. `form` is
  compared literally (`form=None` keeps every form, replacing `ALL`). An older
  file that cannot be read is an error, never a shorter list. `limit` runs to
  1,000.
- **`list_filing_documents`** is marked `transformed`, gives `size` as the page
  shows it, and names the index page and its hash in `original`. `readable` is
  gone.
- **`get_filing_text`** is marked `transformed`, lists its extraction rules and
  points at the original file and its hash.
- **`search_filings`** returns each hit of the search index verbatim in
  `record` (names, forms, dates and SIC codes as given) with the index's
  `total_relation`; `total_is_exact` and the parsed hit fields are gone.
- **Removed modules and data**: `xbrl_parser`, `tag_classifier`,
  `sec_tag_mapping.json`, `filed_statements`, `company_classifier`,
  `config/sic_codes.py` and the bundled `company_index.json`; the
  `EDGAR_COMPANY_INDEX` variable and the `INDEX_UNAVAILABLE` error.
  `company_lookup.get_company_tickers()` is replaced by `company_tickers()`,
  and `find_company()` returns `(status, matches, meta)`.
- **Cache**: a cache read never deletes an entry; an entry that cannot be read
  is a miss, and writes are atomic.
- **Tests**: every test runs offline from frozen SEC answers
  (`tests/fixtures/sec`), and `tests/test_contract.py` checks every tool
  against the contract.

## 0.8.0

- **Named facts by member**: each fact asked for with `facts=` in
  `get_filed_statements` now also gives `by_member`, every numeric value
  tagged under members with its period, unit, decimals and dimensions (a
  conflict on its own cell), so a figure a filing tags only by award type or
  segment can be read member by member. `tagged_by_member` still counts them.

## 0.7.0

- **Statements as filed**: opening and closing balances are read per column
  (before, a "beginning of period" line showed the closing figure: GE's and
  Apple's cash); a cash roll-forward check; `complete` and
  `missing_in_every_column` on each sum check; a conflict on its own cell;
  `facts=` for named facts from the notes, text facts included (the lines a
  lease liability is presented in, with their element names). Two presentation
  arcs that differ only in their order or preferred label are two lines, as
  XBRL 2.1 defines equivalent arcs (none of eight filings checked changed).

## 0.6.0

- **Statements as filed**: a new tool, `get_filed_statements`, reads one
  filing's primary statements from its own XBRL files: the company's lines,
  labels, own elements and calculations, the cover's registered securities
  and shares outstanding per class, and the SEC's acceptance time.
- **Filing list**: each filing from `list_filings` also carries
  `acceptance_datetime`, the SEC's acceptance time as the submissions feed
  gives it.

## 0.5.1

- **Server version**: the server reports its own version (`0.5.1`) when a
  session starts, so a client's record says which release answered. Before,
  it reported the MCP SDK's default. The version is set in one place,
  `edgar_mcp/__init__.py`, and `pyproject.toml` reads it from there.

## 0.5.0

- **Segments**: one currency and one period for every figure, with what was
  left out in `skipped`. Units are matched by their ISO 4217 measure, not
  their ID. Only standard-taxonomy concepts and axes are read. When an axis
  needs two concepts, both are named in `concepts`. Filing history errors are
  reported, not taken for "no annual report".
- **Search**: an answer of the wrong shape is an error, not zero hits.
  `offset` runs to 9,999, so the last page under the 10,000 ceiling can be
  reached.
- **Provenance**: a recording and the live answer share one retrieval time,
  and a replay reports it with `frozen: true`. `lookup_company` says when the
  ticker list was read; a cached answer keeps its first retrieval time.
- **Retries**: a 503 with a long `Retry-After` is `RATE_LIMITED`.
- **Classification**: interest expense is an expense; dividends and
  restricted stock issued are equity movements; two labels keep the
  taxonomy's meaning. `metadata.unclassified` says what was left out.
- **Company index**: it now records its build time and quarters. A builder
  command and `EDGAR_COMPANY_INDEX` were added, and the snapshot no longer
  claims a build date.
- **Packaging**: everything is under `edgar_mcp/`. The cache and a built index
  live in the user's folders, so an ordinary `pip install` works.
- **Filer text**: `get_filing_text` answers say the text is the filer's,
  material to read and not instructions; `search_filings` says the same of
  company names.
