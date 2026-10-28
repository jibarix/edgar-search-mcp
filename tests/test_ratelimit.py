"""The limiter shared across processes (plan v5, change 2): tests T3 to T13a.

Real subprocesses, configured only through the public environment variables
and the test-only EDGAR_TEST_HOOKS; the SEC is a stand-in transport. Every
case uses its own state file, never the user's. Times are time.monotonic(),
which is system-wide. Run from the repository root:
    python -X utf8 tests/test_ratelimit.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ["EDGAR_RATE_FILE"] = os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "state")
for name in ("EDGAR_FROZEN_DIR", "EDGAR_RECORD_DIR", "EDGAR_RATE_LIMIT", "EDGAR_TEST_HOOKS"):
    os.environ.pop(name, None)

from children import EPS, Child, environment, max_in_window, min_spacing, rate_file, times, urls  # noqa: E402

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
    if not ok:
        problems.append(label)


def info(text):
    print(f"INFO {text}")


def run_together(specs, rate_path, start_in=3.0):
    """Start one child per (spec, hooks, env) at the same monotonic time; their results in order."""
    start = time.monotonic() + start_in
    kids = [Child({**spec, "start_at": start}, hooks, EDGAR_RATE_FILE=rate_path, **env)
            for spec, hooks, env in specs]
    return [k.result() for k in kids]


def crashed(results):
    bad = [r for r in results if r.get("crashed")]
    return f"crashed: {bad[0]['stderr'][-400:]}" if bad else ""


def all_ok(results):
    return all("sha256" in x for r in results for x in r["results"])


def shared_limit(label, results, spacing=0.1):
    handoffs = [t for r in results for t in times(r, "handoff")]
    admits = [t for r in results for t in times(r, "admit")]
    expected = sum(len(r["results"]) for r in results)
    check(f"{label}: every request answered", not crashed(results) and all_ok(results) and len(handoffs) >= expected,
          crashed(results) or f"{len(handoffs)} handoffs for {expected} requests")
    check(f"{label}: at most 10 handoffs in any one-second window", 0 < max_in_window(handoffs) <= 10,
          f"max {max_in_window(handoffs)}")
    check(f"{label}: admissions at least {spacing} s apart across processes",
          bool(admits) and min_spacing(admits) >= spacing - EPS, f"min {min_spacing(admits):.4f}")
    handlers = [t for r in results for t in r["handler"]]
    gaps = []
    for r in results:
        hs, hd = times(r, "handoff"), r["handler"]
        gaps += [b - a for a, b in zip(hs, hd)]
    info(f"{label}: handler entries max {max_in_window(handlers)} per window; "
         f"largest handoff-to-handler gap {max(gaps, default=0) * 1000:.1f} ms (best effort, not asserted)")
    return handoffs, admits


# ── T3, T3b, T3c, T11: one limit across processes ─────────────────────────────

def t3():
    path = rate_file()
    results = run_together([({"urls": urls(15, 100 * i)}, {}, {"EDGAR_RATE_LIMIT": "10"}) for i in range(3)], path)
    shared_limit("T3 three processes at rate 10", results)


def t3b():
    path = rate_file()
    specs = [({"urls": urls(15, 100 * i)}, {"after_admit": [0.2]} if i == 0 else {}, {"EDGAR_RATE_LIMIT": "10"})
             for i in range(3)]
    results = run_together(specs, path)
    shared_limit("T3b one process held 0.2 s after admission", results)
    check("T3b the held process abandons its admission and admits again",
          len(times(results[0], "abandon")) >= 1 and len(times(results[0], "admit")) >= 16,
          f"{len(times(results[0], 'abandon'))} abandoned, {len(times(results[0], 'admit'))} admitted")
    path = rate_file()
    specs = [({"urls": urls(15, 100 * i)}, {"after_admit": 0.01} if i == 0 else {}, {"EDGAR_RATE_LIMIT": "10"})
             for i in range(3)]
    shared_limit("T3b one process held 10 ms after every admission", run_together(specs, path))


def t3c():
    path = rate_file()
    specs = [({"urls": urls(15, 100 * i)}, {"after_check": 0.03} if i == 0 else {}, {"EDGAR_RATE_LIMIT": "10"})
             for i in range(3)]
    results = run_together(specs, path)
    shared_limit("T3c 30 ms after a successful check, before the transport", results)
    info("T3c: handler entries past the check are best effort; 11 in one window would be the documented case")


def t11():
    path = rate_file()
    specs = [({"urls": urls(15, 100 * i)}, {}, {"EDGAR_RATE_LIMIT": "10",
                                                "EDGAR_CACHE_DIR": tempfile.mkdtemp(prefix=f"edgar_cache_t11_{i}_")})
             for i in range(2)]
    shared_limit("T11 two processes with different EDGAR_CACHE_DIR", run_together(specs, path))


# ── T4, T5, T6, T7: retries, pauses and rates ─────────────────────────────────

def t4():
    path = rate_file()
    a = ({"urls": urls(1, 900), "responses": [{"status": 503, "headers": {}}], "retry_delay": 0.001},
         {"before_admit": [0, 0.5]}, {"EDGAR_RATE_LIMIT": "10"})
    b = ({"urls": urls(10, 100)}, {}, {"EDGAR_RATE_LIMIT": "10"})
    ra, rb = run_together([a, b], path)
    admits_a, admits_b = times(ra, "admit"), times(rb, "admit")
    late = admits_a[-1] if len(admits_a) >= 2 else None
    earlier_b = [t for t in admits_b if late is not None and t < late]
    check("T4 the late-waking retry is admitted, answered", late is not None and all_ok([ra, rb]),
          crashed([ra, rb]) or f"{len(admits_a)} admissions")
    check("T4 the late retry comes at least 0.1 s after B's last admission",
          late is not None and (not earlier_b or late - max(earlier_b) >= 0.1 - EPS),
          f"gap {late - max(earlier_b):.4f}" if late and earlier_b else "")
    check("T4 no burst: all admissions at least 0.1 s apart", min_spacing(admits_a + admits_b) >= 0.1 - EPS,
          f"min {min_spacing(admits_a + admits_b):.4f}")


def t5():
    # (i) A 429 with Retry-After 1.5 in A holds B.
    path = rate_file()
    a = ({"urls": urls(1, 900), "responses": [{"status": 429, "headers": {"Retry-After": "1.5"}}],
          "retry_delay": 0.001}, {}, {"EDGAR_RATE_LIMIT": "10"})
    b = ({"urls": urls(12, 100)}, {}, {"EDGAR_RATE_LIMIT": "10"})
    start = time.monotonic() + 3
    kb = Child({**b[0], "start_at": start}, b[1], EDGAR_RATE_FILE=path, **b[2])
    ka = Child({**a[0], "start_at": start + 0.35}, a[1], EDGAR_RATE_FILE=path, **a[2])
    ra, rb = ka.result(), kb.result()
    pauses = times(ra, "pause")
    after = [t for t in times(rb, "handoff") if pauses and t > pauses[0]]
    check("T5 a 429 with Retry-After 1.5 in A is published as a shared pause", len(pauses) == 1,
          crashed([ra, rb]) or str(pauses))
    check("T5 B's handoffs after the pause come 1.5 s or more later",
          bool(after) and min(after) >= pauses[0] + 1.5 - EPS,
          f"first after: +{min(after) - pauses[0]:.3f} s" if after and pauses else "no handoff after the pause")
    check("T5 both processes answered", all_ok([ra, rb]))

    # (ii) A pause published while B is held 0.2 s after admission.
    path = rate_file()
    start = time.monotonic() + 3
    kb = Child({"urls": urls(1, 100), "start_at": start}, {"after_admit": [0.2]}, EDGAR_RATE_FILE=path,
               EDGAR_RATE_LIMIT="10")
    ka = Child({"urls": urls(1, 900), "responses": [{"status": 429, "headers": {"Retry-After": "1.5"}}],
                "retry_delay": 0.001, "start_at": start + 0.05}, {}, EDGAR_RATE_FILE=path, EDGAR_RATE_LIMIT="10")
    ra, rb = ka.result(), kb.result()
    pauses, handoffs_b = times(ra, "pause"), times(rb, "handoff")
    check("T5 B, held past the grace while A's pause is published, abandons and admits again",
          len(times(rb, "abandon")) == 1 and len(times(rb, "admit")) == 2, crashed([ra, rb]) or
          f"{len(times(rb, 'abandon'))} abandoned")
    check("T5 B then waits for the pause", bool(pauses) and bool(handoffs_b)
          and handoffs_b[0] >= pauses[0] + 1.5 - EPS,
          f"+{handoffs_b[0] - pauses[0]:.3f} s" if pauses and handoffs_b else "")

    # (iii) A 5xx without Retry-After in A does not delay B.
    path = rate_file()
    start = time.monotonic() + 3
    kb = Child({"urls": urls(15, 100), "start_at": start}, {}, EDGAR_RATE_FILE=path, EDGAR_RATE_LIMIT="10")
    ka = Child({"urls": urls(1, 900), "responses": [{"status": 503, "headers": {}}, {"status": 503, "headers": {}}],
                "retry_delay": 0.6, "start_at": start + 0.2}, {}, EDGAR_RATE_FILE=path, EDGAR_RATE_LIMIT="10")
    ra, rb = ka.result(), kb.result()
    hb = sorted(times(rb, "handoff"))
    gaps = [y - x for x, y in zip(hb, hb[1:])]
    check("T5 a 503 without Retry-After publishes no shared pause", not times(ra, "pause") and all_ok([ra, rb]),
          crashed([ra, rb]) or str(times(ra, "pause")))
    check("T5 B is not delayed by A's local retry wait", bool(gaps) and max(gaps) < 0.35,
          f"largest gap {max(gaps, default=0):.3f} s")

    # (iv) A Retry-After over MAX_RETRY_WAIT in A: B refuses without sending.
    path = rate_file()
    ra = Child({"urls": urls(1, 900), "responses": [{"status": 429, "headers": {"Retry-After": "120"}}]}, {},
               EDGAR_RATE_FILE=path).result()
    rb = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path).result()
    check("T5 A's 429 asking for 120 s is RATE_LIMITED",
          [x.get("error") for x in ra["results"]] == ["RATE_LIMITED"], crashed([ra]) or str(ra["results"])[:120])
    check("T5 B then refuses with RATE_LIMITED without sending",
          [x.get("error") for x in rb["results"]] == ["RATE_LIMITED"] and rb["handler"] == [],
          crashed([rb]) or f"{rb['results']} handler {rb['handler']}"[:160])


def t6():
    path = rate_file()
    r = Child({"urls": urls(1), "responses": [{"status": 503, "headers": {}}], "retry_delay": 0.001}, {},
              EDGAR_RATE_FILE=path).result()
    check("T6 a retry after a 503 takes a second admission and handoff check",
          len(times(r, "admit")) == 2 and len(times(r, "handoff")) == 2 and len(r["handler"]) == 2 and all_ok([r]),
          crashed([r]) or f"{len(times(r, 'admit'))} admissions, {len(times(r, 'handoff'))} handoffs")


def t7():
    path = rate_file()
    start = time.monotonic() + 3
    ka = Child({"urls": urls(15, 100), "start_at": start}, {}, EDGAR_RATE_FILE=path, EDGAR_RATE_LIMIT="10")
    kb = Child({"urls": urls(4, 900), "start_at": start}, {}, EDGAR_RATE_FILE=path, EDGAR_RATE_LIMIT="2")
    ra, rb = ka.result(), kb.result()
    merged = sorted([(t, "A") for t in times(ra, "admit")] + [(t, "B") for t in times(rb, "admit")])
    after_b = [y[0] - x[0] for x, y in zip(merged, merged[1:]) if x[1] == "B"]
    check("T7 A at 10 and B at 2 both answered", all_ok([ra, rb]) and len(after_b) >= 3, crashed([ra, rb]))
    check("T7 every admission after one of B's is at least 0.5 s later",
          bool(after_b) and min(after_b) >= 0.5 - EPS, f"min {min(after_b, default=0):.4f}")


# ── T8: locking, a killed holder, partial state, the reset ────────────────────

def t8():
    # (a) A holder keeps the lock 6 s.
    path = rate_file()
    holder = Child({"urls": urls(1, 900)}, {"hold_lock": [6]}, EDGAR_RATE_FILE=path)
    held = holder.wait_marker("hold_lock")
    other = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path)
    time.sleep(max(0.0, (held or time.monotonic()) + 3 - time.monotonic()))
    later = Child({"urls": urls(1, 200)}, {}, EDGAR_RATE_FILE=path)
    ro, rh, rl = other.result(), holder.result(), later.result()
    check("T8 the holder reports holding the lock", held is not None, crashed([rh]))
    check("T8 a process that cannot lock within 5 s refuses with RATE_LIMITER_UNAVAILABLE and sends nothing",
          [x.get("error") for x in ro["results"]] == ["RATE_LIMITER_UNAVAILABLE"] and ro["handler"] == [],
          crashed([ro]) or str(ro["results"])[:160])
    released = times(rh, "held_release")
    handoff = times(rl, "handoff")
    check("T8 after release, a waiting process admits within 0.2 s",
          bool(released) and bool(handoff) and 0 <= handoff[0] - released[0] <= 0.2 and all_ok([rl]),
          crashed([rl]) or (f"{(handoff[0] - released[0]) * 1000:.0f} ms" if released and handoff else ""))

    # (b) A holder killed hard while holding the lock.
    path = rate_file()
    Child({"urls": urls(1, 1)}, {}, EDGAR_RATE_FILE=path).result()
    holder = Child({"urls": urls(1, 900)}, {"pause_at": "locked"}, EDGAR_RATE_FILE=path)
    marked = holder.wait_marker("locked")
    killed = holder.kill()
    r = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path).result()
    outcome = r["results"][0] if r["results"] else {}
    got = "sha256" in outcome and len(r["handler"]) == 1
    refused = outcome.get("error") == "RATE_LIMITER_UNAVAILABLE" and r["handler"] == []
    check("T8 the holder was killed while holding the lock", marked is not None)
    check("T8 after a hard kill of the holder: the lock within 5 s, or RATE_LIMITER_UNAVAILABLE with nothing sent",
          got or refused, crashed([r]) or str(outcome)[:120])
    if got and times(r, "admit"):
        info(f"T8 lock reacquired {times(r, 'admit')[0] - killed:.3f} s after the kill (including process start)")

    # (c) A holder killed after writing part of the state.
    path = rate_file()
    Child({"urls": urls(1, 1)}, {}, EDGAR_RATE_FILE=path).result()
    holder = Child({"urls": urls(1, 900)}, {"pause_at": "partial_write"}, EDGAR_RATE_FILE=path)
    marked = holder.wait_marker("partial_write")
    holder.kill()
    r = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path).result()
    outcome = r["results"][0] if r["results"] else {}
    message = outcome.get("message", "")
    check("T8 the holder was killed mid-write", marked is not None)
    check("T8 partial state: RATE_LIMITER_UNAVAILABLE, nothing sent",
          outcome.get("error") == "RATE_LIMITER_UNAVAILABLE" and r["handler"] == [], crashed([r]) or message[:160])
    check("T8 the message names the file and the reset command",
          path in message and 'python -m edgar_mcp.ratelimit --reset --reason' in message, message[:200])

    # (d) The audited reset.
    with open(path, "rb") as f:
        previous = f.read()
    env = environment(EDGAR_RATE_FILE=path)
    refused = subprocess.run([sys.executable, "-X", "utf8", "-m", "edgar_mcp.ratelimit", "--reset"], cwd=ROOT,
                             env=env, capture_output=True, text=True, timeout=60)
    check("T8 a reset without --reason is refused", refused.returncode != 0, refused.stderr[-120:])
    done = subprocess.run([sys.executable, "-X", "utf8", "-m", "edgar_mcp.ratelimit", "--reset", "--reason",
                           "T8 test reset"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    check("T8 --reset --reason succeeds", done.returncode == 0, (done.stdout + done.stderr)[-200:])
    log = os.path.join(os.path.dirname(path), "reset.log")
    entries = []
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
    entry = entries[-1] if entries else {}
    check("T8 reset.log holds the reason, the previous bytes and their sha256",
          len(entries) == 1 and entry.get("reason") == "T8 test reset"
          and base64.b64decode(entry.get("previous_base64", "")) == previous
          and entry.get("previous_sha256") == hashlib.sha256(previous).hexdigest(), str(entry)[:200])
    from edgar_mcp import ratelimit
    with open(path, "rb") as f:
        state = json.loads(f.read().decode("ascii"))
    clock = FakeClock()
    limiter = ratelimit.Limiter(path, 10, clock=clock)
    start = clock.monotonic()
    admitted = limiter.admit()
    check("T8 after the reset the next admission waits 60 s (clock injected)",
          admitted >= state["blocked_until"] - EPS and state["blocked_until"] - start > 59,
          f"waited {admitted - start:.2f} s")
    limiter.close()


# ── T9: clock inconsistency (injected clock) ──────────────────────────────────

class FakeClock:
    """Monotonic and wall time that move only when told; sleep moves both."""

    def __init__(self):
        self.mono0, self.wall0, self.offset, self.jump = time.monotonic(), time.time(), 0.0, 0.0

    def monotonic(self):
        return self.mono0 + self.offset

    def time(self):
        return self.wall0 + self.offset + self.jump

    def sleep(self, seconds):
        self.offset += max(0.0, seconds)

    def at(self, offset):
        self.offset = offset
        return self


def t9():
    from edgar_mcp import ratelimit

    def fresh():
        clock = FakeClock()
        return ratelimit.Limiter(rate_file(), 10, clock=clock), clock

    # (a) An active pause is restarted in full.
    lim, clock = fresh()
    lim.pause(30)
    clock.at(1).jump = 100
    state = lim.inspect()
    check("T9a an active pause is restarted in full on a clock inconsistency",
          abs(state["blocked_until"] - (clock.monotonic() + 30)) < 1e-6, f"{state['blocked_until'] - clock.monotonic():.3f}")
    lim.close()

    # (b) A 30 s pause, then a 2 s pause 1 s later: the 30 s pause stays active.
    lim, clock = fresh()
    lim.pause(30)
    clock.at(1)
    lim.pause(2)
    state = lim.inspect()
    check("T9b a shorter pause does not replace the active one",
          (state.get("pause") or {}).get("seconds") == 30 and abs(state["blocked_until"] - (clock.mono0 + 30)) < 1e-6,
          str(state.get("pause")))
    clock.at(2).jump = -50
    state = lim.inspect()
    check("T9b on inconsistency blocked_until is now + 30 s, not now + 2 s",
          abs(state["blocked_until"] - (clock.monotonic() + 30)) < 1e-6, f"{state['blocked_until'] - clock.monotonic():.3f}")
    lim.close()

    # (c) No active pause: the full recovery barrier.
    lim, clock = fresh()
    lim.admit()
    clock.at(0.5).jump = 3600
    now = clock.monotonic()
    admitted = lim.admit()
    check("T9c no active pause: the next admission waits the full barrier, 1 s + g",
          admitted >= now + 1 + ratelimit.GRACE - EPS, f"+{admitted - now:.4f} s")
    lim.close()

    # (d) A pause cleared under consistent clocks is not restarted.
    lim, clock = fresh()
    lim.pause(2)
    clock.at(3)
    cleared = lim.inspect()
    clock.at(3.5).jump = 100
    now = clock.monotonic()
    state = lim.inspect()
    check("T9d a pause that ran out under consistent clocks is cleared", cleared.get("pause") is None,
          str(cleared.get("pause")))
    check("T9d and is not restarted by a later inconsistency",
          (state["blocked_until"] is None or state["blocked_until"] <= now)
          and abs(state["recovery_until"] - (now + 1 + ratelimit.GRACE)) < 1e-6, str(state)[:160])
    lim.close()

    # (e) A populated log across recovery.
    lim, clock = fresh()
    admits = []
    for k in range(10):
        clock.at(k / 10)
        admits.append(lim.admit())
    clock.at(0.901).jump = 100
    recovery = clock.monotonic()
    nxt = lim.admit()
    admits.append(nxt)
    for _ in range(12):
        admits.append(lim.admit())
    check("T9e populated log: the next admission is at least recovery + 1.025 s",
          nxt >= recovery + 1 + ratelimit.GRACE - EPS, f"+{nxt - recovery:.4f} s")
    check("T9e at most 10 admissions in any window across recovery", max_in_window(admits) <= 10,
          f"max {max_in_window(admits)}")
    lim.close()
    for late in (False, True):
        handoffs_across_recovery(late)
    lim, clock = fresh()
    for k in range(6):
        clock.at(k / 10)
        lim.admit()
    clock.at(0.55)
    lim.pause(5)
    clock.at(0.901).jump = 100
    recovery = clock.monotonic()
    nxt = lim.admit()
    check("T9e with an active pause longer than the barrier, the pause wins", nxt >= recovery + 5 - EPS,
          f"+{nxt - recovery:.4f} s")
    lim.close()

    # (f) Two processes: B honours A's shared recovery deadline.
    path = rate_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stale = {"v": 1, "interval": None, "last_admit": None, "log": [], "blocked_until": None, "recovery_until": None,
             "pause": None, "write_wall": time.time(), "write_mono": time.monotonic() + 1000}
    with open(path, "wb") as f:
        f.write(ratelimit.encode(stale))
    go = os.path.join(os.path.dirname(path), "go")
    a = Child({"urls": urls(1, 900)}, {"pause_at": "after_recovery", "go_file": go}, EDGAR_RATE_FILE=path)
    marked = a.wait_marker("after_recovery")
    with open(path, "rb") as f:
        repaired = json.loads(f.read().decode("ascii"))
    rb = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path).result()
    open(go, "w").close()
    ra = a.result()
    deadline = repaired.get("recovery_until")
    admit_b = times(rb, "admit")
    check("T9f A detects the inconsistency and writes recovery_until",
          marked is not None and deadline is not None and bool(times(ra, "inconsistent")), crashed([ra]))
    check("T9f B sees consistent clocks (no second inconsistency)", not times(rb, "inconsistent"), crashed([rb]))
    check("T9f B does not admit before A's recovery_until",
          bool(admit_b) and deadline is not None and admit_b[0] >= deadline - EPS and all_ok([ra, rb]),
          f"{admit_b[0] - deadline:+.4f} s" if admit_b and deadline else "")


class _Meanwhile:
    """B's limiter: after its first admission `meanwhile` runs (the wall clock
    jumps and A reconciles), then the clock moves to B's final check."""

    def __init__(self, inner, meanwhile, check_at):
        self.inner, self.clock, self.meanwhile, self.check_at = inner, inner.clock, meanwhile, check_at

    def admit(self):
        admitted = self.inner.admit()
        if self.meanwhile:
            self.meanwhile()
            self.meanwhile = None
            self.clock.at(self.check_at)
        return admitted


