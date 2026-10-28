"""The errors of every request to the SEC, with the code a tool reports.

They live here, not in sec_client, so the limiter (edgar_mcp.ratelimit) can
raise them without importing the client; sec_client re-exports them all.
"""


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


class SecLimiterUnavailable(SecError):
    """The limiter shared by every process on this machine cannot be used, so
    no request is sent: its state file cannot be opened, locked or read, or
    EDGAR_RATE_LIMIT is not a valid rate."""
    code = "RATE_LIMITER_UNAVAILABLE"
