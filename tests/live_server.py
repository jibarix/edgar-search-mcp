"""The MCP server for tests/live_check.py, with every request counted at the handoff.

    python -X utf8 tests/live_server.py <handoff log> <cap>

Runs edgar_mcp exactly as `python -m edgar_mcp` does, except that every
request the shared limiter hands to the network is first appended to the
handoff log (monotonic time and URL), and a request past `cap` is refused
with UPSTREAM_UNAVAILABLE instead of being sent. A cap of 0 blocks the
network. Not a test: tests/live_check.py starts it.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from edgar_mcp import ratelimit  # noqa: E402  (before sec_client builds its client)
from edgar_mcp.utils.errors import SecError  # noqa: E402

LOG, CAP = sys.argv[1], int(sys.argv[2])
_sent = 0


class CountingTransport:
    """The network transport, behind a count of requests handed to it."""

    def __init__(self, inner):
        self.inner = inner

    def handle_request(self, request):
        global _sent
        if _sent >= CAP:
            raise SecError(f"live check: the cap of {CAP} requests is reached; {request.url} was not sent")
        _sent += 1
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.monotonic():.6f}\t{request.url}\n")
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


_init = ratelimit.RateLimitedTransport.__init__


def _counting_init(self, inner, limiter=None):
    _init(self, CountingTransport(inner), limiter)


ratelimit.RateLimitedTransport.__init__ = _counting_init

from edgar_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