def handoffs_across_recovery(late):
    """T9e by handoffs. A hands off 9 requests at 0.0 to 0.8; B is admitted at
    0.9; at 0.91 the wall clock jumps and A reconciles, setting the barrier;
    B's final check is at 0.92 (within g, so it hands off before the barrier)
    or at 0.95 (late, so it abandons and is admitted again after the barrier).
    Then A and B alternate 12 requests each."""
    import httpx
    from edgar_mcp import ratelimit
    path, clock = rate_file(), FakeClock()
    lim_a, lim_b = ratelimit.Limiter(path, 10, clock=clock), ratelimit.Limiter(path, 10, clock=clock)
    sent = []

    def handler(request):
        sent.append(clock.monotonic() - clock.mono0)
        return httpx.Response(200, content=b"ok")

    def meanwhile():
        clock.at(0.91).jump = 100
        lim_a.inspect()

    check_at = 0.95 if late else 0.92
    a = ratelimit.RateLimitedTransport(httpx.MockTransport(handler), lim_a)
    b = ratelimit.RateLimitedTransport(httpx.MockTransport(handler), _Meanwhile(lim_b, meanwhile, check_at))
    for k in range(9):
        clock.at(k / 10)
        a.handle_request(httpx.Request("GET", f"https://example.test/a{k}"))
    clock.at(0.9)
    b.handle_request(httpx.Request("GET", "https://example.test/b"))
    for k in range(12):
        a.handle_request(httpx.Request("GET", f"https://example.test/a{k}-after"))
        b.handle_request(httpx.Request("GET", f"https://example.test/b{k}-after"))
    recovery, g = 0.91, ratelimit.GRACE
    old = [t for t in sent if t < recovery + 1]
    new = [t for t in sent if t >= recovery + 1]
    label = f"T9e handoffs, B's final check {'late' if late else 'on time'} at {check_at}"
    check(f"{label}: at most 10 handoffs in any window across recovery", max_in_window(sent) <= 10,
          f"max {max_in_window(sent)} of {len(sent)}")
    if late:
        check(f"{label}: B abandons its admission and hands off at the barrier, recovery + 1 + g",
              len(old) == 9 and bool(new) and abs(new[0] - (recovery + 1 + g)) < 1e-6,
              f"{len(old)} older, first newer {new[0] if new else None}")
    else:
        check(f"{label}: B hands off before the barrier, at its check",
              len(old) == 10 and abs(old[-1] - check_at) < 1e-9, f"{len(old)} older, last {old[-1]:.4f}")
    check(f"{label}: every older handoff is at or before recovery + g, every newer one at or after recovery + 1 + g",
          len(sent) == 34 and all(t <= recovery + g + 1e-9 for t in old)
          and all(t >= recovery + 1 + g - 1e-9 for t in new), f"{len(old)} older, {len(new)} newer")
    lim_a.close()
    lim_b.close()


