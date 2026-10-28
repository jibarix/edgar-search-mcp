# Changelog

## Lock update after 1.0.0 (2026-10-28)

- **`requirements.lock`: certifi 2026.7.22 → 2026.10.23.** 2026.7.22 carries
  [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
  (also PYSEC-2026-4320, rated Critical): it trusts an emSign root linked to
  compromised intermediate keys. HTTPX checks every SEC connection against
  certifi's bundle. The package code and the 1.0.0 wheel are unchanged,
  because the wheel does not pin certifi. Install with the lock from this
  commit or later, not the one at tag v1.0.0.

## 1.0.0

The first release whose contract is frozen: "The public API of 1.x" in
`docs/RESPONSE_CONTRACT.md` says what every 1.x release keeps and how it may
change. Differences from 1.0.0rc4:

- **More addresses for `get_filing_document`**: besides a file of one filing
  folder, it returns `https://www.sec.gov/files/company_tickers_exchange.json`
  and the submissions files
  `https://data.sec.gov/submissions/CIK<10 digits>.json` and
  `CIK<10 digits>-submissions-<3 digits>.json`, byte for byte, matched exactly
  as written. Redirects are no longer followed.
- **One request limiter for every process on the machine**: a state file
  (`EDGAR_RATE_FILE`, default `%LOCALAPPDATA%\sec-edgar-rate\state` on
  Windows, `$XDG_STATE_HOME/sec-edgar-rate/state` elsewhere) read and written
  under a file lock. Each request passes a last check within 25 ms of its
  admission before it is handed to the network; across processes sharing the
  file, at most 10 of those last checks fall in any one second. Delivery after
  the check is best effort. `EDGAR_RATE_LIMIT` (new; default 8, at most 10;
  the server does not start with an invalid value) spaces one process's
  admissions; it is not a strict per-process bound. rc4 paced each process
  alone at 8 a second.
- **New error code `RATE_LIMITER_UNAVAILABLE`**: nothing is sent. A lock not
  obtained within 5 seconds fails that call only. A file that cannot be opened
  or read fails every call until the path is fixed. A state that is not whole
  and valid (a cut-short write, an empty file, a checksum mismatch) fails
  every call until
  `python -m edgar_mcp.ratelimit --reset --reason "<why>"`, which keeps the old
  bytes in `reset.log` and holds requests for 60 seconds. A missing state file
  is created whole, with a hard link, so its folder must support hard links
  (choose it with `EDGAR_RATE_FILE`); where it does not, every call is refused
  and no state file is left. A reset stopped part way leaves an invalid file,
  never an empty one. A clock inconsistency is logged with both clocks' readings and
  in `clock.log`. An invalid `EDGAR_RATE_LIMIT` also gives this code.
- **Pauses the SEC asks for hold every process**: a 429, a 403, or a 5xx with
  `Retry-After`. While more than 60 seconds remain, calls fail with
  `RATE_LIMITED` without sending; with 60 seconds or less, they wait. A 403
  without `Retry-After` now pauses every process for 600 seconds, a local
  policy; in rc4 it held nothing. A 5xx without `Retry-After` and a timeout
  wait in the one call only.
- **The in-memory caches have no expiry**: the last 32 files read through
  `get_filing_document` and the text of the last eight documents stay in
  memory in every mode, recording and replay included (the disk cache is off
  in those modes). A company file read that way can be days old in a
  long-running server; its `retrieved_at` says when.
- **Recordings are captured once and never replaced**: with
  `EDGAR_RECORD_DIR`, an address whose description exists is replayed
  (`frozen: true`, the recorded `retrieved_at`) instead of fetched again. The
  body file is now named `<sha256 of url>.<sha256 of body>.body` and the
  description names it in a new `body` field; each file is published whole by
  a hard link. When two processes capture one address, the first description
  wins; the other answers with its metadata, or `UPSTREAM_UNAVAILABLE` if the
  bytes differ. A folder without hard links is refused. rc4 overwrote every
  recording on each fetch. Frozen copies in the rc4 layout are still read.
- **Each tool serves its answer schema as `outputSchema`**, self-contained,
  with `$id` `edgar://contract/1.0/<tool>`; successful results carry
  `structuredContent` that validates against it, plus the same JSON as text.
  The tools' Python return type is now `dict[str, Any]`.
- **The JSON Schema moved into the package**:
  `edgar_mcp/response_contract.schema.json` (was
  `docs/response_contract.schema.json`), shipped in the wheel through the new
  `MANIFEST.in` and read by `edgar_mcp.contract`.
- **Consumer kit**: `tests/test_contract.py` runs against an installed package
  with `EDGAR_TEST_INSTALLED=1` and says which package it imported;
  `tests/build_wheel.py` builds a test wheel from a clean copy of the working
  tree with a record of every hash.
- **Server start**: refuses to start when the test-only `EDGAR_TEST_HOOKS` is
  set.
- **Docs**: the public API, versioning rules, configuration, recording layout
  and limiter are stated in `docs/RESPONSE_CONTRACT.md`;
  `docs/CONSUMER_MIGRATION.md` names version `1.0.0` and the new error code.

`contract_version` stays `"1.0"`; rc1 to rc4 carried it across breaking
changes, and from 1.0.0 a breaking change raises it.

## 1.0.0rc4

A review of 1.0.0rc3 found unread references missing from linkbase answers.
**Breaking** against rc3:

- **Every unread reference in every part**: each address that a locator,
  `roleRef` or `arcroleRef` anywhere in the filing (any linkbase, or the
  instance) names and that is not read (outside the filing, not one file of
  the folder, no href) is listed once in `unresolved` by every schema and
  linkbase part, with `schema`, `references` (counts by element),
  `referenced_from` and `reason`. Linkbase parts add `locators`, the count of
  their own locators into it, possibly 0. rc3 listed them only for
  `part="schema"`; a linkbase part named only outside schemas its own
  locators pointed to (so `part="label"` could return empty `items` and empty
  `unresolved` while a presentation locator named an unread schema), and
  references into the folder that name no single file, or with no href, were
  dropped in every part.

## 1.0.0rc3

A review of 1.0.0rc2 found three places where an answer depended on the
request or hid what the source left open, and a migration check that could
not see stale files. **Breaking** against rc2:

- **Whole DTS first**: `get_filing_xbrl` follows every reference of the
  instance, schemas and linkbases (`linkbaseRef`, `roleRef`, `arcroleRef`,
  `import`, `include` and every locator of every extended link) to its end
  before it selects the requested part. rc2 followed only the requested
  part's references, so a label linkbase reached through a presentation
  locator's schema was missing from `part="label"`, with nothing in
  `unresolved`. The schema part names schemas outside the filing it was
  pointed to, with a count of references by element.
- **Index read with an HTML parser**: `list_filing_documents` and the index
  step of `get_filing_xbrl` read the index page with the standard library's
  `html.parser`, so an href is read however it is quoted. Rows the rules
  cannot read are listed in the new required `skipped_rows` and in
  `warnings` ("left out a row the rules cannot read (line N): reason",
  replacing the old wording for a link outside the folder). While any row is
  skipped, `get_filing_xbrl` asserts no unique file from the index: the
  entry is `resolved: false`, and `files.index.skipped_rows` and
  `unresolved` carry the skipped rows.
- **`numbers_as_text` on every record**: each `search_filings` hit, each
  `list_filings` row, each `lookup_company` match and `get_filing_xbrl`'s
  `filing` has its own `numbers_as_text`, paths from that `record`, so a
  number kept as text is told apart from a string the SEC sent. No response
  type is exempt.
- **Migration check**: the installed-content check in
  `CONSUMER_MIGRATION.md` compares the install with the pinned wheel or the
  release manifest, walking every installed file under `edgar_mcp/` and the
  dist-info, so missing, modified and unexpected files fail. It no longer
  trusts the installed `RECORD`.

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
