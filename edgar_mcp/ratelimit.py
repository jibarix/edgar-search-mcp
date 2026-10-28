"""One request limit shared by every process on this machine.

The SEC allows 10 requests a second per user, counted across every client
the user runs. Two MCP servers, or a server and a batch run, therefore share
one limiter: a small state file (EDGAR_RATE_FILE, else
%LOCALAPPDATA%\\sec-edgar-rate\\state on Windows and
$XDG_STATE_HOME/sec-edgar-rate/state elsewhere), read and written under a
lock on one byte of the same file. EDGAR_RATE_LIMIT sets this process's rate,
more than 0 and at most 10 a second (default 8).

A request is admitted when every rule allows it:
  - the interval since the last admission, by any process, is at least the
    longer of this process's interval and the one that admission used;
  - an admission is more than 1 s + GRACE after the tenth before it, so no
    one-second window holds more than ten requests handed to the transport;
  - no pause the SEC asked for (HTTP 429, 403, or a 5xx with Retry-After)
    is in force, and no recovery barrier is.
A request handed over more than GRACE after its admission gives the
admission up and asks again (RateLimitedTransport).

Times are time.monotonic(), which every process on the machine shares. Each
write stores both clocks; when they disagree (a reboot, the wall clock
stepped by more than 5 s) the stored times are not trusted: the log is
cleared, admissions wait a full window (recovery_until), and a pause in
force restarts in full. A state file that cannot be read is never guessed
at: every request is refused with RATE_LIMITER_UNAVAILABLE until a person
resets it:
    python -m edgar_mcp.ratelimit --reset --reason "<why>"
which keeps the previous bytes in reset.log next to the file and holds
requests for 60 s.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

import httpx

from edgar_mcp import _testhooks
from edgar_mcp.utils.errors import SecLimiterUnavailable, SecRateLimited

STATE_SIZE = 2048  # bytes of state, JSON padded with spaces
LOCK_OFFSET = 4096  # the byte locked, past the state
GRACE = 0.025  # seconds an admission may wait to be handed to the transport
LOCK_TIMEOUT = 5  # seconds to wait for the lock
LOCK_RETRY = 0.005
CLOCK_TOLERANCE = 5  # seconds the two clocks may disagree between writes
RESET_HOLD = 60  # seconds no request is sent after a reset
MAX_RETRY_WAIT = 60  # a pause longer than this is refused, not waited out
DEFAULT_RATE = 8
WINDOW = 10  # admissions per second the SEC allows
_FIELDS = ("v", "interval", "last_admit", "log", "blocked_until", "recovery_until", "pause",
           "write_wall", "write_mono")
RESET_COMMAND = 'python -m edgar_mcp.ratelimit --reset --reason "<why>"'
logger = logging.getLogger(__name__)


def default_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = base if base and os.path.isabs(base) else os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = base if base and os.path.isabs(base) else os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(root, "sec-edgar-rate", "state")


def _fresh() -> dict:
    return {"v": 1, "interval": None, "last_admit": None, "log": [], "blocked_until": None,
            "recovery_until": None, "pause": None, "write_wall": None, "write_mono": None}


def _sum(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("ascii")).hexdigest()


def encode(state: dict) -> bytes:
    """The bytes of `state` as stored: its fields and their sha256 (so a torn
    write is detected), padded to STATE_SIZE."""
    fields = {k: state[k] for k in _FIELDS}
    data = json.dumps({"sum": _sum(fields), **fields}).encode("ascii")
    if len(data) > STATE_SIZE:
        raise SecLimiterUnavailable(f"the limiter state is larger than {STATE_SIZE} bytes")
    return data.ljust(STATE_SIZE, b" ")


def _number(value, empty=True):
    if value is None:
        return empty
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _decode(data: bytes) -> dict | None:
    """The state in `data`, or None when it is not a whole, valid state."""
    try:
        state = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(state, dict) or set(state) != {"sum", *_FIELDS}:
        return None
    fields = {k: state[k] for k in _FIELDS}
    pause = fields["pause"]
    ok = (state["sum"] == _sum(fields) and fields["v"] == 1
          and _number(fields["interval"]) and (fields["interval"] is None or fields["interval"] > 0)
          and all(_number(fields[k]) for k in ("last_admit", "blocked_until", "recovery_until", "write_wall",
                                               "write_mono"))
          and (fields["write_wall"] is None) == (fields["write_mono"] is None)
          and isinstance(fields["log"], list) and len(fields["log"]) <= WINDOW
          and all(_number(t, False) for t in fields["log"])
          and (pause is None or (isinstance(pause, dict) and set(pause) == {"start_wall", "start_mono", "seconds"}
                                 and all(_number(v, False) for v in pause.values()) and pause["seconds"] >= 0)))
    return fields if ok else None


class _Clock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)  # last: the name hides the module below it


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd):
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd):
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, LOCK_OFFSET, os.SEEK_SET)

    def _unlock(fd):
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, LOCK_OFFSET, os.SEEK_SET)


def _open(path):
    """A raw descriptor on the state file. A missing file is created whole: a
    fresh state is written to a temp file and published by a hard link, which
    never replaces a file, so no process sees a new state file empty. An
    existing file is never taken as fresh, whatever its size."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    fd, temp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=folder or ".")
    try:
        _write_all(fd, encode(_fresh()))
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(temp, path)
        except FileExistsError:
            pass  # another process created it first
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(temp)
        except OSError:
            pass
    return os.open(path, flags)