def t9g():
    """Clock recovery is logged with both clocks' readings, test hooks off."""
    from edgar_mcp import ratelimit
    path = rate_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stale_mono, stale_wall = time.monotonic() + 1000, time.time()
    with open(path, "wb") as f:
        f.write(ratelimit.encode({**ratelimit._fresh(), "write_wall": stale_wall, "write_mono": stale_mono}))
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from edgar_mcp import ratelimit, _testhooks; "
              "assert not _testhooks.ACTIVE; ratelimit.Limiter(sys.argv[2], 8).inspect()")
    run = subprocess.run([sys.executable, "-X", "utf8", "-c", script, ROOT, path], cwd=ROOT,
                         env=environment(EDGAR_RATE_FILE=path), capture_output=True, text=True, timeout=60)
    log = os.path.join(os.path.dirname(path), "clock.log")
    entries = []
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
    entry = entries[-1] if entries else {}
    check("T9g with test hooks off, the recovery warning gives the stored and current readings of both clocks",
          run.returncode == 0 and f"wall {stale_wall:.3f}, monotonic {stale_mono:.3f}" in run.stderr
          and run.stderr.count("monotonic") == 2 and run.stderr.count("wall") == 2, (run.stderr or run.stdout)[-240:])
    check("T9g clock.log beside the state file holds the event with both clocks' readings",
          len(entries) == 1 and entry.get("written_mono") == stale_mono and entry.get("written_wall") == stale_wall
          and isinstance(entry.get("now_mono"), float) and isinstance(entry.get("now_wall"), float), str(entry)[:200])


