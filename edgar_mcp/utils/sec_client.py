"""Every request to the SEC goes through this module.

One rate limiter, one retry rule and one set of errors, so a failed request
is never mistaken for "the SEC has no such data".
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import NamedTuple

import httpx

from edgar_mcp.config.constants import HTTP_HEADERS, USER_AGENT
from edgar_mcp.config.settings import (
    API_REQUEST_TIMEOUT, API_RETRY_COUNT, API_RETRY_DELAY, FROZEN_DIR,
    RATE_LIMIT_REQUESTS_PER_SECOND, RECORD_DIR,
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 150 * 1024 * 1024
MAX_RETRY_WAIT = 60  # seconds


class SecError(Exception):
    """A request to the SEC did not produce usable data."""
    code = "UPSTREAM_UNAVAILABLE"


class SecNotFound(SecError):
    """The SEC answered: there is nothing at this address."""
    code = "NOT_FOUND"


class SecRateLimited(SecError):
    code = "RATE_LIMITED"


class SecUnsupported(SecError):
    """The response is too large or not a type this tool reads."""
    code = "UNSUPPORTED_DOCUMENT"


class SecNoFrozenCopy(SecError):
    """Frozen copies only, and this address has none: the network is never tried."""
    code = "NO_FROZEN_COPY"


class SecNoUserAgent(SecError):
    """EDGAR_USER_AGENT is not set, so the SEC would not know who is asking."""
    code = "NO_USER_AGENT"


def _copy_paths(folder, url):
    """The body file and the description file of one saved response."""
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(folder, name + ".body"), os.path.join(folder, name + ".json")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Response(NamedTuple):
    """One answer from the SEC: the body, its content type, the UTC time it
    left the SEC, and whether it was read back from a frozen copy."""
    body: bytes
    content_type: str
    retrieved_at: str
    frozen: bool = False


def _read_frozen(url, max_bytes):
    body_path, about_path = _copy_paths(FROZEN_DIR, url)
    try:
        with open(about_path, encoding="utf-8") as f:
            about = json.load(f)
        size = os.path.getsize(body_path)
        with open(body_path, "rb") as f:
            body = f.read(max_bytes + 1)
    except FileNotFoundError:
        raise SecNoFrozenCopy(f"live sources are blocked and there is no frozen copy of {url}") from None
    except (OSError, ValueError) as e:
        raise SecError(f"the frozen copy of {url} could not be read ({e})") from None
    if not (isinstance(about, dict) and about.get("url") == url and about.get("status") in (200, 404)
            and isinstance(about.get("sha256"), str) and isinstance(about.get("content_type"), str)
            and isinstance(about.get("retrieved_at"), str)):
        raise SecError(f"the description of the frozen copy of {url} is not readable")
    if about["status"] == 404:
        if size:
            raise SecError(f"the frozen copy of {url} does not match its description")
        raise SecNotFound(f"nothing at {url} (frozen copy)")
    if size > max_bytes:
        raise SecUnsupported(f"{url} is larger than {max_bytes:,} bytes")
    if hashlib.sha256(body).hexdigest() != about["sha256"]:
        raise SecError(f"the frozen copy of {url} does not match its description")
    # A replay reports when the copy left the SEC, not when it was read back.
    return Response(body, about["content_type"], about["retrieved_at"], frozen=True)


def _record(url, status, body, content_type, retrieved_at):
    """Save a live response as a frozen copy; written whole or not at all."""
    os.makedirs(RECORD_DIR, exist_ok=True)
    body_path, about_path = _copy_paths(RECORD_DIR, url)
    about = {"url": url, "status": status, "content_type": content_type,
             "sha256": hashlib.sha256(body).hexdigest(),
             "retrieved_at": retrieved_at}
    for path, data in ((body_path, body), (about_path, json.dumps(about, indent=1).encode("utf-8"))):
        handle, temp = tempfile.mkstemp(dir=RECORD_DIR, suffix=".tmp")
        with os.fdopen(handle, "wb") as f:
            f.write(data)
        os.replace(temp, path)


# Redirects are not followed: every address is checked by the caller first.
_client = httpx.Client(headers=HTTP_HEADERS, timeout=API_REQUEST_TIMEOUT, follow_redirects=False)
# The limiter and the back-off time below are shared by every thread of this
# process only. Two processes on one machine (two MCP servers, a server and a
# batch run) each keep their own, and the SEC counts them together.
_rate_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0  # time.monotonic() before which no request may be sent


def _back_off(seconds):
    """Hold every thread's requests for `seconds`, as the SEC asked."""
    global _blocked_until
    with _rate_lock:
        _blocked_until = max(_blocked_until, time.monotonic() + seconds)


