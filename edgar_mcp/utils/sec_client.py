"""Every request to the SEC goes through this module.

One rate limiter (edgar_mcp.ratelimit, shared by every process on the
machine), one retry rule and one set of errors, so a failed request is never
mistaken for "the SEC has no such data".
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import NamedTuple

import httpx

from edgar_mcp import _testhooks, ratelimit
from edgar_mcp.config.constants import HTTP_HEADERS, USER_AGENT
from edgar_mcp.config.settings import (
    API_REQUEST_TIMEOUT, API_RETRY_COUNT, API_RETRY_DELAY, FROZEN_DIR, RECORD_DIR,
)
from edgar_mcp.utils.errors import (  # noqa: F401  (re-exported: callers import them from here)
    SecError, SecLimiterUnavailable, SecNoFrozenCopy, SecNotFound, SecNoUserAgent, SecRateLimited,
    SecUnsupported,
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 150 * 1024 * 1024
MAX_RETRY_WAIT = ratelimit.MAX_RETRY_WAIT  # seconds
FORBIDDEN_PAUSE = 600  # seconds, after a 403 without Retry-After


def _copy_paths(folder, url):
    """The old-layout body file and the description file of one saved response."""
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(folder, name + ".body"), os.path.join(folder, name + ".json")


def _body_name(url, body):
    """A recorded body's file name: the hashes of its address and of its bytes."""
    return f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.{hashlib.sha256(body).hexdigest()}.body"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Response(NamedTuple):
    """One answer from the SEC: the body, its content type, the UTC time it
    left the SEC, and whether it was read back from a frozen copy."""
    body: bytes
    content_type: str
    retrieved_at: str
    frozen: bool = False


def _read_frozen(url, max_bytes, folder=None):
    """The saved response in `folder` (FROZEN_DIR by default). Its description
    names the body by hash; one without a body name is the old layout,
    <sha256(url)>.body."""
    folder = folder or FROZEN_DIR
    body_path, about_path = _copy_paths(folder, url)
    try:
        with open(about_path, encoding="utf-8") as f:
            about = json.load(f)
        if not isinstance(about, dict):
            raise ValueError("the description is not a JSON object")
        if "body" in about:
            name = about["body"]
            prefix = os.path.basename(about_path)[:-len(".json")] + "."
            if not (isinstance(name, str) and _BODY_NAME.fullmatch(name) and name.startswith(prefix)):
                raise ValueError(f"the body name {name!r} is not valid")
            body_path = os.path.join(folder, name)
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


_BODY_NAME = re.compile(r"[0-9a-f]{64}\.[0-9a-f]{64}\.body")


def _publish(path, data, kind):
    """Publish `data` at `path`, whole, by hard-linking a complete temp file:
    False when the name already exists, which is never replaced. `kind`
    ("body" or "desc") names the test hook points."""
    folder = os.path.dirname(path)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _testhooks.pause(f"after_{kind}_temp")
        _testhooks.pause(f"before_{kind}_link")
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        except OSError as e:
            raise SecError(f"the record directory {folder} cannot publish atomically (no hard link: {e})") from None
        _testhooks.pause(f"after_{kind}_link")
        return True
    finally:
        try:
            os.remove(temp)
        except OSError as e:  # e.g. held by antivirus; a leftover temp is harmless
            logger.warning(f"could not remove the temp file {temp}: {e}")


