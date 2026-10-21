"""Frozen copies, offline: record, replay, refuse a tampered copy, and replay
the saved tool outputs with the network blocked.

Run from the repository root (no network, no EDGAR_USER_AGENT needed):
    python -X utf8 tests/test_frozen.py

1. The record and replay machinery of sec_client, with a stand-in transport
   in place of the SEC.
2. Every SEC answer kept under tests/fixtures/sec still matches its hash.
3. Every tool output saved under tests/fixtures/outputs_0.8.0 (written by
   tests/capture_fixtures.py) is replayed from those copies. A tool that kept
   its 0.8.0 behaviour must give the same answer; a tool removed or changed in
   1.0 is listed in REPLACED with the reason, and its answer is checked
   against the 1.0 contract by tests/test_contract.py instead.
"""
import asyncio
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(HERE, "tests", "fixtures")
SEC = os.path.join(FIXTURES, "sec")
OUTPUTS = os.path.join(FIXTURES, "outputs_0.8.0")
URLS = {
    "submissions": "https://data.sec.gov/submissions/CIK0000908315.json",
    "missing": "https://data.sec.gov/submissions/CIK0000000000.json",
    "unrecorded": "https://data.sec.gov/submissions/CIK0000320193.json",
}
# Tool outputs of 0.8.0 that 1.0 no longer reproduces, and why. Each is
# covered by docs/CONSUMER_MIGRATION.md and by tests/test_contract.py.
REPLACED = {
    "get_financial_statement_is": "removed in 1.0 (step 3): it grouped concepts into statements with a tag "
                                  "classifier; get_companyfacts returns the SEC document unchanged",
    "get_concept": "removed in 1.0 (step 3): it chose a unit, filtered and re-sorted rows; "
                   "get_companyconcept returns the SEC document unchanged",
    "get_filed_statements": "changed in 1.0 (step 4): the call used `facts`, which was removed with its "
                            "requested_facts; get_filing_xbrl part=\"instance\" with `concepts` returns those "
                            "facts as written",
}
# Fields that differ between a live answer and its replay by design. The
# capture read some addresses more than once, and a frozen copy keeps the
# time of the last reading, so retrieved_at can differ too.
VOLATILE = {"returned_at", "frozen", "retrieved_at"}
# Keys 1.0 adds to an answer that otherwise kept its 0.8.0 shape. They are
# ignored only where the saved answer does not have them, so every saved key,
# a saved sha256 included, must still match.
ADDED = {"sha256": "step 3: the hash of the SEC bytes, kept with every retrieval record"}


def body_of(url):
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()
    with open(os.path.join(SEC, name + ".body"), "rb") as f:
        return f.read()


def child(mode):
    """Runs in a fresh process, so the settings read the environment set for it."""
    sys.path.insert(0, HERE)
    import httpx
    from edgar_mcp.utils import sec_client
    out = {}
    if mode == "record":
        def answer(request):
            if str(request.url) == URLS["submissions"]:
                return httpx.Response(200, content=body_of(URLS["submissions"]),
                                      headers={"Content-Type": "application/json"})
            return httpx.Response(404)
        sec_client._client = httpx.Client(transport=httpx.MockTransport(answer))
        sec_client.USER_AGENT = "test test@example.com"
    else:
        def no_network(*a, **k):
            raise AssertionError("the network was used")
        sec_client._client.stream = no_network
    if mode == "replay":
        print(json.dumps(replay()))
        return
    for name in ("submissions", "missing") + (("unrecorded",) if mode == "frozen" else ()):
        try:
            body, content_type = sec_client.fetch(URLS[name])
            out[name] = {"ok": len(body), "type": content_type, "head": body[:40].decode("utf-8", "replace")}
        except Exception as e:
            out[name] = {"error": type(e).__name__, "code": getattr(e, "code", "")}
    print(json.dumps(out))


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def same(new, saved):
    """Equal, except that keys in ADDED may appear in `new` where `saved` lacks them."""
    if isinstance(new, dict) and isinstance(saved, dict):
        extra = set(new) - set(saved)
        return extra <= set(ADDED) and set(saved) <= set(new) and all(same(new[k], saved[k]) for k in saved)
    if isinstance(new, list) and isinstance(saved, list):
        return len(new) == len(saved) and all(same(a, b) for a, b in zip(new, saved))
    return new == saved


