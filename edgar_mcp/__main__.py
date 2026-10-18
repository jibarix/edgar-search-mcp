import sys

from config.constants import USER_AGENT
from edgar_mcp.server import mcp


def main():
    if not USER_AGENT:
        # stdout carries the protocol; the warning goes to stderr. Every tool
        # that needs the SEC answers NO_USER_AGENT until the variable is set.
        print('edgar-search-mcp: EDGAR_USER_AGENT is not set; set it to your name and email, '
              'e.g. "Jane Doe jane@example.com"', file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
