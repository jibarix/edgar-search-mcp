# Response contract, version 1.0

This is the contract every tool of edgar-search-mcp 1.x keeps. The JSON Schema
in [`response_contract.schema.json`](response_contract.schema.json) states the
same rules in machine-checkable form, and `tests/test_contract.py` checks each
tool's answer against it, offline, from the frozen SEC answers in
`tests/fixtures/sec`.

## The rule

The server is a transport. It returns what the SEC and the filer published, in
the filer's own structure and order.

- It never assigns a taxonomy, category, standard name, statement type,
  industry group or any other classification.
- A regular expression is used for syntax only: to check that an argument is a
  CIK, a date or an accession number, to split a QName into prefix and local
  name, to find the rows of an HTML table. A regular expression never decides
  what a financial item or a company is.
- Where the source does not establish something, the answer says it is
  unresolved or ambiguous. The server never guesses.
- Any choice among source records is made by the caller, with a filter whose
  value is compared literally (exact string equality, or an exact date bound).

Classification, mapping, period selection, label choice, duplicate handling
and arithmetic checks belong to the consumer.
[`CONSUMER_MIGRATION.md`](CONSUMER_MIGRATION.md) explains how to rebuild each
0.8 view from the raw data.

## The envelope

Every successful answer is a JSON object with these fields.

| Field | Meaning |
|---|---|
| `contract_version` | `"1.0"`. A change that removes or renames a field, or changes its meaning, raises the major version. |
| `representation` | What the data is: see below. |
| `sources` | Every SEC response read for this answer, in the order read. Each has `url`, `retrieved_at` (UTC time the response left the SEC; for a frozen copy, the recorded time), `frozen` (served from a frozen copy), `cache_hit`, and `sha256` of the bytes as received when the server had them (a JSON answer served from the cache keeps the hash recorded when it was fetched). |
| `returned_at` | UTC time the server returned the answer. |

`representation` is one of:

- `source`: `payload` is the SEC document, parsed from JSON and returned
  unchanged: every key, every value and the original order of lists. Nothing is
  added inside `payload`.
- `source_records`: records copied verbatim from a source document (key names
  as the SEC spells them), selected by the literal filters echoed in `filters`.
  Fields the server adds beside a record (a document URL built from the CIK,
  accession number and file name; the position of the record in its source) are
  named in the tool's section below and are never mixed into the record itself.
- `parsed_source`: XML parsed into records that keep every attribute and text
  value as written, in document order, plus mechanical reference resolution
  only: a QName is resolved to a namespace URI and local name through the
  document's own namespace declarations, and an XLink locator is resolved
  through the schema it points to when that schema is in the filing.
- `transformed`: a convenience view, such as plain text extracted from HTML.
  `transformation` names the rules. It never replaces the original, which
  `get_filing_document` returns byte for byte.

### Unresolved and ambiguous

Where an answer depends on something the source does not establish, the item
carries `resolved: false` (or `status: "ambiguous"`) and a `reason`, and the
answer lists it in `unresolved`. Examples: a locator that points to a schema
outside the filing (the FASB `us-gaap` schema, say), a QName whose prefix is
not declared, a prefix declared twice with different namespaces, a company
query that matches several CIKs.

### Order, sequence and pagination

Lists keep source order. Items parsed from XML carry `seq`, their position in
the document. An `order` attribute of an arc, or a `Position` in the
FilingSummary, is returned as written and never used to reorder anything; two
arcs with the same `order` stay two arcs with the same `order`.

A tool that pages takes `offset` and `limit` and returns `total`, `offset`,
`count` and `next_offset` (null on the last page). Paging never reorders or
drops an item.

### Errors

A tool that cannot do its work raises an error whose message starts with a
code: `INVALID_INPUT`, `AMBIGUOUS_COMPANY`, `NOT_FOUND`, `RATE_LIMITED`,
`UPSTREAM_UNAVAILABLE`, `UNSUPPORTED_DOCUMENT`, `NO_USER_AGENT`,
`NO_FROZEN_COPY`. A normal answer with nothing in it always means the SEC had
nothing, never that a request failed.

## Identifying a company

Tools that take `cik_or_ticker` accept a CIK (digits), an exact ticker or an
exact company title from the SEC's `company_tickers.json`, compared without
regard to case and with runs of spaces read as one. Nothing looser is accepted:
no suffix, prefix or fuzzy matching. A title shared by several CIKs is refused
with `AMBIGUOUS_COMPANY` and the candidates.

## Tools

### `lookup_company(query)` — `source_records`

Exact identifier lookup in `company_tickers.json`. `status` is `resolved` (one
CIK), `ambiguous` (several CIKs) or `not_found`. `matches` lists every row that
matched, each as `{"cik": <10 digits>, "matched_on": "cik" | "ticker" |
"title", "record": <the row as published>}`. A company with several tickers
has several rows; they share one CIK.

### `get_submissions(cik_or_ticker, include_older_files=False)` — `source`

The SEC submissions document unchanged, including `sic` and `sicDescription`
as the SEC publishes them. With `include_older_files`, `older_files` lists each
file named in `filings.files`, each as `{"name", "payload"}`.

### `list_filings(cik_or_ticker, form="10-K", as_of=None, limit=10, include_amendments=False)` — `source_records`

