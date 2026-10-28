import os
import sys

from edgar_mcp import ratelimit
from edgar_mcp.config.constants import USER_AGENT
from edgar_mcp.server import mcp


def main():
    if "EDGAR_TEST_HOOKS" in os.environ:
        print("edgar-search-mcp: EDGAR_TEST_HOOKS is set; it is for the test suite only and the server "
              "does not start with it", file=sys.stderr)
        sys.exit(2)
    problem = ratelimit.config_error()
    if problem:
        print(f"edgar-search-mcp: {problem}", file=sys.stderr)
        sys.exit(2)
    if not USER_AGENT:
        # stdout carries the protocol; the warning goes to stderr. Every tool
        # that needs the SEC answers NO_USER_AGENT until the variable is set.
        print('edgar-search-mcp: EDGAR_USER_AGENT is not set; set it to your name and email, '
              'e.g. "Jane Doe jane@example.com"', file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
