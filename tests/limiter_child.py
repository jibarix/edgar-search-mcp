"""A child process for the limiter and recorder tests (not a test script itself).

    python -X utf8 tests/limiter_child.py '<spec as JSON>'

The process is configured only through the public environment variables
(EDGAR_RATE_FILE, EDGAR_RATE_LIMIT, EDGAR_RECORD_DIR, EDGAR_USER_AGENT) and the
test-only EDGAR_TEST_HOOKS. The SEC is a stand-in transport answering from the
spec; nothing reaches the network. The spec:
    urls        the addresses to fetch, in order
    responses   answers given in order, each {status, body, headers}; then `default`
    default     the answer once `responses` is used up (200 with `body` if absent)
    start_at    a time.monotonic() value to wait for before the first fetch
    retry_delay sec_client.API_RETRY_DELAY for this process
    plain       true: no rate-limited transport (recorder tests)
    tool        a server tool to call through its wrapper first, with `arguments`
The last line of stdout is JSON: the limiter's events, the stand-in's entry
times and one result per address.
"""
import base64
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

from edgar_mcp import _testhooks, ratelimit  # noqa: E402
from edgar_mcp.utils import sec_client  # noqa: E402


def main():
    spec = json.loads(sys.argv[1])
    answers = list(spec.get("responses", []))
    default = spec.get("default", {"status": 200, "body": "default body"})
    handler_times = []

    def handler(request):
        handler_times.append(time.monotonic())
        answer = answers.pop(0) if answers else default
        return httpx.Response(answer.get("status", 200), content=answer.get("body", "").encode("utf-8"),
                              headers=answer.get("headers", {"Content-Type": "application/json"}))

    inner = httpx.MockTransport(handler)
    sec_client._client = httpx.Client(transport=inner if spec.get("plain") else ratelimit.RateLimitedTransport(inner))
    if "retry_delay" in spec:
        sec_client.API_RETRY_DELAY = spec["retry_delay"]
    if "start_at" in spec:
        while time.monotonic() < spec["start_at"]:
            time.sleep(0.001)
    results = []
    if "tool" in spec:
        import asyncio
        from edgar_mcp import server
        try:
            asyncio.run(getattr(server, spec["tool"])(**spec.get("arguments", {})))
            results.append({"ok": True})
        except Exception as e:
            text = str(e)
            results.append({"error": text.split(":", 1)[0], "message": text})
    for url in spec.get("urls", []):
        try:
            r = sec_client.fetch_response(url)
            results.append({"sha256": hashlib.sha256(r.body).hexdigest(), "retrieved_at": r.retrieved_at,
                            "frozen": r.frozen, "content_type": r.content_type,
                            "body": base64.b64encode(r.body).decode("ascii")})
        except sec_client.SecError as e:
            results.append({"error": e.code, "message": str(e)})
    print(json.dumps({"pid": os.getpid(), "events": _testhooks.EVENTS, "handler": handler_times,
                      "results": results, "done": time.monotonic()}))


if __name__ == "__main__":
    main()
