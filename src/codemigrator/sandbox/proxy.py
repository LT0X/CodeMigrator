"""Small, auditable HTTP(S) forward proxy for the Shell network profile."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from codemigrator.core._base import CoreModel


class DomainAllowlist:
    """Exact-domain and strict-subdomain matcher with normalized DNS names."""

    def __init__(self, domains: Iterable[str]) -> None:
        normalized = tuple(self._normalize(domain) for domain in domains)
        if not normalized:
            raise ValueError("domain allowlist must not be empty")
        if any(not domain for domain in normalized):
            raise ValueError("domain allowlist contains an empty domain")
        self._domains = frozenset(normalized)

    @staticmethod
    def _normalize(domain: str) -> str:
        value = domain.strip().lower().rstrip(".")
        if not value or any(char.isspace() for char in value) or "/" in value:
            raise ValueError("allowlist entries must be DNS names")
        return value

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted(self._domains))

    def allows(self, host: str) -> bool:
        normalized = self._normalize(host)
        return any(
            normalized == domain or normalized.endswith("." + domain)
            for domain in self._domains
        )


class ProxyAuditEvent(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    port: int = Field(ge=1, le=65535)
    allowed: bool

    @field_validator("host")
    @classmethod
    def host_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host must not be empty")
        return value.strip().lower().rstrip(".")


def proxy_environment(host: str, port: int) -> dict[str, str]:
    """Return only the standard proxy variables injected into Shell sandboxes."""

    if not host or not 1 <= port <= 65535:
        raise ValueError("proxy host and port are invalid")
    if host.strip().lower() == "localhost":
        raise ValueError("proxy host must address the veth endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        formatted_host = host
    else:
        if address.is_loopback or address.is_unspecified:
            raise ValueError("proxy host must address the veth endpoint")
        formatted_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    endpoint = f"http://{formatted_host}:{port}"
    return {"HTTP_PROXY": endpoint, "HTTPS_PROXY": endpoint}


@dataclass(frozen=True)
class _ProxyTarget:
    host: str
    port: int


class AsyncForwardProxy:
    """A minimal CONNECT/absolute-URI proxy with domain-level authorization.

    The proxy intentionally has no CONNECT tunnelling to an unauthorized host and
    emits an audit event before every attempted upstream connection.
    """

    def __init__(
        self,
        allowlist: DomainAllowlist,
        *,
        audit_sink: Callable[[ProxyAuditEvent], Awaitable[None] | None] | None = None,
        allow_private_addresses: bool = False,
    ) -> None:
        self._allowlist = allowlist
        self._audit_sink = audit_sink
        self._allow_private_addresses = allow_private_addresses
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        if self._server is not None:
            raise RuntimeError("proxy is already running")
        self._server = await asyncio.start_server(self._handle_client, host, port)
        socket = self._server.sockets
        if not socket:
            raise RuntimeError("proxy did not bind a socket")
        address = socket[0].getsockname()
        return str(address[0]), int(address[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(*(writer.wait_closed() for writer in writers))
        self._writers.clear()

    async def _audit(self, event: ProxyAuditEvent) -> None:
        if self._audit_sink is None:
            return
        result = self._audit_sink(event)
        if result is not None:
            await result

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            method, target, version = request.decode("latin-1").strip().split(" ", 2)
            headers: list[str] = []
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=5)
                if header in (b"\r\n", b"\n", b""):
                    break
                headers.append(header.decode("latin-1").rstrip("\r\n"))
            parsed = self._parse_target(method, target)
            allowed = self._allowlist.allows(parsed.host)
            await self._audit(ProxyAuditEvent(host=parsed.host, port=parsed.port, allowed=allowed))
            if not allowed:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            upstream_reader, upstream_writer = await self._open_upstream(parsed)
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                parsed_target = urlsplit(target)
                if parsed_target.scheme == "https":
                    raise ValueError("HTTPS proxying requires CONNECT")
                path = parsed_target.path or "/"
                if parsed_target.query:
                    path += "?" + parsed_target.query
                forwarded = [f"{method} {path} {version}"]
                forwarded.extend(
                    header for header in headers if not header.lower().startswith("proxy-")
                )
                upstream_writer.write(
                    ("\r\n".join(forwarded) + "\r\n\r\n").encode("latin-1")
                )
                await upstream_writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer)
            )
        except (ValueError, UnicodeError, TimeoutError, OSError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()
            self._writers.discard(writer)

    async def _open_upstream(
        self, target: _ProxyTarget
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            target.host, target.port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        for _, _, _, _, sockaddr in infos:
            address = ipaddress.ip_address(sockaddr[0])
            if not self._allow_private_addresses and (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_unspecified
            ):
                continue
            try:
                return await asyncio.open_connection(sockaddr[0], target.port)
            except OSError:
                continue
        raise OSError("destination has no permitted reachable IPv4 address")

    @staticmethod
    def _parse_target(method: str, target: str) -> _ProxyTarget:
        if method.upper() == "CONNECT":
            host, separator, port_text = target.rpartition(":")
            if not separator or not host:
                raise ValueError("CONNECT requires host:port")
            port = int(port_text)
            return _ProxyTarget(host, port)
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("proxy request must use an absolute HTTP(S) URI")
        return _ProxyTarget(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
