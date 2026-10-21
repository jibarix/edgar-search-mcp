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
           "get_filing_text", "search_filings", "get_companyfacts", "get_companyconcept", "get_frames",
           "get_filing_xbrl"}
NOT_YET_REMOVED = {"get_financial_statement", "get_concept", "get_filed_statements", "get_segment_revenue",
                   "search_companies"}

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


TOOL_CALLS = []
EXTRA_CHECKS = {}


def main():
    schema_cases()
    inventory_cases()
    tool_cases()
    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