# ── T10, T12, T13a: unusable state, invalid rate, frozen mode ─────────────────

async def stdio_refusal(label, rate_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    # A dead proxy: a request that got past the limiter would fail without reaching the network.
    env = environment(EDGAR_RATE_FILE=rate_path, HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
                      NO_PROXY="")
    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_submissions", {"cik_or_ticker": "47111"})
            text = result.content[0].text if result.content else ""
            check(f"T10 {label}: RATE_LIMITER_UNAVAILABLE through a stdio call, readable by the client",
                  result.is_error and text.split("get_submissions: ", 1)[-1].startswith("RATE_LIMITER_UNAVAILABLE"),
                  text[:140])


def t10():
    folder = tempfile.mkdtemp(prefix="edgar_rate_test_")
    os.makedirs(os.path.join(folder, "state"))
    asyncio.run(stdio_refusal("a directory at the state path", os.path.join(folder, "state")))
    path = rate_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"not a limiter state" * 10)
    asyncio.run(stdio_refusal("garbage state", path))
    r = Child({"urls": urls(1)}, {}, EDGAR_RATE_FILE=path).result()
    check("T10 garbage state: nothing is sent", r["handler"] == [] and
          [x.get("error") for x in r["results"]] == ["RATE_LIMITER_UNAVAILABLE"], crashed([r]) or str(r["results"])[:120])
    from edgar_mcp import ratelimit

    def refused(label, path):
        r = Child({"urls": urls(1)}, {}, EDGAR_RATE_FILE=path).result()
        check(f"T10 {label}: RATE_LIMITER_UNAVAILABLE, nothing sent", r["handler"] == [] and
              [x.get("error") for x in r["results"]] == ["RATE_LIMITER_UNAVAILABLE"],
              crashed([r]) or str(r["results"])[:120])

    # An existing empty file is not a fresh state.
    path = rate_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    refused("an existing empty state file", path)
    check("T10 the empty file is left as it was", os.path.getsize(path) == 0, str(os.path.getsize(path)))

    # A whole state with one field changed and the old checksum kept.
    path = rate_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = json.loads(ratelimit.encode(ratelimit._fresh()).decode("ascii"))
    state["interval"] = 0.5
    with open(path, "wb") as f:
        f.write(json.dumps(state).encode("ascii").ljust(ratelimit.STATE_SIZE, b" "))
    refused("a checksum mismatch", path)

    # A missing file is created whole, also when several processes start on it at once.
    path = rate_file()
    results = run_together([({"urls": urls(1, 10 * k)}, {}, {}) for k in range(4)], path)
    leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".tmp-")]
    check("T10 a missing state file is created at its full size, by four processes at once",
          all_ok(results) and os.path.getsize(path) == ratelimit.STATE_SIZE and not leftovers,
          crashed(results) or f"{os.path.getsize(path)} bytes, temp files {leftovers}")

    # A reset killed while writing the new state.
    path = rate_file()
    Child({"urls": urls(1, 1)}, {}, EDGAR_RATE_FILE=path).result()
    env = environment(EDGAR_RATE_FILE=path, EDGAR_TEST_HOOKS=json.dumps({"pause_at": "partial_write"}))
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "edgar_mcp.ratelimit", "--reset", "--reason",
                             "T10 interrupted reset"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    marked, deadline = False, time.monotonic() + 30
    while not marked and time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        marked = line.decode("utf-8", "replace").strip() == "PAUSED partial_write"
    proc.kill()
    proc.wait()
    proc.stderr.close()
    check("T10 the reset was killed while writing the new state", marked)
    check("T10 an interrupted reset leaves a full-size file, never an empty one",
          os.path.getsize(path) == ratelimit.STATE_SIZE, str(os.path.getsize(path)))
    refused("after an interrupted reset", path)


