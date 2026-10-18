"""Helper functions for the EDGAR Financial Tool."""
from __future__ import annotations

import re
import logging
from datetime import date

logger = logging.getLogger(__name__)


def iso_date(value):
    """Check a YYYY-MM-DD date and return it unchanged.

    Dates are compared as text throughout, which is only correct for this
    exact form, so anything else ("2023-6-30", "06/30/2023") is refused.
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a calendar date") from None
    return value