Rows of the submissions filing tables, in the SEC's order (the recent table,
then each older file in the order `filings.files` lists them). `record` holds
every column under the SEC's own key (`accessionNumber`, `filingDate`,
`reportDate`, `acceptanceDateTime`, `form`, `primaryDocument`,
`primaryDocDescription`, `items`, ...). Beside it: `url`, the primary
document's address built from the CIK, accession number and `primaryDocument`;
and `source`, the file and row index the record came from. `form` is compared
literally (`include_amendments` also keeps `<form>/A`); `as_of` keeps rows
whose `filingDate` is on or before it.

### `list_filing_documents(url)` — `transformed`

The rows of the filing's index page, parsed from its HTML table (sequence,
description, type and size as the page shows them). The index page itself is
available from `get_filing_document`.

### `get_filing_document(url, offset=0, max_bytes=1000000)` — `source`

Any file inside one filing folder, byte for byte, in pages. `data_base64` holds
the page; `text` holds the same bytes decoded as UTF-8 when they decode
strictly, else null. `sha256` and `total_bytes` describe the whole file.

### `get_filing_text(url, find=None, start=0, max_chars=12000)` — `transformed`

Plain text extracted from an HTML or text document by the rules named in
`transformation`, with `original` pointing at the file and its `sha256`.
Offsets count characters of the extracted text.

### `search_filings(query, forms, filed_from, filed_to, offset, limit)` — `source_records`

Hits of the SEC full-text search. Each hit keeps the search index's own record
(`_id` and `_source`) verbatim in `record`, beside the document `url` built from
it.

### `get_companyfacts(cik_or_ticker, taxonomy=None, concept=None)` — `source`, or `source_records` when filtered

The companyfacts document. With `taxonomy` (and optionally `concept`) the
`facts` object keeps only those literal keys; every unit and observation under
them is unchanged.

### `get_companyconcept(cik_or_ticker, taxonomy, concept)` — `source`

The companyconcept document unchanged: every unit and every observation.

### `get_frames(taxonomy, concept, unit, period)` — `source`

The frames document unchanged. `period` is `CY2023`, `CY2023Q4` or
`CY2023Q4I`.

### `get_filing_xbrl(cik_or_ticker, accession_number=None, form="10-K", as_of=None, part="summary", concepts=None, offset=0, limit=500)` — `parsed_source`

The filing is `accession_number`, else the first row of the company's
submissions, in the SEC's order (the recent table, then each older file in
the order `filings.files` lists them), whose `form` equals `form` exactly.
`as_of` is a date, YYYY-MM-DD, compared literally with the row's
`filingDate`: rows filed after it are skipped, and an `accession_number`
filed after it is not found. `filing` holds the `accessionNumber`, the
submissions row as the SEC publishes it (`record`), where that row is
(`source`: file and index) and the filing `folder`.

Every item parsed from XML has `seq` and `node`, the kind of XML node it is
(`context`, `unit`, `fact`, `loc`, `arc`, `resource`, `element`, ...); `node`
names the XML construct, never a meaning. Attribute names in a namespace are
written `{namespace}local`, so `xlink:role` is
`{http://www.w3.org/1999/xlink}role`; attributes without a namespace keep
their plain name. A QName in an attribute or in text is returned as written,
with the namespace and local name it resolves to through the declarations in
scope at that element, or `resolved: false` and a reason.

The instance and the company schema are found from `FilingSummary.xml`: the
one `InputFiles` entry with a `doctype` attribute is the instance (for an
inline XBRL document, the instance the SEC extracted from it,
`<document stem>_htm.xml`), and the one `.xsd` entry is the schema. Each
`files` entry says how it was found (`found_by`); when that is not exactly
one file, the entry is `resolved: false` with its candidates.

The XBRL files of one filing, one `part` at a time:

- `summary`: the filing's submissions record, its acceptance time as the
  filing header states it, the files read and how each was found, and the
  SEC's `FilingSummary.xml` as an element tree (every report with its `Role`,
  `LongName`, `MenuCategory`, `HtmlFileName` and `Position` as written).
- `schema`: the company schema's declarations in document order: elements with
  every attribute (`name`, `id`, `type`, `substitutionGroup`, `abstract`,
  `nillable`, `xbrli:periodType`, `xbrli:balance`, ...), role types with their
  `definition` and `usedOn`, arcrole types, imports and linkbase references.
  Namespaces whose declarations are outside the filing are listed in
  `unresolved`.
- `presentation`, `calculation`, `definition`, `label`, `reference`: every
  extended link of that kind, flattened to its locators, resources and arcs in
  document order. Each keeps every attribute (`xlink:role`, `xlink:arcrole`,
  `order`, `weight`, `priority`, `use`, `preferredLabel`, `xml:lang`, ...).
  Prohibited arcs, repeated arcs and several parents of one concept are all
  returned. Label resources keep their language, role and text; no language is
  preferred and no label is invented.
- `instance`: the instance in document order: contexts (entity identifier and
  scheme, period as written, explicit members, typed members as XML), units
  (measures, divide) and facts (concept as namespace URI plus local name,
  `contextRef`, `unitRef`, `decimals`, `precision`, `nil`, the text exactly as
  written). Duplicates are all returned; none is chosen.

`concepts` (with `part="instance"`) keeps only facts of the named concepts and
the contexts and units they use. A name is `prefix:local` (prefix resolved
through the instance's own declarations), `{namespace}local`, or a bare local
name, which returns every concept with that local name in any namespace;
`concept_matches` says which concepts each name matched.

## What the server never returns

Statement types or `kind`, parenthetical flags, structural roles inferred from
name suffixes, chosen columns, balances moved between periods, values promoted
from members, roll-forward or calculation checks, a preferred label or
language, a merged or "best" duplicate, cover-page interpretations, segment
selections or shares, industry groups, or companies ranked by similarity.
