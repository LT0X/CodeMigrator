"""Network-path browser acceptance for the PostgreSQL-backed Run SSE projection.

Run with the project's Python environment and ``CODEMIGRATOR_TEST_PG_DSN`` set.
The test needs the already-installed web node_modules and a local Chromium binary;
it adds no browser automation dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
from starlette.types import ASGIApp, Message, Scope

from codemigrator.api import ApiConfig, create_app
from codemigrator.api.backend import ProductionApiBackend
from codemigrator.api_read_model import RuntimeRunReadModel
from codemigrator.core import RunId, RunStatus
from codemigrator.runtime.cas import FileHostCAS
from codemigrator.runtime.contracts import EventSpec, RunState
from codemigrator.runtime.store import PostgreSQLRuntimeStore

_CHILD_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
)


def child_environment(**extra: str) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in _CHILD_ENVIRONMENT_KEYS if key in os.environ
    }
    environment.update(extra)
    return environment


@asynccontextmanager
async def isolated_store(dsn: str) -> AsyncIterator[PostgreSQLRuntimeStore]:
    schema = f"browser_sse_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool: asyncpg.Pool[asyncpg.Record] | None = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
        store = PostgreSQLRuntimeStore(pool)
        await store.initialize()
        yield store
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


class SseRequestProbe:
    def __init__(self, app: ASGIApp, run_id: str) -> None:
        self._app = app
        self._path = f"/api/v1/migrations/{run_id}/events"
        self.cursors: asyncio.Queue[str] = asyncio.Queue()

    async def __call__(self, scope: Scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and scope["path"] == self._path:
            headers = dict(scope.get("headers", ()))
            self.cursors.put_nowait(headers.get(b"last-event-id", b"").decode("ascii"))
        await self._app(scope, receive, send)


class AsgiHttpServer:
    """Small HTTP/1.1 test host that invokes the real ASGI application."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket_info = self._server.sockets[0].getsockname()
        return int(socket_info[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response_started = False
        try:
            request_line = await reader.readline()
            method, target, protocol = request_line.decode("ascii").strip().split(" ", 2)
            raw_headers: list[tuple[bytes, bytes]] = []
            header_map: dict[str, str] = {}
            while line := await reader.readline():
                if line in {b"\r\n", b"\n"}:
                    break
                name, value = line.decode("latin-1").split(":", 1)
                key = name.strip().lower()
                clean_value = value.strip()
                raw_headers.append((key.encode("ascii"), clean_value.encode("latin-1")))
                header_map[key] = clean_value
            content_length = int(header_map.get("content-length", "0"))
            body = await reader.readexactly(content_length) if content_length else b""
            raw_headers = [
                (key, value) for key, value in raw_headers if key != b"authorization"
            ]
            raw_headers.append((b"authorization", b"Bearer browser-e2e-token"))
            parsed = urlsplit(target)
            path = parsed.path.encode("ascii")
            first_request = True
            receive_lock = asyncio.Lock()

            async def receive() -> Message:
                nonlocal first_request
                async with receive_lock:
                    if first_request:
                        first_request = False
                        return {"type": "http.request", "body": body, "more_body": False}
                    await reader.read(1)
                    return {"type": "http.disconnect"}

            chunked = False

            async def send(message: Message) -> None:
                nonlocal chunked, response_started
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    headers = [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() not in {b"connection", b"transfer-encoding"}
                    ]
                    chunked = not any(key.lower() == b"content-length" for key, _ in headers)
                    if chunked:
                        headers.append((b"transfer-encoding", b"chunked"))
                    headers.append((b"connection", b"close"))
                    reason = HTTPStatus(status).phrase.encode("ascii")
                    writer.write(f"HTTP/1.1 {status} ".encode("ascii") + reason + b"\r\n")
                    for key, value in headers:
                        writer.write(key + b": " + value + b"\r\n")
                    writer.write(b"\r\n")
                    response_started = True
                    await writer.drain()
                    return
                if message["type"] != "http.response.body":
                    return
                data = message.get("body", b"")
                if data:
                    if chunked:
                        writer.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                    else:
                        writer.write(data)
                    await writer.drain()
                if not message.get("more_body", False) and chunked:
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()

            host = header_map.get("host", "127.0.0.1")
            server_name, _, server_port = host.partition(":")
            scope: Scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": protocol.removeprefix("HTTP/"),
                "method": method,
                "scheme": "http",
                "path": parsed.path,
                "raw_path": path,
                "query_string": parsed.query.encode("ascii"),
                "root_path": "",
                "headers": raw_headers,
                "server": (server_name, int(server_port or 80)),
                "client": writer.get_extra_info("peername"),
            }
            await self._app(scope, receive, send)
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
            pass
        except Exception:
            if not response_started:
                writer.write(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                try:
                    await writer.drain()
                except ConnectionError:
                    pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


def find_chromium() -> str | None:
    configured = os.environ.get("CODEMIGRATOR_CHROMIUM_PATH")
    if configured and Path(configured).is_file():
        return configured
    for command in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if executable := shutil.which(command):
            return executable
    candidates = [
        *Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        *Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
    ]
    return str(sorted(candidates)[-1]) if candidates else None


async def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for_http_server(process: asyncio.subprocess.Process, port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise AssertionError("Vite did not start")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            del reader
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise AssertionError("Vite did not become ready")


async def run_browser_driver(
    node: str, driver: Path, *, url: str, run_id: str, chromium: str, sentinels: list[str]
) -> tuple[int, bytes]:
    environment = child_environment(
        CODEMIGRATOR_BROWSER_URL=url,
        CODEMIGRATOR_BROWSER_RUN_ID=run_id,
        CODEMIGRATOR_BROWSER_CHROMIUM=chromium,
        CODEMIGRATOR_BROWSER_SENSITIVE_MARKERS=json.dumps(sentinels),
    )
    process = await asyncio.create_subprocess_exec(
        node,
        str(driver),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        return 124, b""
    except asyncio.CancelledError:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    return process.returncode or 0, stdout


def node_has_builtin_websocket(node: str) -> bool:
    result = shutil.which(node)
    if result is None:
        return False
    probe = os.spawnve(
        os.P_WAIT,
        result,
        [result, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"],
        child_environment(),
    )
    return probe == 0


@pytest.mark.asyncio
async def test_chromium_reads_safe_agent_run_summary_and_replays_postgres_sse(tmp_path: Path):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    chromium = find_chromium()
    node = shutil.which("node")
    vite_script = Path(__file__).parents[2] / "web/node_modules/vite/bin/vite.js"
    driver = Path(__file__).parents[2] / "web/scripts/browser-sse-e2e.mjs"
    if (
        not chromium
        or not node
        or not node_has_builtin_websocket(node)
        or not vite_script.is_file()
        or not driver.is_file()
    ):
        pytest.skip("local Chromium, Node, or web node_modules are unavailable")

    run_id = uuid4()
    agent_run_id = uuid4()
    marker_root = f"synthetic-browser-e2e-{uuid4().hex}"
    sentinels = [f"{marker_root}-untrusted-event-detail"]

    async with isolated_store(dsn) as store:
        cas = FileHostCAS(tmp_path / "cas")
        await store.create(
            RunState(run_id=RunId(run_id), status=RunStatus.Planning),
            (),
        )
        backend = ProductionApiBackend(
            store,
            run_read_projection=RuntimeRunReadModel(store, cas),
        )
        api = create_app(
            backend,
            config=ApiConfig(token="browser-e2e-token", heartbeat_seconds=0.05),
        )
        probe = SseRequestProbe(api, str(run_id))
        api_server = AsgiHttpServer(probe)
        api_port = await api_server.start()
        vite_port = await free_port()
        web_dir = Path(__file__).parents[2] / "web"
        vite_environment = child_environment(
            CODEMIGRATOR_WEB_API_TARGET=f"http://127.0.0.1:{api_port}"
        )
        vite = await asyncio.create_subprocess_exec(
            node,
            str(vite_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(vite_port),
            "--strictPort",
            cwd=web_dir,
            env=vite_environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        browser_task: asyncio.Task[tuple[int, bytes]] | None = None
        try:
            await wait_for_http_server(vite, vite_port)
            browser_task = asyncio.create_task(
                run_browser_driver(
                    node,
                    driver,
                    url=f"http://127.0.0.1:{vite_port}",
                    run_id=str(run_id),
                    chromium=chromium,
                    sentinels=sentinels,
                )
            )
            try:
                first_cursor = await asyncio.wait_for(probe.cursors.get(), timeout=12)
            except TimeoutError:
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
                assert False, "browser did not reach the ASGI SSE route"
            assert first_cursor == "0"

            untrusted_event_detail = {"debug_metadata": sentinels[0]}
            await store.commit(
                RunState(
                    run_id=RunId(run_id),
                    status=RunStatus.Failed,
                    version=1,
                ),
                (
                    EventSpec(
                        "agent_run.started",
                        {
                            "agent_run_id": str(agent_run_id),
                            "phase": "PLAN",
                            "session_kind": "PLAN_AUXILIARY",
                            **untrusted_event_detail,
                        },
                    ),
                    EventSpec(
                        "agent_run.terminal",
                        {
                            "agent_run_id": str(agent_run_id),
                            "phase": "PLAN",
                            "session_kind": "PLAN_AUXILIARY",
                            "exit": "COMPLETED",
                            "receipt_category": "session.terminal",
                            **untrusted_event_detail,
                        },
                    ),
                    EventSpec("run.status_changed", {"run_status": RunStatus.Failed.value}),
                ),
            )

            try:
                driver_code, stdout = await asyncio.wait_for(browser_task, timeout=35)
            except TimeoutError:
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
                assert False, "Chromium did not finish the browser SSE scenario"
            assert driver_code == 0, "Chromium browser SSE scenario failed"
            try:
                summary = json.loads(stdout)
            except (TypeError, ValueError):
                summary = None
            assert isinstance(summary, dict)
            assert summary.get("ui_summary") == "PLAN · PLAN_AUXILIARY · COMPLETED"
            assert summary.get("ui_cursor") == 3
            assert summary.get("sensitive_marker_visible") is False
            assert summary.get("forbidden_field_name_in_full_sse") is False
            assert summary.get("full_envelope_valid") is True
            assert summary.get("full_event_ids") == ["1", "2", "3"]
            assert summary.get("replay_envelope_valid") is True
            assert summary.get("replay_event_ids") == ["2", "3"]
            assert summary.get("replay_event_types") == [
                "agent_run.terminal",
                "run.status_changed",
            ]
            assert summary.get("sensitive_marker_in_sse") is False
            assert summary.get("forbidden_field_name_in_sse") is False
            replay_cursor = await asyncio.wait_for(probe.cursors.get(), timeout=2)
            full_replay_cursor = await asyncio.wait_for(probe.cursors.get(), timeout=2)
            assert [replay_cursor, full_replay_cursor] == ["0", "1"]
        finally:
            if browser_task is not None and not browser_task.done():
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
            vite.terminate()
            try:
                await asyncio.wait_for(vite.wait(), timeout=5)
            except TimeoutError:
                vite.kill()
                await vite.wait()
            await api_server.close()
            await backend.close()
