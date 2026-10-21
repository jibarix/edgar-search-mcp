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
           "get_filing_text", "search_filings", "get_filing_xbrl"}
NOT_YET_REMOVED = {"get_filed_statements", "get_segment_revenue", "search_companies"}
# Files removed from the package, and the import names that must not come back.
REMOVED_FILES = ["edgar_mcp/edgar/tag_classifier.py", "edgar_mcp/edgar/xbrl_parser.py",
                 "edgar_mcp/edgar/data/sec_tag_mapping.json"]
REMOVED_NAMES = ["tag_classifier", "xbrl_parser", "TagClassifier", "XBRLParser", "sec_tag_mapping"]

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
            if k not in ("payload", "record", "older_files"):  # SEC documents are returned as published
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


TOOL_CALLS = [
    ("get_companyfacts", {"cik_or_ticker": "908315"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "dei"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "revenues"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "US-GAAP"}),
    ("get_companyconcept", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "SalesTypeLeaseSellingProfitLoss", "unit": "USD",
                    "period": "CY2023"}),
]
EXTRA_CHECKS = {"get_companyfacts": [companyfacts_checks], "get_companyconcept": [companyconcept_checks],
                "get_frames": [frames_checks]}
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
