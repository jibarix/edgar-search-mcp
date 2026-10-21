"""The envelope every 1.0 answer carries (docs/RESPONSE_CONTRACT.md).

Nothing here looks at what the data means: it records where the data came
from, when, and in which representation it is returned.
"""
from __future__ import annotations

from datetime import datetime, timezone

CONTRACT_VERSION = "1.0"
REPRESENTATIONS = ("source", "source_records", "parsed_source", "transformed")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source(meta: dict) -> dict:
    """One entry of `sources`, from the meta that sec_client.get_json (or a
    caller holding a Response) returns."""
    return {
        "url": meta["source_url"],
        "retrieved_at": meta["retrieved_at"],
        "frozen": bool(meta.get("frozen", False)),
        "cache_hit": bool(meta.get("cache_hit", False)),
        "sha256": meta.get("sha256"),
    }


def source_of_response(url: str, response, sha256: str | None, cache_hit: bool = False) -> dict:
    """One entry of `sources`, for a sec_client.Response."""
    return {"url": url, "retrieved_at": response.retrieved_at, "frozen": bool(response.frozen),
            "cache_hit": cache_hit, "sha256": sha256}


def envelope(representation: str, sources: list, **fields) -> dict:
    """The answer: the envelope first, then the tool's own fields."""
    if representation not in REPRESENTATIONS:
        raise AssertionError(f"unknown representation {representation!r}")
    if not sources:
        raise AssertionError("an answer names at least one source")
    return {"contract_version": CONTRACT_VERSION, "representation": representation,
            "sources": sources, **fields, "returned_at": now()}
