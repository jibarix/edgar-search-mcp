"""The envelope every 1.0 answer carries (docs/RESPONSE_CONTRACT.md), and
each tool's answer schema, from response_contract.schema.json.

Nothing here looks at what the data means: it records where the data came
from, when, and in which representation it is returned.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from importlib import resources

CONTRACT_VERSION = "1.0"
SCHEMA = json.loads(resources.files("edgar_mcp").joinpath("response_contract.schema.json").read_text("utf-8"))
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


def _refs(node) -> list:
    """Every $ref in a schema."""
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def tool_schema(name: str) -> dict:
    """The answer schema of one tool, self-contained: the tool's definition
    with every $defs entry it reaches bundled in, so a client resolves no
    reference outside it."""
    defs = SCHEMA["$defs"]
    needed, todo = {}, _refs(defs[name])
    while todo:
        ref = todo.pop()
        key = ref.removeprefix("#/$defs/")
        if ref != "#/$defs/" + key or key not in defs:
            raise AssertionError(f"{name}: the contract reference {ref!r} is not a local definition")
        if key not in needed:
            needed[key] = defs[key]
            todo += _refs(defs[key])
    return copy.deepcopy({"$schema": SCHEMA["$schema"], "$id": f"edgar://contract/{CONTRACT_VERSION}/{name}", "type": "object",
                          **defs[name], "$defs": dict(sorted(needed.items()))})