def _wait_for_turn(url):
    """Space requests out; the SEC allows 10 a second per user.

    While the SEC has asked for a pause, waits it out if it is short and
    otherwise raises SecRateLimited saying how long is left, so no thread
    sends a request the SEC has already said it will refuse.
    """
    global _last_request
    with _rate_lock:
        blocked = _blocked_until - time.monotonic()
        if blocked > MAX_RETRY_WAIT:
            raise SecRateLimited(
                f"the SEC asked for a pause; no request may be sent for another {blocked:.0f} seconds ({url})")
        pause = max(blocked, (1.0 / RATE_LIMIT_REQUESTS_PER_SECOND) - (time.monotonic() - _last_request))
        if pause > 0:
            time.sleep(pause)
        _last_request = time.monotonic()


def _retry_after(response, default):
    """Seconds to wait, from a Retry-After header given as seconds or as a date."""
    value = response.headers.get("Retry-After")
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return default


def fetch(url, max_bytes=MAX_JSON_BYTES):
    """GET `url`. Returns (body bytes, content type); see fetch_response."""
    response = fetch_response(url, max_bytes)
    return response.body, response.content_type


def fetch_response(url, max_bytes=MAX_JSON_BYTES):
    """GET `url`. Returns a Response; its retrieved_at is the UTC time the
    body left the SEC (the recorded time, for a frozen copy).

    Retries timeouts, HTTP 429 and HTTP 5xx. A Retry-After from the SEC is
    honoured in full by every thread: a short one is waited out, a long one
    raises at once with the pause left in force. Raises SecNotFound for 404,
    SecRateLimited for 403/429, SecUnsupported when the body exceeds
    `max_bytes`, SecNoUserAgent when EDGAR_USER_AGENT is not set, and
    SecError for anything else; a 5xx whose Retry-After is too long to wait
    raises SecRateLimited, since the SEC has set a pause. With EDGAR_FROZEN_DIR set,
    answers from the frozen copies only (SecNoFrozenCopy when there is none);
    with EDGAR_RECORD_DIR set, saves each live answer, a 404 included.
    """
    if FROZEN_DIR:
        return _read_frozen(url, max_bytes)
    if not USER_AGENT:
        raise SecNoUserAgent('set EDGAR_USER_AGENT to your name and email, e.g. "Jane Doe jane@example.com"; '
                             f"the SEC asks every client to identify itself ({url})")
    failure = SecError(f"no response from {url}")
    for attempt in range(API_RETRY_COUNT + 1):
        wait, asked = API_RETRY_DELAY * (2 ** attempt), False
        _wait_for_turn(url)
        try:
            with _client.stream("GET", url) as response:
                status = response.status_code
                if status == 404:
                    if RECORD_DIR:
                        _record(url, 404, b"", "", _now())
                    raise SecNotFound(f"nothing at {url}")
                if status == 403:
                    # The SEC answers 403 when it has blocked a client for its request rate.
                    raise SecRateLimited(f"SEC refused the request (HTTP 403) for {url}")
                if status == 429:
                    failure = SecRateLimited(f"SEC rate limit reached (HTTP 429) for {url}")
                    wait = _retry_after(response, wait)
                elif status >= 500:
                    failure = SecError(f"SEC server error (HTTP {status}) for {url}")
                    asked = "Retry-After" in response.headers
                    wait = _retry_after(response, wait)
                elif status != 200:
                    raise SecError(f"unexpected HTTP {status} for {url}")
                else:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise SecUnsupported(f"{url} is larger than {max_bytes:,} bytes")
                        chunks.append(chunk)
                    body, content_type = b"".join(chunks), response.headers.get("Content-Type", "")
                    retrieved_at = _now()  # once, so a recording and the answer agree
                    if RECORD_DIR:
                        _record(url, 200, body, content_type, retrieved_at)
                    return Response(body, content_type, retrieved_at)
        except httpx.HTTPError as e:
            failure = SecError(f"request to {url} failed: {e}")
        _back_off(wait)
        if wait > MAX_RETRY_WAIT:
            # Too long to hold a tool call open; the pause stays in force. A
            # server error with a long Retry-After is a pause the SEC has set.
            kind = SecRateLimited if asked else type(failure)
            raise kind(f"{failure}; the SEC asked for a pause of {wait:.0f} seconds")
        if attempt < API_RETRY_COUNT:
            logger.warning(f"{failure}; retrying in {wait:.0f}s")
    raise failure