def _lock(fd, path):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            _try_lock(fd)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise SecLimiterUnavailable(
                    f"the limiter state {path} stayed locked for {LOCK_TIMEOUT} s; no request was sent") from None
            time.sleep(LOCK_RETRY)


def _read_all(fd) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd, data, offset=0):
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class Limiter:
    """The shared limit, for one process at `rate` requests a second.

    `clock` (monotonic, time and sleep) is replaced only by tests. The state
    file is opened on first use and kept open for the life of the process.
    """

    def __init__(self, path: str, rate: float, clock=None):
        if not (isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0):
            raise ValueError(f"rate must be a positive number, got {rate!r}")
        self.path, self.interval, self.clock = path, 1.0 / rate, clock or _Clock()
        self._fd = None
        self._lock = threading.Lock()

    def _unavailable(self, why):
        return SecLimiterUnavailable(f"the limiter state {self.path} {why}; no request is sent until it is "
                                     f"reset: {RESET_COMMAND}")

    def _transaction(self, step):
        """Run step(state, now, wall) on the reconciled state under both locks
        and write the state back; returns (step's result, recovered)."""
        with self._lock:
            if self._fd is None:
                try:
                    self._fd = _open(self.path)
                except OSError as e:
                    raise self._unavailable(f"cannot be opened ({e.strerror or e})") from None
            _lock(self._fd, self.path)
            held = 0.0
            try:
                _testhooks.pause("locked")
                held = _testhooks.hold("hold_lock")
                try:
                    data = _read_all(self._fd)
                except OSError as e:
                    raise self._unavailable(f"cannot be read ({e.strerror or e})") from None
                state = _decode(data) if len(data) == STATE_SIZE else None
                if state is None:
                    raise self._unavailable(f"is not a valid limiter state ({len(data)} bytes)")
                now, wall = self.clock.monotonic(), self.clock.time()
                recovered = self._reconcile(state, now, wall)
                result = step(state, now, wall)
                state["write_mono"], state["write_wall"] = self.clock.monotonic(), self.clock.time()
                self._write(encode(state))
            finally:
                _unlock(self._fd)
            if held:
                _testhooks.event("held_release", time.monotonic())
        if recovered:
            _testhooks.pause("after_recovery")
        return result

    def _write(self, data):
        try:
            if _testhooks.ACTIVE and _testhooks.HOOKS.get("pause_at") == "partial_write":
                _write_all(self._fd, data[:len(data.rstrip(b" ")) // 2])  # half the JSON, not of the padding
                _testhooks.pause("partial_write")
            _write_all(self._fd, data)
        except OSError as e:
            raise self._unavailable(f"cannot be written ({e.strerror or e})") from None

    def _reconcile(self, state, now, wall) -> bool:
        """Trust the stored times only while both clocks agree since the last
        write; clear a pause that has run out. True after a recovery."""
        mono, written = state["write_mono"], state["write_wall"]
        if mono is not None and (mono > now or abs((wall - written) - (now - mono)) > CLOCK_TOLERANCE):
            _testhooks.event("inconsistent", now)
            self._log_recovery(mono, written, now, wall)
            state["log"], state["last_admit"] = [], None
            state["recovery_until"] = now + 1 + GRACE
            if state["pause"] is not None:
                seconds = state["pause"]["seconds"]
                state["pause"] = {"start_wall": wall, "start_mono": now, "seconds": seconds}
                state["blocked_until"] = now + seconds
            else:
                state["blocked_until"] = None
            return True
        if state["pause"] is not None and (state["blocked_until"] is None or now >= state["blocked_until"]):
            state["pause"], state["blocked_until"] = None, None
        return False

    def _log_recovery(self, mono, written, now, wall):
        """Record a clock inconsistency with both clocks' readings: a warning,
        and a line in clock.log beside the state file."""
        entry = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "pid": os.getpid(),
                 "path": os.path.abspath(self.path), "written_wall": written, "written_mono": mono,
                 "now_wall": wall, "now_mono": now}
        logger.warning(f"the limiter state {self.path} was written at wall {written:.3f}, monotonic {mono:.3f}, "
                       f"and is read at wall {wall:.3f}, monotonic {now:.3f}: the clocks disagree, so the stored "
                       f"times are dropped; requests wait {1 + GRACE:.3f} s and a pause in force restarts in full")
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(self.path)), "clock.log"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning(f"could not write clock.log beside {self.path}: {e}")

    def admit(self) -> float:
        """Wait for this process's turn and take it; the admission time
        (monotonic). SecRateLimited when the SEC's pause has more than
        MAX_RETRY_WAIT seconds left; SecLimiterUnavailable when the state
        cannot be used."""
        def step(state, now, wall):
            blocked = state["blocked_until"]
            if blocked is not None and blocked - now > MAX_RETRY_WAIT:
                return "refused", blocked - now
            log = state["log"]
            earliest = max(t for t in (
                state["last_admit"] + max(self.interval, state["interval"] or 0)
                if state["last_admit"] is not None else None,
                log[-WINDOW] + 1 + GRACE if len(log) >= WINDOW else None,
                blocked, state["recovery_until"], now) if t is not None)
            if earliest > now:
                return "wait", earliest - now
            state["last_admit"], state["interval"] = now, self.interval
            state["log"] = (log + [now])[-WINDOW:]
            return "admitted", now

        while True:
            outcome, value = self._transaction(step)
            if outcome == "admitted":
                _testhooks.event("admit", value)
                return value
            if outcome == "refused":
                raise SecRateLimited(f"the SEC asked for a pause; no request may be sent for another "
                                     f"{value:.0f} seconds")
            self.clock.sleep(value)

    def pause(self, seconds: float):
        """Publish a pause the SEC asked for, to every process. A pause that
        ends sooner than the one in force does not replace it."""
        def step(state, now, wall):
            until = now + max(0.0, seconds)
            if state["pause"] is not None and state["blocked_until"] is not None and state["blocked_until"] >= until:
                return False
            state["pause"] = {"start_wall": wall, "start_mono": now, "seconds": max(0.0, seconds)}
            state["blocked_until"] = until
            _testhooks.event("pause", now, seconds)
            return True

        return self._transaction(step)

    def inspect(self) -> dict:
        """The reconciled state, as written back."""
        return self._transaction(lambda state, now, wall: json.loads(json.dumps(state)))

    def reset(self, reason: str) -> dict:
        """Replace the state with a fresh one holding requests RESET_HOLD
        seconds, whatever the file held; the previous bytes go to reset.log."""
        with self._lock:
            if self._fd is None:
                self._fd = _open(self.path)
            _lock(self._fd, self.path)
            try:
                previous = _read_all(self._fd)
                now, wall = self.clock.monotonic(), self.clock.time()
                entry = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason,
                         "path": os.path.abspath(self.path), "previous_size": len(previous),
                         "previous_sha256": hashlib.sha256(previous).hexdigest(),
                         "previous_base64": base64.b64encode(previous).decode("ascii")}
                with open(os.path.join(os.path.dirname(os.path.abspath(self.path)), "reset.log"), "a",
                          encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                state = _fresh()
                state.update(pause={"start_wall": wall, "start_mono": now, "seconds": RESET_HOLD},
                             blocked_until=now + RESET_HOLD, write_mono=now, write_wall=wall)
                # Written over the old bytes, then cut to size: a reset stopped part
                # way leaves a state that fails its checksum, never an empty file.
                self._write(encode(state))
                os.ftruncate(self._fd, STATE_SIZE)
                os.fsync(self._fd)
            finally:
                _unlock(self._fd)
        return entry

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


class _Broken:
    """The limiter when EDGAR_RATE_LIMIT is not valid: every request refused."""

    def __init__(self, message):
        self.message = message

    def admit(self):
        raise SecLimiterUnavailable(self.message)

    def pause(self, seconds):
        raise SecLimiterUnavailable(self.message)


_default = None
_default_lock = threading.Lock()


def config_error() -> str | None:
    """Why EDGAR_RATE_LIMIT cannot be used, or None."""
    value = os.environ.get("EDGAR_RATE_LIMIT")
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except ValueError:
        rate = None
    if rate is None or not math.isfinite(rate) or not 0 < rate <= WINDOW:
        return f"EDGAR_RATE_LIMIT must be a number of requests a second, more than 0 and at most 10; got {value!r}"
    return None


def get_default():
    """The process's limiter, from EDGAR_RATE_FILE and EDGAR_RATE_LIMIT."""
    global _default
    with _default_lock:
        if _default is None:
            problem = config_error()
            if problem:
                _default = _Broken(problem + "; no request is sent")
            else:
                rate = float(os.environ.get("EDGAR_RATE_LIMIT") or DEFAULT_RATE)
                _default = Limiter(os.environ.get("EDGAR_RATE_FILE") or default_path(), rate)
        return _default


def set_default(limiter):
    """Replace the process's limiter (tests)."""
    global _default
    with _default_lock:
        _default = limiter


class RateLimitedTransport(httpx.BaseTransport):
    """Hands a request to `inner` only after the shared limiter admits it,
    within GRACE of the admission; `limiter` None means the process's own."""

    def __init__(self, inner: httpx.BaseTransport, limiter=None):
        self.inner, self.limiter = inner, limiter

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        limiter = self.limiter or get_default()
        clock = getattr(limiter, "clock", time)
        _testhooks.delay("before_admit")
        while True:
            admitted = limiter.admit()
            _testhooks.delay("after_admit")
            now = clock.monotonic()
            if now - admitted <= GRACE:
                break
            _testhooks.event("abandon", now)
        _testhooks.event("handoff", now)
        _testhooks.delay("after_check")
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def limited_client(**kwargs) -> httpx.Client:
    """An httpx.Client whose every transport, proxies from the environment
    included, goes through the shared limiter."""
    client = httpx.Client(**kwargs)
    client._transport = RateLimitedTransport(client._transport)
    for pattern, transport in list(client._mounts.items()):
        if transport is not None:
            client._mounts[pattern] = RateLimitedTransport(transport)
    return client


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m edgar_mcp.ratelimit",
                                     description="Reset the request limiter shared by every edgar-search-mcp "
                                                 "process on this machine.")
    parser.add_argument("--reset", action="store_true", required=True,
                        help="replace the state file; its previous bytes are kept in reset.log")
    parser.add_argument("--reason", required=True, help="why the state is reset (kept in reset.log)")
    args = parser.parse_args(argv)
    problem = config_error()
    path = os.environ.get("EDGAR_RATE_FILE") or default_path()
    limiter = Limiter(path, DEFAULT_RATE if problem else float(os.environ.get("EDGAR_RATE_LIMIT") or DEFAULT_RATE))
    try:
        entry = limiter.reset(args.reason)
    except (OSError, SecLimiterUnavailable) as e:
        print(f"could not reset {path}: {e}", file=sys.stderr)
        return 1
    finally:
        limiter.close()
    print(f"reset {path} ({entry['previous_size']} bytes kept in reset.log); "
          f"no request is sent for {RESET_HOLD} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
