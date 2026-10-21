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

Exact identifier lookup in `company_tickers.json`. `status` is `resolved` (the
matching rows share one CIK), `ambiguous` (they name several CIKs; the answer
also lists the query and those CIKs in `unresolved`) or `not_found`. `matches`
lists every row that matched, in the feed's order, each as `{"cik": <10
digits>, "matched_on": "cik" | "ticker" | "title", "record": <the row as
published>}`. A company with several tickers has several rows; they share one
CIK. A CIK missing from the feed can still be given to every other tool.

### `get_submissions(cik_or_ticker, include_older_files=False)` — `source`

The SEC submissions document unchanged, including `sic` and `sicDescription`
as the SEC publishes them. With `include_older_files`, `older_files` lists each
file named in `filings.files`, in the order listed, each as `{"name",
"payload"}` with the file unchanged.

### `list_filings(cik_or_ticker, form=None, as_of=None, limit=10, include_amendments=False)` — `source_records`

Rows of the submissions filing tables, in the SEC's order (the recent table,
then each older file in the order `filings.files` lists them); nothing is
re-sorted, merged or de-duplicated. `record` holds every column under the
SEC's own key (`accessionNumber`, `filingDate`, `reportDate`,
`acceptanceDateTime`, `form`, `primaryDocument`, `primaryDocDescription`,
`items`, ...). Beside it: `url`, the primary document's address built from the
CIK, accession number and `primaryDocument` (null when either is missing, or
when `primaryDocument` is a path such as `xslF345X06/form4.xml`: that names an
SEC rendering, not a file of the filing folder; the record keeps it as
published); and `source`, the file and row index the record came from.

`form` is compared literally (`include_amendments` also keeps `<form>/A`).
Without `form` (the default) every row is kept, whatever its form: no form is
assumed. `as_of` keeps rows whose `filingDate` is on or
before it; a row without a `filingDate` in YYYY-MM-DD form cannot be placed
against `as_of`, so it is left out and named in `warnings`. Older files are read
only as far as `limit` needs, and one that cannot be read is an error, never a
shorter list. `filters` echoes the arguments, `limit_reached` says the list
stopped at `limit`, and `complete` is false when `warnings` names a row left out
or a column cut short.

### `list_filing_documents(url)` — `transformed`

The rows of the filing's index page (`<accession>-index.htm`), read from its
HTML table by the rules listed in `transformation`: `sequence`, `description`,
`type` and `size` as the page shows them, the file name (`document`) and its
`url`. `original` names the index page and its `sha256`; `get_filing_document`
returns it byte for byte. A link outside the filing folder is left out and
named in `warnings`.

### `get_filing_document(url, offset=0, max_bytes=1000000)` — `source`

Any file inside one filing folder, byte for byte, in pages of up to 5,000,000
bytes. `data_base64` holds `length` bytes from `offset`; `text` holds the same
bytes decoded as UTF-8 when they decode strictly, else null (a page boundary
can split a character). `sha256` and `total_bytes` describe the whole file and
`content_type` is the SEC's. `next_offset` is null on the last page.

### `get_filing_text(url, find=None, start=0, max_chars=12000)` — `transformed`

Plain text extracted from an HTML or text document by the rules listed in
`transformation` (`name` changes whenever a rule changes), with `original`
pointing at the file, its `sha256` and `get_filing_document`. Offsets count
characters of the extracted text.

### `search_filings(query, forms=None, filed_from=None, filed_to=None, offset=0, limit=25)` — `source_records`

Hits of the SEC full-text search, in the SEC's order. `query` is sent exactly
as given (whitespace included; it must not be empty or only whitespace).
`forms` is one form type or several separated by commas, sent as given;
without it (the default) every form is searched. Each hit keeps the search
index's own record (`_id`, `_source` and any other key it returns) verbatim in
`record`. Beside it, `urls` lists the document's address under each CIK of
`_source.ciks`, in that order, built with the accession number and file name
in `_id` (empty when those are missing). `url` is that address when the hit
names exactly one CIK; a hit naming several CIKs (co-registrants file one
document under each) has `url` null and is listed in `unresolved` with its
CIKs: the server does not choose one. `total_hits` and `total_relation` are the index's `total.value`
and `total.relation`; `offset`, `count` and `next_offset` page through the
hits; `filters` echoes the arguments.

### `get_companyfacts(cik_or_ticker, taxonomy=None, concept=None)` — `source`, or `source_records` when filtered

The companyfacts document. With `taxonomy` (and optionally `concept`) the
`facts` object keeps only those literal keys; every unit and observation under
them is unchanged.

### `get_companyconcept(cik_or_ticker, taxonomy, concept)` — `source`

The companyconcept document unchanged: every unit and every observation.

### `get_frames(taxonomy, concept, unit, period)` — `source`

The frames document unchanged. `period` is `CY2023`, `CY2023Q4` or
`CY2023Q4I`.

### `get_filing_xbrl(cik_or_ticker, accession_number=None, form=None, as_of=None, part="summary", concepts=None, offset=0, limit=500, instance_file=None, schema_file=None)` — `parsed_source`

The filing is `accession_number`, else the first row of the company's
submissions, in the SEC's order (the recent table, then each older file in
the order `filings.files` lists them), whose `form` equals `form` exactly;
without `form` (the default) the first row of any form. No form is assumed.
`as_of` is a date, YYYY-MM-DD, compared literally with the row's
`filingDate`: rows filed after it are skipped, and an `accession_number`
filed after it is not found. A row whose `filingDate` is missing or not a
YYYY-MM-DD date cannot be placed against `as_of`: it is skipped and named in
`warnings`, and an `accession_number` naming such a row is refused with
`NOT_FOUND` and that reason; it is never taken to be earlier. `filing` holds the `accessionNumber`, the
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

The instance and the company schema are found only from what the filing
publishes; no file name is derived, matched against a pattern or picked by
its suffix, and the folder's directory listing is never read:

- the instance is `instance_file` when the caller names it, else the one row
  of the filing index page (`<accession>-index.htm`) whose type is
  `EX-101.INS` or whose description is `EXTRACTED XBRL INSTANCE DOCUMENT` (the
  instance the SEC extracted from an inline XBRL document); `index_row` is
  that row as the page shows it;
- the schema is `schema_file` when the caller names it, else the file the
  instance's one `link:schemaRef` names (its `xlink:href` resolved against
  the instance's URL and any inherited `xml:base`), else, when the instance is
  unresolved, the one index row typed `EX-101.SCH`.

Each `files` entry says how it was found (`found_by`). Zero or several
candidates (two rows listed as the instance, two `schemaRef`s, one pointing
outside the filing) leave the entry `resolved: false` with the reason and
its `candidates`, and the same in `unresolved`; the caller then names the
file. `instance_file` and `schema_file` are plain file names of the filing
folder (no path); one that is not there is `NOT_FOUND`. A filing without
`FilingSummary.xml` is not an error: `filing_summary` is null,
`files.input_files` is null, and `unresolved` names `FilingSummary.xml` and
what is missing with it (its report list and `InputFiles`). An index page
that is missing is named in `unresolved` the same way.

The schema and linkbase parts read every file the filing's own files lead
to (its discoverable taxonomy set, as far as it lies inside the filing): from
each schema read, the schemas its `xs:import` and `xs:include` name
(`schemaLocation`), the linkbases its `link:linkbaseRef` names and those
written inside it; the linkbases the instance names with `link:linkbaseRef`;
and a schema of the filing that a locator points to. Every href and
`schemaLocation` is resolved against the base URI in effect where it is
written (the file's URL, changed by any inherited `xml:base`). Each file is
read once; a reference back to a file already read stops there. `schemas`
lists every schema reached, with `name`, `url`, `read`, `target_namespace`
and `discovered_by` (the file, `seq` and element of the reference). A file
that is not read (outside the filing, missing, unreadable, or referenced by
an instance that is itself unresolved) is named in `unresolved` with the
reason, so an empty `items` with an empty `unresolved` means every dependency
was read and none held anything of that part.

The XBRL files of one filing, one `part` at a time:

- `summary`: the filing's submissions record, its acceptance time as the
  filing header states it, the files read and how each was found
  (`files.filing_summary` and `files.index` with `read`, `files.input_files`,
  `files.instance`, `files.schema`), and the SEC's `FilingSummary.xml` as an
  element tree (every report with its `Role`, `LongName`, `MenuCategory`,
  `HtmlFileName` and `Position` as written), or null when there is none.
- `schema`: the declarations of the company schema and of every schema of the
  filing it imports or includes, file by file in discovery order and in
  document order within a file (each item has `file`), each by its XML
  construct (`import`, `include`, `linkbaseRef`, `roleType`,
  `arcroleType`, `element`, `linkbase` for one written inside the schema,
  `other` as XML): elements with every attribute (`name`, `id`, `type`,
  `substitutionGroup`, `abstract`, `nillable`, `xbrli:periodType`,
  `xbrli:balance`, ...) and `type` and `substitutionGroup` resolved as QNames;
  role and arcrole types with their `definition` as written and `usedOn`.
  An `import`, `include` or `linkbaseRef` also has `url`, its
  `schemaLocation` or `href` resolved. `target_namespace` and `namespaces` are
  the company schema's; each schema's own `target_namespace` is in `schemas`.
  Imported schemas outside the filing, a QName that does not resolve and an
  id two elements share are listed in `unresolved`.
- `presentation`, `calculation`, `definition`, `label`, `reference`: every
  extended link of that element (`extended_link_element`, for instance
  `{http://www.xbrl.org/2003/linkbase}calculationLink`) in every linkbase
  reached as described above: those of the schemas in discovery order, then
  those the instance references. `linkbases` lists each reference with
  `seq`, `href`, `url`, `name`, `read`, `discovered_by`, its namespaces and how
  many extended links of this kind it holds (or, for one inside a schema,
  `embedded_in`); one the filing lacks or outside the filing is `read: false`
  and unresolved, and a second reference to a linkbase already read has
  `repeat_of` and no items of its own. `schemas` is as for `schema`, and
  imported schemas outside the filing are listed in `unresolved` here too. Items are flattened in document order: the linkbase's
  `roleRef` and `arcroleRef` (only from a linkbase holding a link of this
  kind), each `extendedLink`, then its children by `xlink:type`: `loc` (with
  `href` as written), `arc`, `resource` (with `text`, and `xml` when it holds
  markup) or `other` (as XML). Each child has `file` and `link_seq`, the
  `seq` of its extended link. Every attribute is kept (`xlink:role`,
  `xlink:arcrole`, `order`, `weight`, `priority`, `use`, `preferredLabel`,
  `xml:lang`, ...). Prohibited and overriding arcs, repeated arcs, equal
  orders and several parents of one concept are all returned as written;
  nothing is applied, combined or sorted. Label resources keep their
  language, role and text; no language is preferred and no label is
  invented. A locator and a `roleRef` or `arcroleRef` keep `href` as written
  and add `url`, the href resolved against the base URI in effect where it is
  written (the file's URL changed by every inherited `xml:base`, outermost
  first). A locator's `target` is the element of the filing's schema at that
  `url` (`file`) that is the only one with the `id` its fragment names
  (`namespace`, `local_name`, `schema_seq`). When several elements of the
  schema have that id, the target is `resolved: false`, `ambiguous: true`,
  with every `candidates` entry and none chosen, and the shared id is in
  `unresolved`; a
  locator into a schema outside the filing (the US GAAP taxonomy, say) is
  `resolved: false` with `schema` and `fragment`, and each such schema is
  listed once in `unresolved` with its count of locators. The fragment is
  the schema's own element id, which the filer and the taxonomy write;
  this server does not read external taxonomies, so it does not turn a
  fragment into a concept name. An extended link of an element no part
  returns (a generic link, say) is listed in `unresolved`.
- `instance`: the instance in document order: contexts (entity identifier and
  scheme, period as written, explicit members, typed members as XML), units
  (measures, divide) and facts (concept as namespace URI plus local name,
  `contextRef`, `unitRef`, `decimals`, `precision`, `nil` as written in
  `xsi:nil`, the text exactly as written). Duplicates are all returned; none is chosen.

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