def t10b():
    """A folder that cannot hold hard links: a missing state file is never created (os.link forced to fail)."""
    import errno
    import httpx
    from edgar_mcp import ratelimit
    from edgar_mcp.utils.errors import SecLimiterUnavailable
    path = rate_file()
    sent, real_link = [], ratelimit.os.link

    def no_link(src, dst):
        raise OSError(errno.EPERM, "hard links are not supported here (simulated)")

    def handler(request):
        sent.append(request.url)
        return httpx.Response(200, content=b"ok")

    limiter = ratelimit.Limiter(path, 8)
    transport = ratelimit.RateLimitedTransport(httpx.MockTransport(handler), limiter)
    ratelimit.os.link = no_link
    try:
        transport.handle_request(httpx.Request("GET", urls(1)[0]))
        outcome = "sent"
    except SecLimiterUnavailable as e:
        outcome = f"{e.code}: {e}"
    finally:
        ratelimit.os.link = real_link
        limiter.close()
    folder = os.path.dirname(path)
    left = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    check("T10b os.link failing on a missing state file: RATE_LIMITER_UNAVAILABLE, nothing sent",
          outcome.startswith("RATE_LIMITER_UNAVAILABLE") and "cannot be opened" in outcome and sent == [], outcome[:200])
    check("T10b no state file and no temp file is left", not os.path.exists(path) and left == [], str(left))


