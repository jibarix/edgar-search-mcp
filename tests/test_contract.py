"""The 1.0 response contract, checked offline against the frozen SEC answers.

Run from the repository root (no network):
    python -X utf8 tests/test_contract.py

docs/response_contract.schema.json lists the 1.0 tools and their answers.
Each tool is called through its real wrapper with the network blocked, its
answer is validated against the schema, and no key the contract forbids
(an inferred classification) may appear anywhere in it. Where a tool returns
an SEC document unchanged, the answer is compared with the frozen bytes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = os.path.join(ROOT, "tests", "fixtures", "sec")
sys.path.insert(0, ROOT)
os.environ["EDGAR_FROZEN_DIR"] = SEC
os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
os.environ.pop("EDGAR_RECORD_DIR", None)

from jsonschema import Draft202012Validator  # noqa: E402

from edgar_mcp import server  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402

with open(os.path.join(ROOT, "docs", "response_contract.schema.json"), encoding="utf-8") as f:
    SCHEMA = json.load(f)
with open(os.path.join(ROOT, "docs", "RESPONSE_CONTRACT.md"), encoding="utf-8") as f:
    DOC = f.read()
with open(os.path.join(ROOT, "tests", "fixtures", "outputs_0.8.0", "_about.json"), encoding="utf-8") as f:
    ABOUT = json.load(f)

# 1.0 tools whose 1.0 answer is not implemented yet (absent, or still in
# their 0.8 shape), and 0.8 tools not yet removed. Both shrink step by step;
# in the release both are empty.
PENDING = {"lookup_company", "get_submissions", "list_filings", "list_filing_documents", "get_filing_document",
           "get_filing_text", "search_filings"}
NOT_YET_REMOVED = {"get_segment_revenue", "search_companies"}
# Files removed from the package, and the import names that must not come back.
REMOVED_FILES = ["edgar_mcp/edgar/tag_classifier.py", "edgar_mcp/edgar/xbrl_parser.py",
                 "edgar_mcp/edgar/data/sec_tag_mapping.json", "edgar_mcp/edgar/filed_statements.py"]
REMOVED_NAMES = ["tag_classifier", "xbrl_parser", "TagClassifier", "XBRLParser", "sec_tag_mapping",
                 "filed_statements", "get_filed_statements"]

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def no_network(*a, **k):
    raise AssertionError("the network was used")


sec_client._client.stream = no_network


def frozen_json(url):
    with open(os.path.join(SEC, hashlib.sha256(url.encode("utf-8")).hexdigest() + ".body"), "rb") as f:
        return json.load(f)


def frozen_bytes(url):
    with open(os.path.join(SEC, hashlib.sha256(url.encode("utf-8")).hexdigest() + ".body"), "rb") as f:
        return f.read()


def call(name, **kwargs):
    try:
        return asyncio.run(getattr(server, name)(**kwargs)), None
    except Exception as e:
        return None, str(e)


def forbidden_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if k in SCHEMA["x-forbidden-keys"]:
                found.add(k)
            # SEC documents and the filer's own attributes are returned as published.
            if k not in ("payload", "record", "older_files", "attributes"):
                forbidden_keys(v, found)
    elif isinstance(value, list):
        for v in value:
            forbidden_keys(v, found)
    return found


def validate(tool, result, label):
    validator = Draft202012Validator({"$ref": f"#/$defs/{tool}", "$defs": SCHEMA["$defs"]})
    errors = sorted(validator.iter_errors(result), key=lambda e: list(e.path))
    check(f"{label}: matches the contract", not errors,
          "; ".join(f"{list(e.path)}: {e.message[:120]}" for e in errors[:3]))
    keys = forbidden_keys(result)
    check(f"{label}: carries no inferred classification", not keys, str(sorted(keys)))


def schema_cases():
    Draft202012Validator.check_schema(SCHEMA)
    check("the schema is valid JSON Schema 2020-12", True)
    for tool in SCHEMA["x-tools"]:
        check(f"the contract document describes {tool}", f"`{tool}(" in DOC)
        check(f"the schema defines the answer of {tool}", tool in SCHEMA["$defs"])
    check("the document and the schema give one contract version",
          f"version {SCHEMA['x-contract-version']}" in DOC.splitlines()[0])


def inventory_cases():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    wanted, removed = set(SCHEMA["x-tools"]), set(SCHEMA["x-removed-tools"])
    check("every 1.0 tool is in the server or declared pending", wanted - PENDING <= names,
          str(sorted(wanted - PENDING - names)))
    check("every removed tool is gone or declared not yet removed", not (names & removed - NOT_YET_REMOVED),
          str(sorted(names & removed - NOT_YET_REMOVED)))
    check("no tool is declared not yet removed once it is gone", NOT_YET_REMOVED <= names,
          str(sorted(NOT_YET_REMOVED - names)))
    check("the server has no tool outside the contract", names <= wanted | NOT_YET_REMOVED,
          str(sorted(names - wanted - NOT_YET_REMOVED)))


def tool_cases():
    for tool, kwargs in TOOL_CALLS:
        if tool in PENDING:
            continue
        result, error = call(tool, **kwargs)
        label = f"{tool}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})"[:110]
        check(f"{label}: answers offline from frozen copies", error is None, str(error)[:200])
        if error is None:
            validate(tool, result, label)
            for extra in EXTRA_CHECKS.get(tool, ()):
                extra(result, kwargs)


CIK = "0000908315"
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
CONCEPT_URL = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{CIK}/us-gaap/Revenues.json"


def sha_of(url):
    return hashlib.sha256(frozen_bytes(url)).hexdigest()


def source_is(url, label):
    def extra(result, kwargs):
        s = result["sources"]
        check(f"{label}: names its one source with the hash of the frozen bytes",
              len(s) == 1 and s[0]["url"] == url and s[0]["sha256"] == sha_of(url) and s[0]["frozen"] is True,
              str(s)[:200])
    return extra


def companyfacts_checks(result, kwargs):
    raw = frozen_json(FACTS_URL)
    taxonomy, concept = kwargs.get("taxonomy"), kwargs.get("concept")
    label = f"get_companyfacts taxonomy={taxonomy!r} concept={concept!r}"
    source_is(FACTS_URL, label)(result, kwargs)
    check(f"{label}: filters echoed as given", result["filters"] == {"taxonomy": taxonomy, "concept": concept})
    if taxonomy is None:
        check(f"{label}: the payload is the SEC document unchanged",
              result["payload"] == raw and result["representation"] == "source")
        check(f"{label}: the key order of the document is kept",
              list(result["payload"]) == list(raw)
              and list(result["payload"]["facts"]["us-gaap"]) == list(raw["facts"]["us-gaap"]))
        return
    if taxonomy not in raw["facts"]:
        expected = {}
    elif concept is None:
        expected = {taxonomy: raw["facts"][taxonomy]}
    else:
        expected = {taxonomy: {concept: raw["facts"][taxonomy][concept]} if concept in raw["facts"][taxonomy] else {}}
    check(f"{label}: facts keeps only the literal keys, every unit and observation unchanged",
          result["payload"]["facts"] == expected and result["representation"] == "source_records",
          str(result["payload"]["facts"])[:160])
    check(f"{label}: every other key of the document is unchanged",
          {k: v for k, v in result["payload"].items() if k != "facts"}
          == {k: v for k, v in raw.items() if k != "facts"})


def companyconcept_checks(result, kwargs):
    source_is(CONCEPT_URL, "get_companyconcept")(result, kwargs)
    raw = frozen_json(CONCEPT_URL)
    check("get_companyconcept: the payload is the SEC document unchanged, every unit kept",
          result["payload"] == raw and list(result["payload"]["units"]) == list(raw["units"])
          and all(result["payload"]["units"][u] == raw["units"][u] for u in raw["units"]))


def frames_checks(result, kwargs):
    source_is(ABOUT["frame"], "get_frames")(result, kwargs)
    check("get_frames: the payload is the SEC document unchanged", result["payload"] == frozen_json(ABOUT["frame"]))


ACCESSION = ABOUT["accession_number"]
FOLDER = f"https://www.sec.gov/Archives/edgar/data/908315/{ACCESSION.replace('-', '')}/"
INSTANCE_URL = FOLDER + "wina-20251227x10k_htm.xml"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
XBRLI_NS, LINK_NS = "http://www.xbrl.org/2003/instance", "http://www.xbrl.org/2003/linkbase"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def raw_instance():
    """The frozen instance read with the standard library alone, to compare against."""
    root = ET.fromstring(frozen_bytes(INSTANCE_URL))
    facts = [el for el in root if not el.tag.startswith(("{" + XBRLI_NS + "}", "{" + LINK_NS + "}"))]
    contexts = {el.get("id"): el for el in root.iter("{%s}context" % XBRLI_NS)}
    return root, facts, contexts


def qname_of(el):
    return tuple(el.tag[1:].split("}"))


def xbrl_sources_checks(result, kwargs):
    label = f"get_filing_xbrl {kwargs}"[:90]
    bad = [s["url"] for s in result["sources"] if s["sha256"] != sha_of(s["url"]) or s["frozen"] is not True]
    check(f"{label}: every source carries the hash of its frozen bytes", not bad, str(bad))
    row = result["filing"]
    recent = frozen_json(SUBMISSIONS_URL)["filings"]["recent"]
    i = row["source"]["index"]
    check(f"{label}: the filing record is the SEC row at its stated index, every column as published",
          row["source"]["file"] == f"CIK{CIK}.json#filings.recent"
          and row["record"] == {k: v[i] for k, v in recent.items()} and row["accessionNumber"] == ACCESSION)


def xbrl_summary_checks(result, kwargs):
    xbrl_sources_checks(result, kwargs)
    files = result["files"]
    check("get_filing_xbrl summary: the instance is the SEC extraction of the one doctype file",
          files["instance"]["resolved"] and files["instance"]["url"] == INSTANCE_URL, str(files["instance"]))
    check("get_filing_xbrl summary: the schema is the one .xsd in InputFiles",
          files["schema"]["resolved"] and files["schema"]["name"].endswith(".xsd"), str(files["schema"]))
    header = frozen_bytes(FOLDER + f"{ACCESSION}-index-headers.html").decode("latin-1")
    raw = result["acceptance"]["header_acceptance_datetime"]
    check("get_filing_xbrl summary: the header acceptance time is returned as written",
          raw is not None and f"<ACCEPTANCE-DATETIME>{raw}" in header.replace("\n", ""), str(raw))
    check("get_filing_xbrl summary: the submissions acceptanceDateTime is returned as published",
          result["acceptance"]["submissions_acceptanceDateTime"] == result["filing"]["record"]["acceptanceDateTime"])
    tree = result["filing_summary"]
    summary = ET.fromstring(frozen_bytes(FOLDER + "FilingSummary.xml"))
    check("get_filing_xbrl summary: FilingSummary.xml is returned as its element tree",
          tree["tag"] == "FilingSummary" and len(tree["children"]) == len(list(summary)))
    roles = [r.findtext("Role") for r in summary.find("MyReports").findall("Report")]
    mine = next(c for c in tree["children"] if c["tag"] == "MyReports")
    got = [next((g["text"] for g in c["children"] if g["tag"] == "Role"), None) for c in mine["children"]
           if c["tag"] == "Report"]
    check("get_filing_xbrl summary: every report is kept, in the file's order", got == roles and roles,
          f"{len(got)} {len(roles)}")


def xbrl_instance_checks(result, kwargs):
    xbrl_sources_checks(result, kwargs)
    root, facts, contexts = raw_instance()
    items = result["items"]
    got = [i for i in items if i["node"] == "fact"]
    seqs = [i["seq"] for i in items]
    check("get_filing_xbrl instance: items keep document order", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
    if kwargs.get("concepts") is None and kwargs.get("limit") == 5000:
        check("get_filing_xbrl instance: one item per fact element, none merged or dropped",
              result["page"]["total"] == len(items) and len(got) == len(facts), f"{len(got)} vs {len(facts)}")
        check("get_filing_xbrl instance: one item per context and unit element",
              sum(i["node"] == "context" for i in items) == len(contexts)
              and sum(i["node"] == "unit" for i in items) == len(root.findall("{%s}unit" % XBRLI_NS)))
        nil = [f for f in facts if f.get(XSI_NIL) is not None]
        got_nil = [f for f in got if f["nil"] is not None]
        check("get_filing_xbrl instance: xsi:nil is kept as written, and a nil fact has no text",
              len(got_nil) == len(nil) > 0 and all(f["nil"] == el.get(XSI_NIL) for f, el in zip(got, facts))
              and all(not f["text"] for f in got_nil), f"{len(got_nil)} vs {len(nil)}")
        check("get_filing_xbrl instance: concept, contextRef, unitRef, decimals and text as written",
              all((f["concept"]["namespace"], f["concept"]["local_name"]) == qname_of(el)
                  and f["contextRef"] == el.get("contextRef") and f["unitRef"] == el.get("unitRef")
                  and f["decimals"] == el.get("decimals") and f["precision"] == el.get("precision")
                  and (f["text"] or "") == (el.text or "") for f, el in zip(got, facts)))
    wrong = []
    for c in (i for i in items if i["node"] == "context"):
        ident = contexts[c["id"]].find("{%s}entity/{%s}identifier" % (XBRLI_NS, XBRLI_NS))
        if c["entity"] != {"scheme": ident.get("scheme"), "identifier": ident.text}:
            wrong.append(c["id"])
    check("get_filing_xbrl instance: each context keeps its entity identifier and scheme", not wrong, str(wrong[:3]))
    if kwargs.get("concepts"):
        matched = {(m["namespace"], m["local_name"]) for e in result["concept_matches"] for m in e["matches"]}
        want = [el for el in facts if qname_of(el) in matched]
        check(f"get_filing_xbrl concepts={kwargs['concepts']}: every fact of the matched concepts, and only those",
              len(got) == len(want) > 0
              and all((f["concept"]["namespace"], f["concept"]["local_name"]) in matched for f in got),
              f"{len(got)} vs {len(want)}")
        refs = {f["contextRef"] for f in got}
        check(f"get_filing_xbrl concepts={kwargs['concepts']}: exactly the contexts the facts use",
              {i["id"] for i in items if i["node"] == "context"} == refs)


def xbrl_bare_name_checks(result, kwargs):
    xbrl_instance_checks(result, kwargs)
    _, facts, _ = raw_instance()
    names = sorted({qname_of(el) for el in facts if qname_of(el)[1] == "Revenues"})
    entry = result["concept_matches"][0]
    check("get_filing_xbrl: a bare name matches that local name in every namespace the instance uses",
          bool(names) and sorted((m["namespace"], m["local_name"]) for m in entry["matches"]) == names, str(entry))


def xbrl_paging_checks(result, kwargs):
    xbrl_instance_checks(result, kwargs)
    full, error = call("get_filing_xbrl", cik_or_ticker="908315", accession_number=ACCESSION, part="instance",
                       limit=5000)
    pages, offset = [], 0
    while offset is not None and error is None:
        page, error = call("get_filing_xbrl", cik_or_ticker="908315", accession_number=ACCESSION, part="instance",
                           offset=offset, limit=kwargs["limit"])
        if error is None:
            pages += page["items"]
            offset = page["page"]["next_offset"]
    check("get_filing_xbrl: pages put together give every item once, in order",
          error is None and pages == full["items"] and result["page"]["count"] == kwargs["limit"], str(error))


XLINK = "{http://www.w3.org/1999/xlink}"
XS_NS = "http://www.w3.org/2001/XMLSchema"
LINK_PARTS = {"presentation": "presentationLink", "calculation": "calculationLink", "definition": "definitionLink",
              "label": "labelLink", "reference": "referenceLink"}


def frozen_schema():
    name = next(n for n in input_files() if n.endswith(".xsd"))
    return name, ET.fromstring(frozen_bytes(FOLDER + name))


def input_files():
    summary = ET.fromstring(frozen_bytes(FOLDER + "FilingSummary.xml"))
    return [f.text for f in summary.find("InputFiles")]


def xbrl_schema_checks(result, kwargs):
    xbrl_sources_checks(result, kwargs)
    name, root = frozen_schema()
    items = result["items"]
    elements = [i for i in items if i["node"] == "element"]
    raw = root.findall("{%s}element" % XS_NS)
    check("get_filing_xbrl schema: one item per top-level xs:element, every attribute as written and in order",
          len(elements) == len(raw) > 0 and all(list(i["attributes"].items()) == list(el.attrib.items())
                                                for i, el in zip(elements, raw)), f"{len(elements)} vs {len(raw)}")
    roles = [(i["attributes"].get("roleURI"), i["definition"]) for i in items if i["node"] == "roleType"]
    raw_roles = [(r.get("roleURI"), r.findtext("{%s}definition" % LINK_NS)) for r in root.iter("{%s}roleType" % LINK_NS)]
    check("get_filing_xbrl schema: every roleType with its definition as written, in order",
          roles == raw_roles and roles, f"{len(roles)} vs {len(raw_roles)}")
    imports = [i["attributes"].get("schemaLocation") for i in items if i["node"] == "import"]
    outside = [u["schemaLocation"] for u in result["unresolved"] if "schemaLocation" in u]
    check("get_filing_xbrl schema: every import is listed, and each outside the filing is unresolved",
          imports == [i.get("schemaLocation") for i in root.findall("{%s}import" % XS_NS)] and outside == imports,
          f"{len(imports)} {len(outside)}")
    check("get_filing_xbrl schema: the target namespace is the schema's",
          result["target_namespace"] == root.get("targetNamespace") and result["file"]["name"] == name)
    seqs = [i["seq"] for i in items]
    check("get_filing_xbrl schema: items keep document order", seqs == sorted(seqs))


def xbrl_linkbase_checks(result, kwargs):
    xbrl_sources_checks(result, kwargs)
    part = kwargs["part"]
    wanted = "{%s}%s" % (LINK_NS, LINK_PARTS[part])
    schema_name, _ = frozen_schema()
    label = f"get_filing_xbrl {part}"
    check(f"{label}: names the extended-link element it returns", result["extended_link_element"] == wanted)
    items = result["items"]
    for entry in result["linkbases"]:
        root = ET.fromstring(frozen_bytes(entry["url"]))
        mine = [i for i in items if i["file"] == entry["name"]]
        links = [l for l in root if l.tag == wanted and l.get(XLINK + "type") == "extended"]
        children = [c for l in links for c in l]
        check(f"{label} {entry['name']}: one item per extended link and per child, none dropped or merged",
              entry["extended_links"] == len(links)
              and sum(i["node"] == "extendedLink" for i in mine) == len(links)
              and sum(i["node"] in ("loc", "arc", "resource", "other") for i in mine) == len(children),
              f"{len(links)} links, {len(children)} children")
        got = [i["attributes"] for i in mine if i["node"] in ("loc", "arc", "resource", "other")]
        check(f"{label} {entry['name']}: every attribute of every child as written, in document order",
              got == [dict(c.attrib) for c in children])
        texts = [i["text"] for i in mine if i["node"] == "resource"]
        check(f"{label} {entry['name']}: resource text as written",
              texts == [c.text for c in children if c.get(XLINK + "type") == "resource"])
    locs = [i for i in items if i["node"] == "loc"]
    own = [l for l in locs if l["href"].split("#")[0] == schema_name]
    check(f"{label}: every locator into the company schema resolves to one of its elements",
          all(l["target"]["resolved"] for l in own), str([l["href"] for l in own if not l["target"]["resolved"]][:3]))
    external = sum(u["locators"] for u in result["unresolved"] if "locators" in u)
    check(f"{label}: every other locator is counted unresolved", external == len(locs) - len(own),
          f"{external} vs {len(locs) - len(own)}")
    if part == "label":
        langs = {r["attributes"].get("{http://www.w3.org/XML/1998/namespace}lang")
                 for r in items if r["node"] == "resource"}
        raw_langs = {c.get("{http://www.w3.org/XML/1998/namespace}lang")
                     for e in result["linkbases"] for c in ET.fromstring(frozen_bytes(e["url"])).iter()
                     if c.get(XLINK + "type") == "resource"}
        check(f"{label}: every language as written", langs == raw_langs and None not in langs, str(langs))
    if part == "calculation":
        arcroles = {a["attributes"][XLINK + "arcrole"] for a in items if a["node"] == "arc"}
        check(f"{label}: arcroles as written", bool(arcroles), str(arcroles))


def xbrl_checks(result, kwargs):
    """One dispatcher for the get_filing_xbrl calls in TOOL_CALLS."""
    if kwargs.get("part", "summary") == "summary":
        xbrl_summary_checks(result, kwargs)
    elif kwargs["part"] == "schema":
        xbrl_schema_checks(result, kwargs)
    elif kwargs["part"] in LINK_PARTS:
        xbrl_linkbase_checks(result, kwargs)
    elif kwargs.get("concepts") == ["Revenues"]:
        xbrl_bare_name_checks(result, kwargs)
    elif kwargs.get("limit") == 300:
        xbrl_paging_checks(result, kwargs)
    else:
        xbrl_instance_checks(result, kwargs)


TOOL_CALLS = [
    ("get_companyfacts", {"cik_or_ticker": "908315"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "dei"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "revenues"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "US-GAAP"}),
    ("get_companyconcept", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "SalesTypeLeaseSellingProfitLoss", "unit": "USD",
                    "period": "CY2023"}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"]}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "form": "10-K", "as_of": "2026-03-01"}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "instance", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "instance",
                         "concepts": ["us-gaap:Revenues", "dei:EntityCommonStockSharesOutstanding"]}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "instance",
                         "concepts": ["Revenues"]}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "instance", "limit": 300}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "schema", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "presentation", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "calculation", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "definition", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "label", "limit": 5000}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "part": "reference"}),
]
EXTRA_CHECKS = {"get_companyfacts": [companyfacts_checks], "get_companyconcept": [companyconcept_checks],
                "get_frames": [frames_checks], "get_filing_xbrl": [xbrl_checks]}
# Calls that must fail, with the code the message starts with.
ERROR_CALLS = [
    ("get_companyfacts", {"cik_or_ticker": "908315", "concept": "Revenues"}, "INVALID_INPUT"),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "FranchiseRevenue", "unit": "USD", "period": "CY2023"},
     "NOT_FOUND"),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "Revenues", "unit": "USD", "period": "FY2023"},
     "INVALID_INPUT"),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "Revenues", "unit": "../x", "period": "CY2023"},
     "INVALID_INPUT"),
    ("get_companyconcept", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Rev/../x"},
     "INVALID_INPUT"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": "0000908315-26-999999"}, "NOT_FOUND"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"], "as_of": "2026-02-24"}, "NOT_FOUND"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "form": "10-k"}, "NOT_FOUND"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "as_of": "2026-02-25T00:00:00Z"}, "INVALID_INPUT"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "concepts": ["Revenues"]}, "INVALID_INPUT"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "part": "instance", "concepts": ["a b"]}, "INVALID_INPUT"),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "part": "instance", "concepts": []}, "INVALID_INPUT"),
]


def error_cases():
    for tool, kwargs, code in ERROR_CALLS:
        if tool in PENDING:
            continue
        result, error = call(tool, **kwargs)
        check(f"{tool}({kwargs}) fails with {code}", result is None and str(error).startswith(code + ":"),
              str(error)[:160])


def removal_cases():
    import subprocess
    for path in REMOVED_FILES:
        check(f"removed from the package: {path}", not os.path.exists(os.path.join(ROOT, path)))
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
        pyproject = f.read()
    check("pyproject ships no data files for the removed classifier", '"edgar_mcp.edgar"' not in pyproject)
    left = []
    for folder, _, files in os.walk(os.path.join(ROOT, "edgar_mcp")):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    text = f.read()
                left += [f"{name}: {n}" for n in REMOVED_NAMES if n in text]
    check("no module refers to the removed classifier or parser", not left, str(left))
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    env["EDGAR_FROZEN_DIR"] = SEC
    started = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import asyncio, edgar_mcp.server as s; print(len(asyncio.run(s.mcp.list_tools())))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    check("the server starts without the removed files", started.returncode == 0 and started.stdout.strip().isdigit(),
          started.stderr[-300:])


def main():
    schema_cases()
    inventory_cases()
    tool_cases()
    error_cases()
    removal_cases()
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