class _NumberText(str):
    """A JSON number kept as the text it was written as (see decode_json)."""


def _float(text):
    value = float(text)
    # The double is kept when the shortest text that reads back as it, which
    # is what a JSON encoder writes, denotes exactly the decimal in the source.
    if math.isfinite(value) and Decimal(repr(value)) == Decimal(text):
        return value
    return _NumberText(text)


def _int(text):
    try:
        return int(text)
    except ValueError:  # more digits than Python converts
        return _NumberText(text)


def _collect_text(node, path, found):
    items = list(node.items()) if isinstance(node, dict) else list(enumerate(node))
    for k, v in items:
        if isinstance(v, _NumberText):
            node[k] = str(v)
            found.append(path + [k])
        elif isinstance(v, (dict, list)):
            _collect_text(v, path + [k], found)


def paths_under(paths, prefix) -> list:
    """The paths of `paths` (as decode_json lists them) that lie under
    `prefix`, made relative to it, in their order."""
    n = len(prefix)
    return [list(p[n:]) for p in paths or [] if len(p) > n and list(p[:n]) == list(prefix)]


def decode_json(body):
    """Parse JSON without changing the value of any number.

    An integer is a Python int, and a number with a fraction or an exponent
    is a float when the float denotes the same decimal value as the text
    (1.50 is 1.5). Any other number (9007199254740993.0, 1e400, a NaN or
    Infinity constant, an integer of more digits than Python converts) is
    kept as its text, a string. Returns (data, paths): `paths` lists where
    each number kept as text is, as the keys and list indexes from the top,
    in document order.
    """
    data = json.loads(body, parse_float=_float, parse_int=_int, parse_constant=_NumberText)
    if isinstance(data, _NumberText):
        return str(data), [[]]
    found = []
    if isinstance(data, (dict, list)):
        _collect_text(data, [], found)
    return data, found


def get_json(url, cache=None, key=None, ttl=None):
    """Fetch and parse JSON with decode_json, so no number changes value.
    Returns (data, meta).

    `meta` records where the data came from and when it left the SEC:
    {'source_url', 'retrieved_at', 'cache_hit', 'frozen', 'sha256',
    'numbers_as_text'}; a cache hit keeps the retrieval time and the hash of
    the bytes of the answer it holds (sha256 is None for an entry stored
    before hashes were kept). An entry stored without `numbers_as_text` was
    parsed by a release that rounded numbers to doubles, and is not used.
    """
    if cache is not None:
        entry = cache.get_entry(key or url)
        if entry and isinstance((entry.get('meta') or {}).get('numbers_as_text'), list):
            return entry['data'], {'sha256': None, **entry['meta'], 'source_url': url, 'cache_hit': True}
    response = fetch_response(url)
    try:
        data, numbers_as_text = decode_json(response.body)
    except (ValueError, RecursionError) as e:
        raise SecError(f"{url} did not return JSON: {e}") from e
    meta = {
        'source_url': url,
        'retrieved_at': response.retrieved_at,
        'frozen': response.frozen,
        'sha256': hashlib.sha256(response.body).hexdigest(),
        'numbers_as_text': numbers_as_text,
    }
    if cache is not None:
        cache.set(key or url, data, ttl=ttl, meta=meta)
    return data, {**meta, 'cache_hit': False}
