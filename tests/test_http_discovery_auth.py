"""HTTP transports: OAuth discovery answers 404, the key gate stays 401.

MCP clients read a 401 on ``/.well-known/*`` or ``/register`` as "OAuth
required" and start a login flow this server cannot complete. Every other
endpoint must still refuse a request without the key. The ``?api_key=``
transport must not leak the key into uvicorn's access log.

Each test runs the real router under a real uvicorn on a loopback port.
"""
from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from mcp.server import Server

from src.main import EWSMCPServer

API_KEY = "secret"

DISCOVERY_PATHS = [
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/openid-configuration",
    "/mcp/.well-known/oauth-authorization-server",
    "/mcp/.well-known/oauth-protected-resource",
    "/sse/.well-known/oauth-protected-resource",
]


def _server() -> EWSMCPServer:
    """Only the state the HTTP routers read; no Exchange connection."""
    srv = EWSMCPServer.__new__(EWSMCPServer)
    srv.settings = SimpleNamespace(
        mcp_api_key=API_KEY,
        mcp_host="127.0.0.1",
        mcp_port=0,
        log_level="INFO",
        http_keep_alive_timeout_seconds=5,
        sse_keepalive_enabled=False,
        sse_keepalive_interval_seconds=15,
        tcp_keepalive_enabled=False,
    )
    srv.tools = {}
    srv.server = Server("test")
    srv.logger = logging.getLogger("test_http_discovery_auth")
    return srv


def _exchange(monkeypatch, transport, requests):
    """Serve ``transport``, send ``(method, path)`` requests, return results.

    Returns the status codes and everything uvicorn's access handler wrote.
    """
    started = []
    original_serve = uvicorn.Server.serve

    async def serve(self, sockets=None):
        started.append(self)
        await original_serve(self, sockets)

    monkeypatch.setattr(uvicorn.Server, "serve", serve)

    async def scenario():
        srv = _server()
        run = srv.run_sse if transport == "sse" else srv.run_streamable_http
        task = asyncio.create_task(run())
        for _ in range(200):
            if started and started[0].started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
        server = started[0]
        access_log = io.StringIO()
        for handler in logging.getLogger("uvicorn.access").handlers:
            handler.setStream(access_log)
        port = server.servers[0].sockets[0].getsockname()[1]
        statuses = []
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10,
            ) as client:
                for method, path in requests:
                    response = await client.request(method, path)
                    statuses.append((method, path, response.status_code))
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=15)
        return statuses, access_log.getvalue()

    return asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_discovery_and_register_return_404_without_a_key(monkeypatch, transport):
    requests = [("GET", p) for p in DISCOVERY_PATHS] + [
        ("POST", "/register"),
        ("GET", "/register"),
    ]
    statuses, _ = _exchange(monkeypatch, transport, requests)
    assert [s for s in statuses if s[2] != 404] == []


def test_streamable_http_mcp_still_requires_the_key(monkeypatch):
    statuses, _ = _exchange(monkeypatch, "streamable-http", [
        ("GET", "/mcp"),
        ("POST", "/mcp"),
        ("POST", "/mcp/"),
        ("POST", "/mcp/register"),
    ])
    assert [s[2] for s in statuses] == [401, 401, 401, 401]


def test_sse_endpoints_still_require_the_key(monkeypatch):
    statuses, _ = _exchange(monkeypatch, "sse", [
        ("GET", "/sse"),
        ("POST", "/messages"),
        ("GET", "/openapi.json"),
        ("POST", "/api/tools/send_email"),
        ("GET", "/mcp"),
        ("POST", "/mcp"),
    ])
    assert [s[2] for s in statuses] == [401] * 6


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_access_log_redacts_the_query_key(monkeypatch, transport):
    statuses, log = _exchange(monkeypatch, transport, [
        ("GET", f"/health?api_key={API_KEY}"),
        ("GET", f"/nowhere?x=1&api_key={API_KEY}"),
        ("GET", f"/nowhere?api%5Fkey={API_KEY}"),
    ])
    assert [s[2] for s in statuses] == [200, 404, 404]
    # The lines were logged, with the key masked.
    assert "GET /health?api_key=[redacted]" in log
    assert "GET /nowhere?x=1&api_key=[redacted]" in log
    assert API_KEY not in log
