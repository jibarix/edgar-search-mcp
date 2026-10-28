"""The 1.0.0 freeze (plan v5, change 4): tests T19 and T20.

T19: over stdio, every tool serves a self-contained outputSchema (or the
contract resource); every successful answer on the frozen copies validates
against it, and its text block is the same JSON. T20: the consumer kit runs
tests/test_contract.py against a working-tree wheel unpacked into an empty
folder, with the source tree off the path. Run from the repository root:
    python -X utf8 tests/test_schema.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = os.path.join(ROOT, "tests", "fixtures", "sec")
sys.path.insert(0, ROOT)

from jsonschema import Draft202012Validator  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

with open(os.path.join(ROOT, "tests", "fixtures", "outputs_0.8.0", "_about.json"), encoding="utf-8") as f:
    ABOUT = json.load(f)
FOLDER = "https://www.sec.gov/Archives/edgar/data/908315/000090831526000007/"
# One successful call per tool, on the frozen copies.
CALLS = [
    ("lookup_company", {"query": "WINA"}),
    ("get_submissions", {"cik_or_ticker": "908315"}),
    ("list_filings", {"cik_or_ticker": "908315", "limit": 3}),
    ("list_filing_documents", {"url": ABOUT["main_document"]}),
    ("get_filing_document", {"url": FOLDER + "FilingSummary.xml"}),
    ("get_filing_document", {"url": "https://data.sec.gov/submissions/CIK0000908315.json", "max_bytes": 2000}),
    ("get_filing_text", {"url": ABOUT["exhibit"], "max_chars": 500}),
    ("search_filings", {"query": '"Winmark"', "forms": "10-K", "filed_from": "2024-01-01", "filed_to": "2024-12-31"}),
    ("get_companyfacts", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_companyconcept", {"cik_or_ticker": "908315", "taxonomy": "us-gaap", "concept": "Revenues"}),
    ("get_frames", {"taxonomy": "us-gaap", "concept": "SalesTypeLeaseSellingProfitLoss", "unit": "USD",
                    "period": "CY2023"}),
    ("get_filing_xbrl", {"cik_or_ticker": "908315", "accession_number": ABOUT["accession_number"]}),
]
BUILDER = r"C:\ProgramData\miniconda3\python.exe" if os.name == "nt" else sys.executable

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def environment(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    env["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
    env["EDGAR_RATE_FILE"] = os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "state")
    return {**env, **extra}


def refs(node):
    """Every $ref in a schema."""
    if isinstance(node, dict):
        return ([node["$ref"]] if isinstance(node.get("$ref"), str) else []) + [r for v in node.values() for r in refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in refs(v)]
    return []


async def t19():
    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT,
                                   env=environment(EDGAR_FROZEN_DIR=SEC))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            schemas = {name: t.output_schema for name, t in tools.items()}
            missing = sorted(n for n, s in schemas.items() if not s)
            resource = None
            if missing:
                try:
                    read_back = await session.read_resource("edgar://contract/schema")
                    resource = json.loads(read_back.contents[0].text)
                except Exception as e:  # noqa: BLE001
                    resource = None
                    check("T19 the contract resource is served", False, str(e)[:120])
            check("T19 every tool serves an outputSchema, or the contract resource is served",
                  not missing or resource is not None, f"without: {missing}")
            for name, schema in schemas.items():
                if not schema:
                    continue
                local = [r for r in refs(schema) if not r.startswith("#")]
                inner = [r for r in refs(schema) if r.startswith("#/$defs/")]
                ok = not local and all(r.split("/")[-1] in schema.get("$defs", {}) for r in inner)
                check(f"T19 {name}: the outputSchema is self-contained (every $ref bundled)",
                      ok and schema.get("$id") in (None, f"edgar://contract/1.0/{name}"), str(local or inner)[:120])
                Draft202012Validator.check_schema(schema)
            for name, arguments in CALLS:
                result = await session.call_tool(name, arguments)
                label = f"T19 {name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())[:60]})"
                if result.is_error:
                    check(f"{label}: succeeds", False, result.content[0].text[:140] if result.content else "")
                    continue
                data = result.structured_content
                schema = schemas.get(name) or {"$ref": f"#/$defs/{name}", "$defs": (resource or {}).get("$defs", {})}
                errors = list(Draft202012Validator(schema).iter_errors(data)) if isinstance(data, dict) else ["none"]
                check(f"{label}: structuredContent validates against the served schema", not errors,
                      "; ".join(str(getattr(e, "message", e))[:100] for e in errors[:2]))
                text = json.loads(result.content[0].text) if result.content else None
                check(f"{label}: the text block is the same JSON", text == data)
            result = await session.call_tool("get_submissions", {"cik_or_ticker": "Winmark"})
            text = result.content[0].text if result.content else ""
            check("T19 a coded error passes through the client as isError, its code readable",
                  result.is_error and text.split("get_submissions: ", 1)[-1].startswith("NOT_FOUND:"), text[:100])
    from edgar_mcp import server
    with open(os.path.join(ROOT, "docs", "RESPONSE_CONTRACT.md"), encoding="utf-8") as f:
        doc = f.read()
    check("T19 the error list includes RATE_LIMITER_UNAVAILABLE (server and contract)",
          "RATE_LIMITER_UNAVAILABLE" in (server.__doc__ or "") and "RATE_LIMITER_UNAVAILABLE" in doc)


def t20():
    out = tempfile.mkdtemp(prefix="edgar_wheel_test_")
    build = subprocess.run([sys.executable, "-X", "utf8", os.path.join(ROOT, "tests", "build_wheel.py"), out,
                            "--builder", BUILDER], cwd=ROOT, capture_output=True, text=True, timeout=600)
    wheels = [n for n in os.listdir(out) if n.endswith(".whl")]
    check("T20 the working-tree test wheel builds, with its record", build.returncode == 0 and len(wheels) == 1
          and os.path.exists(os.path.join(out, "build_record.json")), (build.stdout + build.stderr)[-300:])
    if not wheels:
        return
    unpacked = tempfile.mkdtemp(prefix="edgar_wheel_unpacked_")
    with zipfile.ZipFile(os.path.join(out, wheels[0])) as z:
        z.extractall(unpacked)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_") and k != "PYTHONPATH"}
    env.update(EDGAR_TEST_INSTALLED="1", PYTHONPATH=unpacked)
    run = subprocess.run([sys.executable, "-X", "utf8", os.path.join(ROOT, "tests", "test_contract.py")],
                         cwd=tempfile.mkdtemp(), env=env, capture_output=True, text=True, timeout=600)
    ok = run.stdout.count("\nOK ") + run.stdout.startswith("OK ")
    check("T20 test_contract passes against the unpacked wheel", run.returncode == 0 and "problems: 0" in run.stdout,
          f"{ok} OK; " + "; ".join(l for l in run.stdout.splitlines() if l.startswith("FAIL"))[:300] + run.stderr[-300:])
    check("T20 the package was imported from the unpacked wheel, not the source tree",
          f"imported from {unpacked}" in run.stdout, run.stdout[:200])


if __name__ == "__main__":
    chosen = set(sys.argv[1:])
    for name, case in (("T19", lambda: asyncio.run(t19())), ("T20", t20)):
        if chosen and name not in chosen:
            continue
        try:
            case()
        except Exception as e:  # noqa: BLE001
            check(f"{name} ran to the end", False, f"{type(e).__name__}: {e}"[:200])
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
