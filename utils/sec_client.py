"""Every request to the SEC goes through this module.

One rate limiter, one retry rule and one set of errors, so a failed request
is never mistaken for "the SEC has no such data".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from config.constants import HTTP_HEADERS, USER_AGENT
from config.settings import (
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


def _read_frozen(url, max_bytes):
    body_path, about_path = _copy_paths(FROZEN_DIR, url)
    try:
        with open(about_path, encoding="utf-8") as f:
            about = json.load(f)
        with open(body_path, "rb") as f:
            body = f.read()
    except OSError:
        raise SecNoFrozenCopy(f"live sources are blocked and there is no frozen copy of {url}") from None
    if about.get("url") != url or hashlib.sha256(body).hexdigest() != about.get("sha256"):
        raise SecError(f"the frozen copy of {url} does not match its description")
    if about.get("status") == 404:
        raise SecNotFound(f"nothing at {url} (frozen copy)")
    if len(body) > max_bytes:
        raise SecUnsupported(f"{url} is larger than {max_bytes:,} bytes")
    return body, about.get("content_type", "")


def _record(url, status, body, content_type):
    """Save a live response as a frozen copy; written whole or not at all."""
    os.makedirs(RECORD_DIR, exist_ok=True)
    body_path, about_path = _copy_paths(RECORD_DIR, url)
    about = {"url": url, "status": status, "content_type": content_type,
             "sha256": hashlib.sha256(body).hexdigest(),
             "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
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
    """GET `url`. Returns (body bytes, content type).

    Retries timeouts, HTTP 429 and HTTP 5xx. A Retry-After from the SEC is
    honoured in full by every thread: a short one is waited out, a long one
    raises at once with the pause left in force. Raises SecNotFound for 404,
    SecRateLimited for 403/429, SecUnsupported when the body exceeds
    `max_bytes`, SecNoUserAgent when EDGAR_USER_AGENT is not set, and
    SecError for anything else. With EDGAR_FROZEN_DIR set,
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
        wait = API_RETRY_DELAY * (2 ** attempt)
        _wait_for_turn(url)
        try:
            with _client.stream("GET", url) as response:
                status = response.status_code
                if status == 404:
                    if RECORD_DIR:
                        _record(url, 404, b"", "")
                    raise SecNotFound(f"nothing at {url}")
                if status == 403:
                    # The SEC answers 403 when it has blocked a client for its request rate.
                    raise SecRateLimited(f"SEC refused the request (HTTP 403) for {url}")
                if status == 429:
                    failure = SecRateLimited(f"SEC rate limit reached (HTTP 429) for {url}")
                    wait = _retry_after(response, wait)
                elif status >= 500:
                    failure = SecError(f"SEC server error (HTTP {status}) for {url}")
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
                    if RECORD_DIR:
                        _record(url, 200, body, content_type)
                    return body, content_type
        except httpx.HTTPError as e:
            failure = SecError(f"request to {url} failed: {e}")
        _back_off(wait)
        if wait > MAX_RETRY_WAIT:
            # Too long to hold a tool call open; the pause stays in force.
            raise type(failure)(f"{failure}; the SEC asked for a pause of {wait:.0f} seconds")
        if attempt < API_RETRY_COUNT:
            logger.warning(f"{failure}; retrying in {wait:.0f}s")
    raise failure


def get_json(url, cache=None, key=None, ttl=None):
    """Fetch and parse JSON. Returns (data, meta).

    `meta` records where the data came from and when it left the SEC:
    {'source_url', 'retrieved_at', 'cache_hit'}.
    """
    if cache is not None:
        entry = cache.get_entry(key or url)
        if entry:
            return entry['data'], {**entry.get('meta', {}), 'source_url': url, 'cache_hit': True}
    body, _ = fetch(url)
    try:
        data = json.loads(body)
    except ValueError as e:
        raise SecError(f"{url} did not return JSON: {e}") from e
    meta = {
        'source_url': url,
        'retrieved_at': datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if cache is not None:
        cache.set(key or url, data, ttl=ttl, meta=meta)
    return data, {**meta, 'cache_hit': False}