def replay():
    """Call every saved tool output again, in the order it was captured."""
    from edgar_mcp import server
    results = {}
    order = ["lookup_company_ticker", "lookup_company_name", "get_financial_statement_is", "get_concept",
             "list_filings_10k", "list_filing_documents", "get_filed_statements", "get_segment_revenue",
             "get_filing_text_page", "get_filing_text_find", "search_filings", "search_companies_sic"]
    for name in order:
        with open(os.path.join(OUTPUTS, name + ".json"), encoding="utf-8") as f:
            saved = json.load(f)
        tool = getattr(server, saved["tool"], None)
        if name in REPLACED or tool is None:
            results[name] = {"skipped": True, "present": tool is not None}
            continue
        try:
            result = asyncio.run(tool(**saved["arguments"]))
        except Exception as e:
            result = {"error": str(e)}
        results[name] = {"same": same(strip(json.loads(json.dumps(result))), strip(saved["result"]))}
    return results


def run(mode, env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    clean["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
    result = subprocess.run([sys.executable, "-X", "utf8", __file__, mode], cwd=HERE, capture_output=True,
                            text=True, env={**clean, **env})
    if result.returncode:
        print(result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def main():
    problems = []

    def check(label, ok, detail=""):
        print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
        if not ok:
            problems.append(label)

    folder = tempfile.mkdtemp(prefix="edgar_frozen_")
    live = run("record", {"EDGAR_RECORD_DIR": folder})
    check("an answer is returned while recording", "ok" in live["submissions"], str(live["submissions"])[:120])
    check("a 404 is reported as not found", live["missing"].get("error") == "SecNotFound")
    check("the 200 and the 404 are both saved", len([f for f in os.listdir(folder) if f.endswith(".json")]) == 2)

    frozen = run("frozen", {"EDGAR_FROZEN_DIR": folder})
    check("the frozen copy gives the same answer, network blocked", frozen["submissions"] == live["submissions"])
    check("a frozen 404 is still not found", frozen["missing"].get("error") == "SecNotFound")
    check("an address with no frozen copy is refused, network never tried",
          frozen["unrecorded"].get("code") == "NO_FROZEN_COPY", str(frozen["unrecorded"]))

    body = next(os.path.join(folder, f) for f in os.listdir(folder)
                if f.endswith(".body") and os.path.getsize(os.path.join(folder, f)) > 0)
    with open(body, "ab") as f:
        f.write(b" ")
    tampered = run("frozen", {"EDGAR_FROZEN_DIR": folder})
    check("a changed frozen copy is refused", tampered["submissions"].get("error") == "SecError")

    # The kept SEC answers.
    abouts = sorted(glob.glob(os.path.join(SEC, "*.json")))
    bad = []
    for path in abouts:
        with open(path, encoding="utf-8") as f:
            about = json.load(f)
        with open(path[:-5] + ".body", "rb") as f:
            data = f.read()
        name = hashlib.sha256(about["url"].encode("utf-8")).hexdigest()
        if (os.path.basename(path) != name + ".json" or hashlib.sha256(data).hexdigest() != about["sha256"]
                or (about["status"] == 404) != (not data)):
            bad.append(about["url"])
    check("every kept SEC answer matches its name and hash", bool(abouts) and not bad,
          f"{len(abouts)} answers {bad[:3]}")

    replayed = run("replay", {"EDGAR_FROZEN_DIR": SEC})
    for name, outcome in replayed.items():
        if outcome.get("skipped"):
            check(f"0.8.0 {name} is replaced in 1.0: {REPLACED.get(name, 'tool absent')}",
                  name in REPLACED, f"tool still present: {outcome['present']}")
        else:
            check(f"0.8.0 {name} replays offline to the saved answer", outcome["same"])

    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        sys.exit(main())
