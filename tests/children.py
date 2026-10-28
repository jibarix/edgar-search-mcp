"""Helpers for the multi-process tests (not a test script itself): start a
limiter_child.py process, watch for the marker a test hook prints, collect its
answer, and count times in a sliding one-second window."""
import bisect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHILD = os.path.join(ROOT, "tests", "limiter_child.py")
EPS = 1e-9


def rate_file():
    """A fresh state file path, so no test ever touches the user's own limiter."""
    return os.path.join(tempfile.mkdtemp(prefix="edgar_rate_test_"), "state")


def environment(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    env.update(EDGAR_USER_AGENT="test test@example.com",
               EDGAR_CACHE_DIR=tempfile.mkdtemp(prefix="edgar_cache_test_"))
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def urls(n, start=0):
    return [f"https://data.sec.gov/submissions/CIK{i:010d}.json" for i in range(start, start + n)]


class Child:
    """One limiter_child.py process. `hooks` becomes EDGAR_TEST_HOOKS."""

    def __init__(self, spec, hooks=None, **env):
        self.env = environment(EDGAR_TEST_HOOKS=json.dumps(hooks or {}), **env)
        self.proc = subprocess.Popen([sys.executable, "-X", "utf8", CHILD, json.dumps(spec)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.out, self.err, self.markers = [], [], {}
        self._seen = threading.Condition()
        self._threads = [threading.Thread(target=self._read, args=(self.proc.stdout, self.out), daemon=True),
                         threading.Thread(target=self._read, args=(self.proc.stderr, self.err), daemon=True)]
        for t in self._threads:
            t.start()

    def _read(self, stream, lines):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            with self._seen:
                lines.append(line)
                if line.startswith("PAUSED "):
                    self.markers[line.split(" ", 1)[1].strip()] = time.monotonic()
                self._seen.notify_all()

    def wait_marker(self, name, timeout=30):
        """The time the hook `name` reported itself paused, or None."""
        with self._seen:
            self._seen.wait_for(lambda: name in self.markers or self.proc.poll() is not None, timeout)
            return self.markers.get(name)

    def result(self, timeout=120):
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for t in self._threads:
            t.join(10)
        for line in reversed(self.out):
            if line.startswith("{"):
                return json.loads(line)
        return {"crashed": True, "returncode": self.proc.returncode, "stderr": "\n".join(self.err[-30:]),
                "events": [], "handler": [], "results": []}

    def kill(self):
        """Hard termination: TerminateProcess on Windows, SIGKILL elsewhere."""
        self.proc.kill()
        self.proc.wait()
        return time.monotonic()


def times(result, kind):
    return [e[1] for e in result.get("events", []) if e[0] == kind]


def max_in_window(values):
    """The most values in any half-open window [t, t + 1 s), over every logged t."""
    values = sorted(values)
    return max((bisect.bisect_left(values, v + 1.0) - i for i, v in enumerate(values)), default=0)


def min_spacing(values):
    values = sorted(values)
    return min((b - a for a, b in zip(values, values[1:])), default=float("inf"))