def _record(url, status, body, content_type, retrieved_at):
    """Publish a live response in RECORD_DIR, once: the body under its hash,
    then the description naming it. Nothing published is ever replaced. If
    another capture published this address first, returns its Response (its
    metadata) when it holds the same status and bytes, and raises SecError
    when it differs; returns None when this capture is the one published."""
    os.makedirs(RECORD_DIR, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    body_path = os.path.join(RECORD_DIR, _body_name(url, body))
    if not _publish(body_path, body, "body"):
        # The same address and bytes were published before: check, never touch.
        with open(body_path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != digest:
                raise SecError(f"the recording of {url} is corrupt: {body_path} does not match its name")
    about = {"url": url, "status": status, "content_type": content_type, "sha256": digest,
             "retrieved_at": retrieved_at, "body": os.path.basename(body_path)}
    about_path = _copy_paths(RECORD_DIR, url)[1]
    if _publish(about_path, json.dumps(about, indent=1).encode("utf-8"), "desc"):
        return None
    with open(about_path, encoding="utf-8") as f:
        winner = json.load(f)
    if winner.get("status") != status or winner.get("sha256") != digest:
        raise SecError(f"two captures of {url} differ; the first one recorded is kept")
    return Response(body, winner["content_type"], winner["retrieved_at"], frozen=True)


# Redirects are not followed: every address is checked by the caller first.
# Every request waits for the limiter shared by every process on the machine.
_client = ratelimit.limited_client(headers=HTTP_HEADERS, timeout=API_REQUEST_TIMEOUT, follow_redirects=False)


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

    Retries timeouts, HTTP 429 and HTTP 5xx. A pause the SEC asks for (a
    429, a 403, a Retry-After on a 5xx) is published to every process on the
    machine through the shared limiter: a short one is waited out, a long
    one raises at once with the pause left in force. A 5xx without
    Retry-After and a timeout wait in this call only. Raises SecNotFound for
    404, SecRateLimited for 403/429, SecUnsupported when the body exceeds
    `max_bytes`, SecNoUserAgent when EDGAR_USER_AGENT is not set,
    SecLimiterUnavailable when the shared limiter cannot be used, and
    SecError for anything else; a 5xx whose Retry-After is too long to wait
    raises SecRateLimited, since the SEC has set a pause. With EDGAR_FROZEN_DIR set,
    answers from the frozen copies only (SecNoFrozenCopy when there is none);
    with EDGAR_RECORD_DIR set, publishes each live 200 or 404 once and
    replays it from then on (frozen), never capturing that address again.
    """
    if FROZEN_DIR:
        return _read_frozen(url, max_bytes)
    if RECORD_DIR and os.path.exists(_copy_paths(RECORD_DIR, url)[1]):
        # Snapshot: an address recorded once is replayed, never captured again.
        return _read_frozen(url, max_bytes, RECORD_DIR)
    if not USER_AGENT:
        raise SecNoUserAgent('set EDGAR_USER_AGENT to your name and email, e.g. "Jane Doe jane@example.com"; '
                             f"the SEC asks every client to identify itself ({url})")
    failure = SecError(f"no response from {url}")
    for attempt in range(API_RETRY_COUNT + 1):
        wait, shared = API_RETRY_DELAY * (2 ** attempt), False
        try:
            with _client.stream("GET", url) as response:
                status = response.status_code
                if status == 404:
                    if RECORD_DIR:
                        _record(url, 404, b"", "", _now())  # another capture's 404 is the same answer
                    raise SecNotFound(f"nothing at {url}")
                if status == 403:
                    # The SEC answers 403 when it has blocked a client for its request rate:
                    # every process holds off, for the Retry-After or FORBIDDEN_PAUSE.
                    pause = _retry_after(response, FORBIDDEN_PAUSE)
                    ratelimit.get_default().pause(pause)
                    raise SecRateLimited(f"SEC refused the request (HTTP 403) for {url}; "
                                         f"no request is sent for {pause:.0f} seconds")
                if status == 429:
                    failure = SecRateLimited(f"SEC rate limit reached (HTTP 429) for {url}")
                    wait, shared = _retry_after(response, wait), True
                elif status >= 500:
                    failure = SecError(f"SEC server error (HTTP {status}) for {url}")
                    shared = "Retry-After" in response.headers
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
                    recorded = _record(url, 200, body, content_type, retrieved_at) if RECORD_DIR else None
                    return recorded or Response(body, content_type, retrieved_at)
        except httpx.HTTPError as e:
            failure = SecError(f"request to {url} failed: {e}")
        if shared:
            # A pause the SEC asked for holds every process; the next
            # admission waits it out.
            ratelimit.get_default().pause(wait)
        if wait > MAX_RETRY_WAIT:
            # Too long to hold a tool call open; the pause stays in force. A
            # server error with a long Retry-After is a pause the SEC has set.
            kind = SecRateLimited if shared else type(failure)
            raise kind(f"{failure}; the SEC asked for a pause of {wait:.0f} seconds")
        if attempt < API_RETRY_COUNT:
            logger.warning(f"{failure}; retrying in {wait:.0f}s")
            if not shared:
                time.sleep(wait)  # this call only: other processes are not held
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
