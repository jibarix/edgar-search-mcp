"""Recordings published once and never replaced (plan v5, change 3): tests T14 to T18.

In-process cases use a stand-in transport; the races and hard terminations use
real subprocesses (tests/limiter_child.py) paused by the test-only hooks. Run
from the repository root:
    python -X utf8 tests/test_recorder.py
"""
from __future__ import annotations

import asyncio
import errno
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
os.environ["EDGAR_RATE_FILE"] = os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "state")
for name in ("EDGAR_FROZEN_DIR", "EDGAR_RECORD_DIR", "EDGAR_RATE_LIMIT", "EDGAR_TEST_HOOKS"):
    os.environ.pop(name, None)

import httpx  # noqa: E402

from children import Child  # noqa: E402
from edgar_mcp import server  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402

problems = []
URL = "https://data.sec.gov/submissions/CIK0000047111.json"
SEC = os.path.join(ROOT, "tests", "fixtures", "sec")


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def description(folder, url=URL):
    path = os.path.join(folder, url_hash(url) + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def stat(path):
    s = os.stat(path)
    with open(path, "rb") as f:
        return (f.read(), s.st_mtime_ns, s.st_ino)


def bodies(folder, url=URL):
    return sorted(glob.glob(os.path.join(folder, url_hash(url) + ".*.body")))


sec_client.USER_AGENT = "test test@example.com"
calls = []


def answer(status=200, body=b'{"a": 1}', headers=None):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, content=body, headers=headers or {"Content-Type": "application/json"})
    sec_client._client = httpx.Client(transport=httpx.MockTransport(handler))


def recording(folder):
    class Recording:
        def __enter__(self):
            sec_client.RECORD_DIR = folder

        def __exit__(self, *exc):
            sec_client.RECORD_DIR = None
    return Recording()


def fetch(url=URL, **kw):
    try:
        return sec_client.fetch_response(url, **kw), None
    except sec_client.SecError as e:
        return None, e


# ── T14: live, then replayed ──────────────────────────────────────────────────

def t14():
    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    answer()
    calls.clear()
    with recording(folder):
        live, e1 = fetch()
        replay, e2 = fetch()
    check("T14 the first fetch is live and recorded", live is not None and not live.frozen
          and description(folder) is not None and len(calls) == 1, str(e1))
    check("T14 the second is replayed: frozen, same sha256, original retrieved_at, no request",
          replay is not None and replay.frozen and replay.body == live.body and replay.retrieved_at == live.retrieved_at
          and len(calls) == 1, str(e2) if e2 else f"{len(calls)} requests")
    about = description(folder) or {}
    check("T14 the description names its body by hash",
          about.get("body") == f"{url_hash(URL)}.{hashlib.sha256(live.body).hexdigest()}.body" if live else False,
          str(about)[:160])

    # A paged file: pages after the first come from the recording.
    doc = "https://www.sec.gov/Archives/edgar/data/47111/000162828026008586/hsy-20251231.htm"
    body = bytes(range(256)) * 10
    answer(body=body, headers={"Content-Type": "text/html"})
    calls.clear()
    with recording(folder):
        first = asyncio.run(server.get_filing_document(url=doc, offset=0, max_bytes=1000))
        server._archive_cache.clear()
        second = asyncio.run(server.get_filing_document(url=doc, offset=1000, max_bytes=1000))
    check("T14 a later page in record mode comes from the recording, same sha256",
          len(calls) == 1 and first["sha256"] == second["sha256"] == hashlib.sha256(body).hexdigest()
          and second["sources"][0]["frozen"] is True, f"{len(calls)} requests")


# ── T15: two processes capture one address at once ────────────────────────────

def child(folder, hooks, body, start_at=None):
    spec = {"urls": [URL], "plain": True, "default": {"status": 200, "body": body}}
    if start_at:
        spec["start_at"] = start_at
    return Child(spec, hooks, EDGAR_RECORD_DIR=folder)


