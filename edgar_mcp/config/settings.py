"""
Settings for the EDGAR Financial Tool.
"""

import os
import sys
from pathlib import Path

APP_NAME = "edgar-search-mcp"


def _user_dir(kind):
    """The per-user folder for `kind` ("cache" or "data"), outside the
    installed package: an installed copy may not write next to its code, and
    clearing the cache should not remove files kept on purpose."""
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base and os.path.isabs(base) else home / "AppData" / "Local"
        return root / APP_NAME / ("Cache" if kind == "cache" else "Data")
    if sys.platform == "darwin":
        return home / "Library" / ("Caches" if kind == "cache" else "Application Support") / APP_NAME
    variable, fallback = (("XDG_CACHE_HOME", home / ".cache") if kind == "cache"
                          else ("XDG_DATA_HOME", home / ".local" / "share"))
    base = os.environ.get(variable)
    # The XDG rules say a relative value is to be ignored.
    return (Path(base) if base and os.path.isabs(base) else fallback) / APP_NAME


# API settings
API_REQUEST_TIMEOUT = 30  # seconds
API_RETRY_COUNT = 3
API_RETRY_DELAY = 2  # seconds

# Rate limiting to comply with SEC guidelines
# https://www.sec.gov/os/accessing-edgar-data
# The SEC ceiling is 10 a second per user across all machines; stay under it.
RATE_LIMIT_REQUESTS_PER_SECOND = 8

# Frozen copies, for tests. EDGAR_FROZEN_DIR serves saved responses only and
# blocks the network; EDGAR_RECORD_DIR saves every live response there.
FROZEN_DIR = os.environ.get("EDGAR_FROZEN_DIR") or None
RECORD_DIR = os.environ.get("EDGAR_RECORD_DIR") or None

# Cache settings: EDGAR_CACHE_DIR, else the user's cache folder. While
# copies are served or recorded the cache is off, so every answer comes from
# the copies and every response is recorded.
CACHE_DIR = os.environ.get("EDGAR_CACHE_DIR") or str(_user_dir("cache"))

CACHE_ENABLED = FROZEN_DIR is None and RECORD_DIR is None
CACHE_EXPIRY = 24 * 60 * 60  # 24 hours in seconds

# Directories are created by the code that writes to them, not on import.