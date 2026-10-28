"""T21 of plan v5: one live compatibility check against the SEC, then its replay.

    python -X utf8 tests/live_check.py record <new folder> --compare <filing folder>
    python -X utf8 tests/live_check.py replay <folder>

`record` refuses a folder that exists. It writes the URL inventory first,
then starts the working-tree server over stdio (tests/live_server.py) with
EDGAR_RECORD_DIR=<folder>/recording, a fresh cache, its own limiter state
file and EDGAR_RATE_LIMIT=4, and reads every inventory URL through
get_filing_document, all pages. EDGAR_USER_AGENT must be set. The run stops
at the first error answer, at the request cap (counted at the handoff), or
when the submissions document lists more than 3 older files, before any
filing file. Then it replays the recording with the network blocked.

`replay` repeats only the replay, against answers.json from the record run;
it is what is repeated against a release artefact.

Checks: each answer's sha256 equals the hash of its reassembled bytes; the
eight filing files are byte-identical to the --compare folder; the replay
gives the same bytes, sha256 and retrieved_at for every URL. Everything is
written to the folder: inventory.json, handoffs_*.log, answers.json,
manifest.json (every recording file with its sha256) and run.json.
Compatibility only, never evidence of live reliability. Not part of run_all.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CIK = "0000047111"
FOLDER = "https://www.sec.gov/Archives/edgar/data/47111/000162828026008586/"
FILING_FILES = ["FilingSummary.xml", "hsy-20251231.htm", "hsy-20251231.xsd", "hsy-20251231_cal.xml",
                "hsy-20251231_def.xml", "hsy-20251231_htm.xml", "hsy-20251231_lab.xml", "hsy-20251231_pre.xml"]
MAX_OLDER = 3
MAX_URLS = 3 + MAX_OLDER + len(FILING_FILES)  # 14
ATTEMPTS = 4  # API_RETRY_COUNT + 1
CAP = MAX_URLS * ATTEMPTS  # 56
RATE = "4"
PAGE = 1_000_000


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with open(path, "x", encoding="utf-8") as f:  # never overwrites
        json.dump(value, f, indent=1)


class Stop(Exception):
    """The run stops here, for the reason given."""


def server(folder, mode, extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    if mode == "record":
        env["EDGAR_USER_AGENT"] = os.environ["EDGAR_USER_AGENT"]
    env.update(EDGAR_CACHE_DIR=os.path.join(folder, f"cache_{mode}"),
               EDGAR_RATE_FILE=os.path.join(folder, f"rate_{mode}", "state"),
               EDGAR_RATE_LIMIT=RATE, **extra)
    os.makedirs(os.path.join(folder, f"rate_{mode}"), exist_ok=True)
    cap = str(CAP) if mode == "record" else "0"
    log = os.path.join(folder, f"handoffs_{mode}.log")
    return StdioServerParameters(command=sys.executable,
                                 args=["-X", "utf8", os.path.join(ROOT, "tests", "live_server.py"), log, cap],
                                 cwd=ROOT, env=env), log


async def read_all(session, url):
    """Every page of `url`: (bytes, the first page's answer, pages)."""
    chunks, offset, first, pages = [], 0, None, 0
    while offset is not None:
        result = await session.call_tool("get_filing_document", {"url": url, "offset": offset, "max_bytes": PAGE})
        if result.is_error:
            raise Stop(f"error answer for {url}: {result.content[0].text if result.content else ''}")
        answer = result.structured_content
        if answer is None or json.loads(result.content[0].text) != answer:
            raise Stop(f"{url}: no structured answer, or its text differs")
        first = first or answer
        if answer["sha256"] != first["sha256"]:
            raise Stop(f"{url}: the file changed between pages")
        chunks.append(base64.b64decode(answer["data_base64"]))
        offset, pages = answer["next_offset"], pages + 1
    return b"".join(chunks), first, pages


def entry(url, body, first, pages):
    return {"url": url, "sha256": first["sha256"], "total_bytes": first["total_bytes"],
            "bytes_sha256": sha256(body), "bytes": len(body), "pages": pages,
            "content_type": first["content_type"],
            "sources": [{k: s.get(k) for k in ("url", "retrieved_at", "sha256", "frozen", "cache_hit")}
                        for s in first["sources"]]}


async def run(params, urls_of, check, answers, bodies):
    """Reads each URL that `urls_of` yields (each body is sent back to it) into
    `answers` ({url: entry}) and `bodies` ({url: bytes})."""
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            gen = urls_of()
            url = next(gen, None)
            while url is not None:
                body, first, pages = await read_all(session, url)
                answers[url], bodies[url] = entry(url, body, first, pages), body
                check(f"{url}: sha256 is the hash of the reassembled bytes",
                      first["sha256"] == sha256(body) and first["total_bytes"] == len(body))
                try:
                    url = gen.send(body)
                except StopIteration:
                    url = None


def record_urls(found):
    """The inventory, in order; the older submissions files come from the submissions document."""
    yield "https://www.sec.gov/files/company_tickers_exchange.json"
    submissions = yield f"https://data.sec.gov/submissions/CIK{CIK}.json"
    older = [f["name"] for f in json.loads(submissions)["filings"].get("files", [])]
    found.extend(older)
    if len(older) > MAX_OLDER:
        raise Stop(f"the submissions document lists {len(older)} older files, more than {MAX_OLDER}")
    for name in older:
        yield f"https://data.sec.gov/submissions/{name}"
    yield FOLDER + "index.json"
    for name in FILING_FILES:
        yield FOLDER + name


def handoffs(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [line.rstrip("\n").split("\t") for line in f]


def manifest(folder):
    rec = os.path.join(folder, "recording")
    files = {n: {"sha256": sha256(open(os.path.join(rec, n), "rb").read()),
                 "bytes": os.path.getsize(os.path.join(rec, n))} for n in sorted(os.listdir(rec))}
    problems = []
    for name in (n for n in files if n.endswith(".json")):
        with open(os.path.join(rec, name), encoding="utf-8") as f:
            about = json.load(f)
        body = about.get("body")
        if body not in files or files[body]["sha256"] != about["sha256"]:
            problems.append(f"{name}: its body {body} is missing or does not hash to {about['sha256']}")
    return files, problems


async def replay(folder, recorded_answers, recorded_bodies, check):
    params, log = server(folder, "replay", {"EDGAR_FROZEN_DIR": os.path.join(folder, "recording")})
    urls = list(recorded_answers)

    def urls_of():
        for url in urls:
            yield url
    answers, bodies = {}, {}
    await run(params, urls_of, check, answers, bodies)
    for url in urls:
        a, b = recorded_answers[url], answers.get(url)
        same_bytes = (bodies.get(url) == recorded_bodies[url]) if recorded_bodies else \
            (b is not None and b["bytes_sha256"] == a["bytes_sha256"])
        check(f"replay {url}: same bytes, sha256 and retrieved_at",
              b is not None and same_bytes and b["sha256"] == a["sha256"]
              and [s["retrieved_at"] for s in b["sources"]] == [s["retrieved_at"] for s in a["sources"]]
              and all(s["frozen"] for s in b["sources"]))
    sent = handoffs(log)
    check("the replay sent no request", not sent, str(len(sent)))
    return answers


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["record", "replay"])
    parser.add_argument("folder")
    parser.add_argument("--compare", help="the local folder of the filing, for the byte comparison")
    args = parser.parse_args()
    folder = os.path.abspath(args.folder)
    problems, lines = [], []

    def check(label, ok, detail=""):
        line = f"{'OK  ' if ok else 'FAIL'} {label} {detail}".rstrip()
        print(line)
        lines.append(line)
        if not ok:
            problems.append(label)

    if args.mode == "replay":
        with open(os.path.join(folder, "answers.json"), encoding="utf-8") as f:
            recorded = json.load(f)
        started = now()
        stop = None
        try:
            await replay(folder, recorded, None, check)
        except Stop as e:
            stop = str(e)
            check("the replay ran to the end", False, stop)
        name = f"replay_{started.replace(':', '')}.json"
        write_json(os.path.join(folder, name), {"started": started, "ended": now(), "stop": stop,
                                                 "python": sys.executable, "checks": lines,
                                                 "problems": problems})
        print(f"problems: {len(problems)}")
        return 1 if problems else 0

    if os.path.exists(folder):
        sys.exit(f"{folder} exists; T21 records into a fresh folder")
    if not os.environ.get("EDGAR_USER_AGENT"):
        sys.exit("set EDGAR_USER_AGENT")
    if not args.compare:
        sys.exit("--compare is required for record")
    os.makedirs(os.path.join(folder, "recording"))
    write_json(os.path.join(folder, "inventory.json"), {
        "written_at": now(), "plan": "notes/2026-10-28_plan_edgar_mcp_fixes_v5.md, T21",
        "urls": ["https://www.sec.gov/files/company_tickers_exchange.json",
                 f"https://data.sec.gov/submissions/CIK{CIK}.json",
                 f"each older submissions file that CIK{CIK}.json lists in filings.files, at most {MAX_OLDER}; "
                 "more stops the run before any filing file",
                 FOLDER + "index.json"] + [FOLDER + n for n in FILING_FILES],
        "max_urls": MAX_URLS, "request_cap": CAP, "rate_limit": float(RATE)})

    started, stop, older = now(), None, []
    params, log = server(folder, "record", {"EDGAR_RECORD_DIR": os.path.join(folder, "recording")})
    answers, bodies = {}, {}
    try:
        await run(params, lambda: record_urls(older), check, answers, bodies)
    except Stop as e:
        stop = str(e)
        check("the record run ran to the end", False, stop)
    sent = handoffs(log)
    check(f"requests handed to the network within the cap of {CAP}", len(sent) <= CAP, str(len(sent)))
    check(f"distinct URLs within {MAX_URLS}", len(answers) <= MAX_URLS, str(len(answers)))
    for url in answers:
        check(f"{url}: the record run fetched it live once", answers[url]["sources"][0]["frozen"] is False
              and sum(1 for _, u in sent if u == url) >= 1)
    for name in FILING_FILES:
        url = FOLDER + name
        if url in bodies:
            with open(os.path.join(args.compare, name), "rb") as f:
                local = f.read()
            check(f"{name} is byte-identical to {args.compare}", bodies[url] == local,
                  f"{sha256(bodies[url])[:12]} vs {sha256(local)[:12]}")
        else:
            check(f"{name} was read", False)
    write_json(os.path.join(folder, "answers.json"), answers)
    files, broken = manifest(folder)
    check("every recorded description names a body with its hash", not broken, "; ".join(broken))
    write_json(os.path.join(folder, "manifest.json"), files)

    replayed_at = now()
    replay_stop = None
    if stop is None:
        try:
            await replay(folder, answers, bodies, check)
        except Stop as e:
            replay_stop = str(e)
            check("the replay ran to the end", False, replay_stop)
    write_json(os.path.join(folder, "run.json"), {
        "started": started, "replay_started": replayed_at, "ended": now(), "stop": stop,
        "replay_stop": replay_stop, "older_submissions_files": older, "requests_sent": len(sent),
        "request_cap": CAP, "rate_limit": float(RATE), "python": sys.executable,
        "checks": lines, "problems": problems})
    print(f"requests sent: {len(sent)}; problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
