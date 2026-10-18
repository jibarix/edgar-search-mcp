"""
Settings for the EDGAR Financial Tool.
"""

import os
from pathlib import Path

# Project base directory
BASE_DIR = Path(__file__).resolve().parent.parent

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

# Cache settings. EDGAR_CACHE_DIR moves the cache out of the source tree.
# While copies are served or recorded the cache is off, so every answer
# comes from the copies and every response is recorded.
CACHE_DIR = os.environ.get("EDGAR_CACHE_DIR") or os.path.join(BASE_DIR, "cache")
CACHE_ENABLED = FROZEN_DIR is None and RECORD_DIR is None
CACHE_EXPIRY = 24 * 60 * 60  # 24 hours in seconds

# Directories are created by the code that writes to them, not on import.