def t15():
    # (a) Identical bodies, both paused before the description link, then released together.
    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    go = os.path.join(tempfile.mkdtemp(), "go")
    start = time.monotonic() + 3
    x = child(folder, {"pause_at": "after_desc_temp", "go_file": go}, "same body", start)
    y = child(folder, {"pause_at": "after_desc_temp", "go_file": go}, "same body", start + 1.5)
    marked = x.wait_marker("after_desc_temp") and y.wait_marker("after_desc_temp")
    open(go, "w").close()
    rx, ry = x.result(), y.result()
    about = description(folder) or {}
    answers = [r["results"][0] if r["results"] else {} for r in (rx, ry)]
    check("T15a both captured live and paused before publishing", bool(marked), str(rx.get("stderr", ""))[-200:])
    check("T15a one description; both answers carry the winner's metadata",
          len(glob.glob(os.path.join(folder, "*.json"))) == 1
          and all(a.get("retrieved_at") == about.get("retrieved_at") and a.get("sha256") == about.get("sha256")
                  for a in answers), str(answers)[:200])
    check("T15a the winner answers frozen false (fetched live), the loser frozen true (the recording)",
          sorted(a.get("frozen") for a in answers) == [False, True], str([a.get("frozen") for a in answers]))

    # (b) Identical bodies, forced order: the delayed writer reaches its body link after the winner published.
    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    go = os.path.join(tempfile.mkdtemp(), "go")
    x = child(folder, {"pause_at": "before_body_link", "go_file": go}, "same body")
    marked = x.wait_marker("before_body_link")
    time.sleep(1.1)
    ry = child(folder, {}, "same body").result()
    about = description(folder) or {}
    winner = os.path.join(folder, about.get("body", "missing"))
    before = stat(winner) if os.path.exists(winner) else None
    open(go, "w").close()
    rx = x.result()
    after = stat(winner) if os.path.exists(winner) else None
    ax = rx["results"][0] if rx["results"] else {}
    check("T15b the delayed writer was paused before its body link", marked is not None)
    check("T15b the winner's body is untouched: same bytes, mtime and file id", before is not None and before == after)
    check("T15b the delayed writer answers with the winner's metadata",
          ax.get("sha256") == about.get("sha256") and ax.get("retrieved_at") == about.get("retrieved_at")
          and ry["results"][0].get("retrieved_at") == about.get("retrieved_at"), str(ax)[:160])

    # (c) Different bodies: the loser refuses and nothing of the winner's changes.
    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    go_x, go_y = os.path.join(tempfile.mkdtemp(), "go"), os.path.join(tempfile.mkdtemp(), "go")
    x = child(folder, {"pause_at": "after_desc_temp", "go_file": go_x}, "body A")
    y = child(folder, {"pause_at": "after_desc_temp", "go_file": go_y}, "body B")
    marked = x.wait_marker("after_desc_temp") and y.wait_marker("after_desc_temp")
    open(go_x, "w").close()
    rx = x.result()
    about_path = os.path.join(folder, url_hash(URL) + ".json")
    about = description(folder) or {}
    winner = os.path.join(folder, about.get("body", "missing"))
    before = (stat(about_path), stat(winner)) if about and os.path.exists(winner) else None
    open(go_y, "w").close()
    ry = y.result()
    after = (stat(about_path), stat(winner)) if about and os.path.exists(winner) else None
    ay = ry["results"][0] if ry["results"] else {}
    check("T15c both captured different bodies and paused", bool(marked) and "sha256" in (rx["results"] or [{}])[0])
    check("T15c the loser raises UPSTREAM_UNAVAILABLE: two captures differ",
          ay.get("error") == "UPSTREAM_UNAVAILABLE" and "differ" in ay.get("message", ""), str(ay)[:160])
    check("T15c both bodies stay on disk", len(bodies(folder)) == 2, str(bodies(folder)))
    check("T15c the winner's description and body are unchanged (bytes, mtime, file id)",
          before is not None and before == after)


# ── T16: hard termination at each boundary ────────────────────────────────────

class Poller(threading.Thread):
    """Reads the description over and over; every read must be a whole description."""

    def __init__(self, folder):
        super().__init__(daemon=True)
        self.path = os.path.join(folder, url_hash(URL) + ".json")
        self.stop, self.reads, self.partial = threading.Event(), 0, []

    def run(self):
        while not self.stop.is_set():
            try:
                with open(self.path, "rb") as f:
                    data = f.read()
            except OSError:
                time.sleep(0.0005)
                continue
            self.reads += 1
            try:
                about = json.loads(data)
                if not {"url", "status", "sha256", "retrieved_at", "body"} <= set(about):
                    self.partial.append(data[:80])
            except ValueError:
                self.partial.append(data[:80])


def complete(folder):
    about = description(folder)
    if about is None:
        return False
    path = os.path.join(folder, about["body"])
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest() == about["sha256"]