def t14b():
    """A 403 without Retry-After publishes the fallback pause to every process."""
    from edgar_mcp import ratelimit
    from edgar_mcp.utils import sec_client
    path = rate_file()
    ra = Child({"urls": urls(1, 1), "responses": [{"status": 403, "body": "", "headers": {}}]}, {},
               EDGAR_RATE_FILE=path).result()
    with open(path, "rb") as f:
        state = json.loads(f.read().decode("ascii"))
    remaining = (state.get("blocked_until") or 0) - time.monotonic()
    check("T14b a headerless 403 answers RATE_LIMITED after one request",
          [x.get("error") for x in ra["results"]] == ["RATE_LIMITED"] and len(ra["handler"]) == 1,
          crashed([ra]) or str(ra["results"])[:140])
    check(f"T14b the published pause is the {sec_client.FORBIDDEN_PAUSE} s fallback",
          (state.get("pause") or {}).get("seconds") == sec_client.FORBIDDEN_PAUSE
          and sec_client.FORBIDDEN_PAUSE - 30 < remaining <= sec_client.FORBIDDEN_PAUSE, f"{remaining:.1f} s left")
    rb = Child({"urls": urls(1, 100)}, {}, EDGAR_RATE_FILE=path).result()
    check(f"T14b another process refuses with RATE_LIMITED and sends nothing while more than "
          f"{ratelimit.MAX_RETRY_WAIT} s remain",
          [x.get("error") for x in rb["results"]] == ["RATE_LIMITED"] and rb["handler"] == [],
          crashed([rb]) or str(rb["results"])[:140])


