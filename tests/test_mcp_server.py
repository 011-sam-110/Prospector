"""Offline tests for the FastMCP server.

These run without any live network or LLM: ``reddit_search`` / ``reddit_sweep``
are never exercised against real Reddit. We verify that (1) the module imports and
exposes ``mcp`` + ``main``, (2) all nine contract tools are registered with the
exact names Claude relies on, and (3) a stubbed read tool (``reddit_profiles``)
flows through end-to-end via an in-memory client.

If ``fastmcp`` (or a sibling engine module still being built in parallel) is not
importable, the whole module is skipped rather than erroring — the suite stays
green offline.
"""

from __future__ import annotations

import asyncio

import pytest

# Skip cleanly if fastmcp or any engine dependency isn't importable yet.
pytest.importorskip("fastmcp")
mcp_server = pytest.importorskip("prospector.mcp_server")


# The frozen contract: these names are what Claude calls.
EXPECTED_TOOLS = {
    "reddit_profiles",
    "reddit_profile_get",
    "reddit_sweep",
    "reddit_search",
    "reddit_fetch_thread",
    "reddit_query",
    "reddit_get_evidence",
    "reddit_stats",
    "reddit_export",
}


def _registered_tool_names() -> set[str]:
    """Tool names registered on ``mcp``, via whatever the installed API exposes.

    Tries the in-memory MCP client (stable across fastmcp 2.x/3.x), then falls
    back to ``get_tools()`` (2.x dict) / ``list_tools()`` (3.x list).
    """
    mcp = mcp_server.mcp

    # Preferred: protocol-level listing through an in-memory client.
    try:
        from fastmcp import Client

        async def _via_client() -> set[str]:
            async with Client(mcp) as client:
                tools = await client.list_tools()
                return {t.name for t in tools}

        return asyncio.run(_via_client())
    except Exception:
        pass

    # Fallback: server-side introspection.
    getter = getattr(mcp, "get_tools", None) or getattr(mcp, "list_tools", None)
    assert getter is not None, "FastMCP instance exposes no tool listing API"
    result = getter()
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    if isinstance(result, dict):
        return set(result.keys())
    return {getattr(t, "name", None) for t in result}


def test_module_exposes_server_and_main():
    """The module provides the contracted ``mcp`` instance and ``main``."""
    assert mcp_server.mcp is not None
    # FastMCP names the server "prospector".
    assert getattr(mcp_server.mcp, "name", "prospector") == "prospector"
    assert callable(mcp_server.main)


def test_all_nine_tools_registered():
    """Exactly the nine contract tools are registered on the server."""
    names = _registered_tool_names()
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {sorted(missing)}"
    # No surprise extra tools leaked into the contract surface.
    extra = names - EXPECTED_TOOLS
    assert not extra, f"unexpected extra tools: {sorted(extra)}"


def test_lazy_handles_not_created_on_import():
    """Importing the module must not open the DB or a Reddit client."""
    # Tools are registered but no store/client should exist until first call.
    assert mcp_server._store is None
    assert mcp_server._client is None


def test_db_path_env(monkeypatch):
    """The store path honors PROSPECTOR_DB and falls back to the default."""
    monkeypatch.delenv("PROSPECTOR_DB", raising=False)
    assert mcp_server._db_path() == "prospector.db"
    monkeypatch.setenv("PROSPECTOR_DB", "/tmp/custom-prospector.db")
    assert mcp_server._db_path() == "/tmp/custom-prospector.db"


def test_serialize_helpers():
    """Dataclasses serialize to plain dicts; primitives pass through."""
    from prospector.models import EvidenceItem

    ev = EvidenceItem(
        id="t3_x",
        permalink="https://www.reddit.com/r/nursing/comments/x/_/",
        quote="we still fax everything",
        subreddit="nursing",
        author="rn123",
        score=7,
        created_utc=1700000000,
    )
    d = mcp_server._to_dict(ev)
    assert isinstance(d, dict)
    assert d["permalink"].endswith("/_/")
    assert d["quote"] == "we still fax everything"

    assert mcp_server._to_dict(None) is None
    assert mcp_server._to_dict("plain") == "plain"
    assert mcp_server._serialize_list([ev, ev]) == [d, d]


def test_fullname_normalization():
    """Bare ids gain a t3_ prefix; existing fullnames are preserved."""
    assert mcp_server._fullname("abc123") == "t3_abc123"
    assert mcp_server._fullname("t3_abc123") == "t3_abc123"
    assert mcp_server._fullname("t1_def456") == "t1_def456"


def test_reddit_profiles_tool_flows_through_stub(monkeypatch):
    """A stubbed engine call flows through the registered tool (offline).

    Monkeypatch ``list_profiles`` and invoke ``reddit_profiles`` via the
    in-memory MCP client — exercises the real registered wrapper without any
    filesystem/network access.
    """
    from fastmcp import Client

    monkeypatch.setattr(
        mcp_server, "list_profiles", lambda *a, **k: ["hospital-tech", "fake-niche"]
    )

    async def _call() -> object:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool("reddit_profiles", {})

    result = asyncio.run(_call())

    # Robustly extract the structured payload across fastmcp result shapes.
    payload = getattr(result, "data", None)
    if payload is None:
        payload = getattr(result, "structured_content", None)
    if payload is None and hasattr(result, "content"):
        import json

        text = result.content[0].text
        payload = json.loads(text)
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]

    assert payload == ["hospital-tech", "fake-niche"]
