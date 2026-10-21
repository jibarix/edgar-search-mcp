"""Start the MCP server over stdio and call it, offline, from the frozen SEC answers.

Run from the repository root (no network):
    python -X utf8 tests/test_mcp_stdio.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = os.path.join(ROOT, "tests", "fixtures", "sec")
sys.path.insert(0, ROOT)

from edgar_mcp import __version__  # noqa: E402

with open(os.path.join(ROOT, "tests", "fixtures", "outputs_0.8.0", "_about.json"), encoding="utf-8") as f:
    ABOUT = json.load(f)


def payload(result):
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) else data
    return json.loads(result.content[0].text)


def environment(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("EDGAR_")}
    env["EDGAR_CACHE_DIR"] = tempfile.mkdtemp(prefix="edgar_cache_test_")
    return {**env, **extra}


async def main():
    problems = []

    def check(label, ok, detail=""):
        print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
        if not ok:
            problems.append(label)

    # Frozen copies only: the server cannot reach the network.
    server = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT,
                                   env=environment(EDGAR_FROZEN_DIR=SEC))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            started = await session.initialize()
            check("server reports its version", started.server_info.version == __version__,
                  f"{started.server_info.name} {started.server_info.version}")

            async def refused(label, code, tool, **arguments):
                result = await session.call_tool(tool, arguments)
                text = result.content[0].text if result.content else ""
                check(label, result.is_error and code in text, text[:110])

            tools = (await session.list_tools()).tools
            sys.path.insert(0, ROOT)
            from edgar_mcp import server as module
            expected = sorted(t.name for t in await module.mcp.list_tools())
            names = sorted(t.name for t in tools)
            check("the tools listed over stdio are the server's tools", names == expected, str(names))
            check("tools are marked read-only", all(t.annotations and t.annotations.read_only_hint for t in tools))

            found = payload(await session.call_tool("lookup_company", {"query": "WINA"}))
            check("lookup_company answers from the frozen ticker file",
                  any(m.get("cik") == "0000908315" for m in found["matches"]), str(found["matches"])[:120])
            text = payload(await session.call_tool("get_filing_text", {"url": ABOUT["exhibit"], "max_chars": 300}))
            check("get_filing_text answers from a frozen copy", bool(text.get("text")), repr(text.get("text", ""))[:80])
            await refused("an address with no frozen copy is refused", "NO_FROZEN_COPY", "get_filing_text",
                          url="https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/nosuchfile.htm")
            await refused("get_filing_text refuses other hosts", "INVALID_INPUT",
                          "get_filing_text", url="https://example.com/x.htm")
            await refused("get_filing_text refuses a path out of the archive", "INVALID_INPUT", "get_filing_text",
                          url="https://www.sec.gov/Archives/edgar/data/../../../../outside.htm")
            await refused("list_filing_documents refuses other hosts", "INVALID_INPUT",
                          "list_filing_documents", url="https://example.com/Archives/edgar/data/1/000000000000000000/x.htm")
            await refused("list_filings bounds limit", "INVALID_INPUT", "list_filings",
                          cik_or_ticker="908315", limit=0)

    # A server started without EDGAR_USER_AGENT sends nothing to the SEC.
    server = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "edgar_mcp"], cwd=ROOT,
                                   env=environment())
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_filings", {"cik_or_ticker": "0000320193"})
            text = result.content[0].text if result.content else ""
            check("without EDGAR_USER_AGENT the SEC is not called", result.is_error and "NO_USER_AGENT" in text,
                  text[:110])

    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