def t12():
    for value in ("0", "11", "abc"):
        env = environment(EDGAR_RATE_LIMIT=value, EDGAR_RATE_FILE=rate_file())
        try:
            run = subprocess.run([sys.executable, "-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            code, err = run.returncode, run.stderr
        except subprocess.TimeoutExpired:
            code, err = None, "still running"
        check(f"T12 EDGAR_RATE_LIMIT={value!r} stops the server at startup with a message",
              code not in (0, None) and "EDGAR_RATE_LIMIT" in err, f"exit {code}: {err[-120:]!r}")
        r = Child({"tool": "get_submissions", "arguments": {"cik_or_ticker": "47111"}}, {},
                  EDGAR_RATE_LIMIT=value, EDGAR_RATE_FILE=rate_file()).result()
        check(f"T12 EDGAR_RATE_LIMIT={value!r}: a tool call in-process refuses with RATE_LIMITER_UNAVAILABLE",
              [x.get("error") for x in r["results"]] == ["RATE_LIMITER_UNAVAILABLE"] and r["handler"] == [],
              crashed([r]) or str(r["results"])[:140])
    env = environment(EDGAR_TEST_HOOKS="{}", EDGAR_RATE_FILE=rate_file())
    run = subprocess.run([sys.executable, "-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    check("the server refuses to start with the test-only EDGAR_TEST_HOOKS set",
          run.returncode != 0 and "EDGAR_TEST_HOOKS" in run.stderr, f"exit {run.returncode}: {run.stderr[-120:]!r}")


def t13a():
    path = os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "never", "state")
    r = Child({"tool": "get_submissions", "arguments": {"cik_or_ticker": "908315"}}, {},
              EDGAR_FROZEN_DIR=os.path.join(ROOT, "tests", "fixtures", "sec"), EDGAR_RATE_FILE=path).result()
    check("T13a frozen mode answers", [x.get("ok") for x in r["results"]] == [True], crashed([r]) or str(r["results"])[:120])
    check("T13a frozen mode creates no state file and takes no lock",
          not os.path.exists(path) and not os.path.exists(os.path.dirname(path)), path)


CASES = [("T3", t3), ("T3b", t3b), ("T3c", t3c), ("T4", t4), ("T5", t5), ("T6", t6), ("T7", t7), ("T8", t8),
         ("T9", t9), ("T9g", t9g), ("T10", t10), ("T10b", t10b), ("T14b", t14b), ("T11", t11), ("T12", t12), ("T13a", t13a)]

if __name__ == "__main__":
    chosen = set(sys.argv[1:])
    for name, case in CASES:
        if chosen and name not in chosen:
            continue
        try:
            case()
        except Exception as e:  # a case that cannot run is a failure, and the rest still run
            check(f"{name} ran to the end", False, f"{type(e).__name__}: {e}"[:200])
    print(f"problems: {len(problems)}")
    sys.exit(1 if problems else 0)
