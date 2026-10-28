"""Test-only hooks: delays, holds and pause points the multi-process tests
use to place one process at an exact step of the limiter or the recorder.

Active only when EDGAR_TEST_HOOKS is set (to a JSON object); the server
refuses to start with it set. Inactive, every function returns at once and
nothing is recorded.

    {"after_admit": 0.01}          delay(name): a float sleeps every call,
    {"after_admit": [0.2]}         a list sleeps its values in turn, then 0
    {"hold_lock": [6]}             hold(name): the same, printing "PAUSED <name>"
    {"pause_at": "locked",         pause(point): at that point print
     "go_file": "<path>"}          "PAUSED <point>" and wait for go_file
                                   (forever without one); fires once
"""
import json
import os
import sys
import time

ACTIVE = "EDGAR_TEST_HOOKS" in os.environ
HOOKS = json.loads(os.environ["EDGAR_TEST_HOOKS"] or "{}") if ACTIVE else {}
EVENTS = []
_fired = set()


def event(kind, t, *extra):
    """Record that `kind` happened at time.monotonic() value `t`."""
    if ACTIVE:
        EVENTS.append([kind, t, *extra])


def _next(name):
    value = HOOKS.get(name)
    if isinstance(value, list):
        return float(value.pop(0)) if value else 0.0
    return float(value or 0.0)


def delay(name):
    if ACTIVE:
        seconds = _next(name)
        if seconds > 0:
            time.sleep(seconds)


def hold(name):
    """Sleep as `delay` does, announcing it; the seconds slept."""
    if not ACTIVE:
        return 0.0
    seconds = _next(name)
    if seconds > 0:
        print(f"PAUSED {name}", file=sys.stderr, flush=True)
        time.sleep(seconds)
    return seconds


def pause(point):
    if not ACTIVE or HOOKS.get("pause_at") != point or point in _fired:
        return
    _fired.add(point)
    print(f"PAUSED {point}", file=sys.stderr, flush=True)
    go = HOOKS.get("go_file")
    while go is None or not os.path.exists(go):
        time.sleep(0.01)