def t16():
    for point in ("after_body_temp", "after_body_link", "after_desc_temp", "after_desc_link"):
        folder = tempfile.mkdtemp(prefix="edgar_record_test_")
        poller = Poller(folder)
        poller.start()
        x = child(folder, {"pause_at": point}, "killed body")
        marked = x.wait_marker(point)
        x.kill()
        names = os.listdir(folder)
        whole = complete(folder)
        harmless = all(n.endswith(".body") or n.startswith(".tmp-") for n in names)
        check(f"T16 killed at {point}: a complete recording or only harmless orphans and temps",
              marked is not None and (whole or harmless), str(names))
        r = child(folder, {}, "killed body").result()
        poller.stop.set()
        poller.join()
        res = r["results"][0] if r["results"] else {}
        expect_replay = point == "after_desc_link"
        check(f"T16 after a kill at {point} the next capture {'replays' if expect_replay else 'succeeds'}",
              res.get("sha256") == hashlib.sha256(b"killed body").hexdigest() and res.get("frozen") is expect_replay
              and complete(folder), str(res)[:160])
        check(f"T16 a reader polling during publication at {point} never reads a partial description",
              not poller.partial, f"{poller.reads} reads, partial {poller.partial[:2]}")


# ── T17: what is recorded, and refusals ───────────────────────────────────────

def t17():
    real_delay = sec_client.API_RETRY_DELAY
    sec_client.API_RETRY_DELAY = 0.001
    try:
        for status, headers in ((429, {"Retry-After": "0"}), (403, {}), (503, {})):
            folder = tempfile.mkdtemp(prefix="edgar_record_test_")
            answer(status=status, body=b"", headers=headers)
            with recording(folder):
                _, e = fetch()
            check(f"T17 an HTTP {status} is never recorded", e is not None and os.listdir(folder) == [],
                  f"{e.code if e else None} {os.listdir(folder)}")
    finally:
        sec_client.API_RETRY_DELAY = real_delay

    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    answer(status=404, body=b"")
    calls.clear()
    with recording(folder):
        _, e1 = fetch()
        _, e2 = fetch()
    check("T17 a 404 is recorded and replayed as NOT_FOUND",
          e1 is not None and e1.code == "NOT_FOUND" and e2 is not None and e2.code == "NOT_FOUND"
          and len(calls) == 1 and (description(folder) or {}).get("status") == 404, f"{len(calls)} requests")

    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    answer()
    real_link = os.link

    def no_link(*a, **k):
        raise OSError(errno.EPERM, "hard links not supported")
    os.link = no_link
    try:
        with recording(folder):
            _, e = fetch()
    finally:
        os.link = real_link
    check("T17 a volume without hard links is refused: UPSTREAM_UNAVAILABLE",
          e is not None and e.code == "UPSTREAM_UNAVAILABLE" and "atomically" in str(e), str(e)[:140])
    check("T17 and nothing is published", description(folder) is None and not bodies(folder), str(os.listdir(folder)))

    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    answer()
    real_remove, real_unlink = os.remove, os.unlink

    def stuck(path, *a, **k):
        if os.path.basename(str(path)).startswith(".tmp-"):
            raise PermissionError(errno.EACCES, "held by another program", str(path))
        return real_remove(path, *a, **k)
    os.remove = os.unlink = stuck
    try:
        with recording(folder):
            r, e = fetch()
    finally:
        os.remove, os.unlink = real_remove, real_unlink
    check("T17 a temp that cannot be removed is still a success", r is not None and complete(folder), str(e))

    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    body = b'{"a": 1}'
    with open(os.path.join(folder, f"{url_hash(URL)}.{hashlib.sha256(body).hexdigest()}.body"), "wb") as f:
        f.write(b"something else")
    answer(body=body)
    with recording(folder):
        _, e = fetch()
    check("T17 an existing body whose hash does not match its name: UPSTREAM_UNAVAILABLE",
          e is not None and e.code == "UPSTREAM_UNAVAILABLE", str(e)[:140])


# ── T18: the old layout still replays ─────────────────────────────────────────

def t18():
    old = "https://data.sec.gov/submissions/CIK0000908315.json"
    check("T18 the fixtures are in the old layout", os.path.exists(os.path.join(SEC, url_hash(old) + ".body")))
    sec_client.FROZEN_DIR = SEC
    try:
        r, e = fetch(old)
    finally:
        sec_client.FROZEN_DIR = None
    check("T18 an old-layout frozen copy replays", r is not None and r.frozen, str(e))
    folder = tempfile.mkdtemp(prefix="edgar_record_test_")
    for suffix in (".body", ".json"):
        shutil.copy(os.path.join(SEC, url_hash(old) + suffix), folder)
    answer()
    calls.clear()
    with recording(folder):
        r, e = fetch(old)
    check("T18 an old-layout recording replays in record mode without a request",
          r is not None and r.frozen and not calls, str(e))


CASES = [("T14", t14), ("T15", t15), ("T16", t16), ("T17", t17), ("T18", t18)]

if __name__ == "__main__":
    chosen = set(sys.argv[1:])
    for name, case in CASES:
        if chosen and name not in chosen:
            continue
        try:
            case()
        except Exception as e:
            check(f"{name} ran to the end", False, f"{type(e).__name__}: {e}"[:200])
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
