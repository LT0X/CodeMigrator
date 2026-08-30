from __future__ import annotations

import asyncio

import pytest

from codemigrator.sandbox import DomainAllowlist, ProxyAuditEvent, proxy_environment
from codemigrator.sandbox.proxy import AsyncForwardProxy


def test_proxy_allows_exact_domains_and_subdomains_only() -> None:
    allowlist = DomainAllowlist(("pypi.org", "files.pythonhosted.org"))

    assert allowlist.allows("pypi.org")
    assert allowlist.allows("files.pythonhosted.org")
    assert allowlist.allows("mirror.files.pythonhosted.org")
    assert not allowlist.allows("evilpypi.org")
    assert not allowlist.allows("example.com")


def test_proxy_environment_is_explicit_and_connection_audit_is_structured() -> None:
    environment = proxy_environment("10.0.0.2", 3128)
    event = ProxyAuditEvent(host="pypi.org", port=443, allowed=True)

    assert environment == {
        "HTTP_PROXY": "http://10.0.0.2:3128",
        "HTTPS_PROXY": "http://10.0.0.2:3128",
    }
    assert event.allowed is True
    with pytest.raises(ValueError, match="veth"):
        proxy_environment("127.0.0.1", 3128)


@pytest.mark.asyncio
async def test_async_proxy_forwards_allowed_http_and_audits_destination() -> None:
    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = int(upstream_server.sockets[0].getsockname()[1])
    events: list[ProxyAuditEvent] = []
    proxy = AsyncForwardProxy(
        DomainAllowlist(("127.0.0.1",)),
        audit_sink=events.append,
        allow_private_addresses=True,
    )
    proxy_host, proxy_port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
        writer.write(
            f"GET http://127.0.0.1:{upstream_port}/health HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n\r\n".encode()
        )
        await writer.drain()
        writer.write_eof()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        upstream_server.close()
        await upstream_server.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert events == [ProxyAuditEvent(host="127.0.0.1", port=upstream_port, allowed=True)]
