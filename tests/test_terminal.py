"""A sandbox's terminal: a ticket asked for over HTTP, then a WebSocket opened with it as a subprotocol.

The HTTP answers are scripted; the WebSocket is a real one, to a stand-in for the API's terminal
on this computer that echoes what is typed, says a resize as "size CxR", ends with "exit N", and
closes with 4003 on "revoke". The API's own side is tried in the agental repository's
test/terminal.test.ts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import Server, ServerConnection, serve
from websockets.typing import Subprotocol

from agental import Agental, AgentalConnectionError, AgentalError, AsyncAgental, TerminalExit
from agental import _core

ID = "sbx_0123456789abcdef"
TICKET = "t" * 43
PATH = f"/v1/sandboxes/{ID}/terminal"


class StandIn:
    """The API's terminal on a port of this computer: what each handshake offered, and what each terminal was sent."""

    def __init__(self) -> None:
        self.handshakes: list[tuple[str, Headers]] = []
        self.received: list[str | bytes] = []
        self.refuse = False
        self.redirect: str | None = None
        self.answer: str | None = "agental.terminal.v1"
        self.server: Server = serve(
            self.terminal,
            "127.0.0.1",
            0,
            select_subprotocol=self.select,
            process_request=self.check,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.socket.getsockname()[1]}"

    def check(self, connection: ServerConnection, request: Request) -> Response | None:
        self.handshakes.append((request.path, request.headers))
        if self.redirect:
            moved = connection.respond(301, "")
            moved.headers["Location"] = self.redirect
            return moved
        if self.refuse:
            return connection.respond(401, json.dumps({"error": {"code": "unauthorized", "message": "The ticket is used or expired."}}))
        return None

    def select(self, connection: ServerConnection, offered: list[Subprotocol]) -> Subprotocol | None:
        return Subprotocol(self.answer) if self.answer else None

    def terminal(self, connection: ServerConnection) -> None:
        connection.send(json.dumps({"type": "started"}))
        for message in connection:
            self.received.append(message)
            if isinstance(message, str):
                said = json.loads(message)
                connection.send(f"size {said['cols']}x{said['rows']}".encode())
            elif message.startswith(b"exit "):
                connection.send(json.dumps({"type": "exit", "code": int(message[5:]), "signal": 0, "timedOut": False}))
                connection.close(1000, "the shell ended")
            elif message == b"revoke":
                connection.send(json.dumps({"type": "error", "code": "access_ended", "message": "The key was revoked."}))
                connection.close(4003, "access ended")
            else:
                connection.send(message)


@pytest.fixture
def stand_in() -> Iterator[StandIn]:
    one = StandIn()
    yield one
    one.server.shutdown()


class Tickets:
    """The API's HTTP side: a sandbox, and a ticket for each POST."""

    def __init__(self, path: str = PATH) -> None:
        self.posted: list[httpx.Request] = []
        self.path = path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": ID, "name": "agent-1", "status": "running"})
        self.posted.append(request)
        return httpx.Response(201, json={"ticket": TICKET, "expiresAt": "2026-11-12T00:00:30.000Z", "path": self.path, "protocols": ["agental.terminal.v1", f"ticket.{TICKET}"]})


def client(base_url: str, tickets: Tickets) -> Agental:
    return Agental("key_test", base_url=base_url, retry_delay=0, http_client=httpx.Client(transport=httpx.MockTransport(tickets)))


def test_a_terminal_types_resizes_and_ends_with_the_shells_exit(stand_in: StandIn) -> None:
    tickets = Tickets()
    sandbox = client(stand_in.base_url, tickets).sandboxes.get(ID)
    with sandbox.terminal(cols=120, rows=32) as terminal:
        terminal.write("echo hi\n")
        terminal.resize(132, 40)
        terminal.write(b"exit 3")
        out = b"".join(terminal)
        assert terminal.started
    asked = json.loads(tickets.posted[0].content)
    assert {"cols": asked["cols"], "rows": asked["rows"]} == {"cols": 120, "rows": 32}
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", asked["challenge"])
    assert tickets.posted[0].url.path == PATH
    assert tickets.posted[0].headers["authorization"] == "Bearer key_test"
    assert out == b"echo hi\nsize 132x40"
    assert terminal.exit == TerminalExit(exit_code=3, signal=0, timed_out=False, close_code=1000, reason="the shell ended")
    assert stand_in.received == [b"echo hi\n", json.dumps({"type": "resize", "cols": 132, "rows": 40}), b"exit 3"]


def test_the_ticket_goes_only_as_a_subprotocol(stand_in: StandIn) -> None:
    tickets = Tickets()
    sandbox = client(stand_in.base_url, tickets).sandboxes.get(ID)
    with sandbox.terminal() as terminal:
        terminal.write("exit 0")
        assert terminal.wait().exit_code == 0
    path, headers = stand_in.handshakes[0]
    assert path == PATH
    assert TICKET not in path
    # The ticket, then the verifier of the challenge it was asked with (RFC 7636, S256): no answer carries the verifier.
    terminal_protocol, ticket, verifier = headers["Sec-WebSocket-Protocol"].split(", ")
    assert (terminal_protocol, ticket) == ("agental.terminal.v1", f"ticket.{TICKET}")
    assert verifier.startswith("verifier.")
    challenge = json.loads(tickets.posted[0].content)["challenge"]
    digest = hashlib.sha256(verifier.removeprefix("verifier.").encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    assert "Origin" not in headers
    assert headers["User-Agent"].startswith("agental-python/")


def test_input_longer_than_64_kib_goes_in_pieces(stand_in: StandIn) -> None:
    sandbox = client(stand_in.base_url, Tickets()).sandboxes.get(ID)
    with sandbox.terminal() as terminal:
        terminal.write(b"x" * (64 * 1024 * 2 + 5))
        terminal.write("exit 0")
        terminal.wait()
    assert [len(piece) for piece in stand_in.received] == [65536, 65536, 5, 6]


def test_a_terminal_the_api_closes_says_why(stand_in: StandIn) -> None:
    sandbox = client(stand_in.base_url, Tickets()).sandboxes.get(ID)
    with sandbox.terminal() as terminal:
        terminal.write("revoke")
        ended = terminal.wait()
    assert (ended.exit_code, ended.close_code, ended.error_code, ended.error_message) == (None, 4003, "access_ended", "The key was revoked.")
    with pytest.raises(AgentalConnectionError, match="has ended"):
        terminal.write("more")


def test_a_refused_handshake_says_why_without_the_ticket(stand_in: StandIn) -> None:
    stand_in.refuse = True
    sandbox = client(stand_in.base_url, Tickets()).sandboxes.get(ID)
    with pytest.raises(AgentalConnectionError) as raised:
        sandbox.terminal()
    assert "HTTP 401: The ticket is used or expired." in str(raised.value)
    assert TICKET not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_an_answer_without_the_terminals_protocol_is_refused(stand_in: StandIn) -> None:
    stand_in.answer = None
    sandbox = client(stand_in.base_url, Tickets()).sandboxes.get(ID)
    with pytest.raises(AgentalConnectionError):
        sandbox.terminal()


def test_the_size_is_checked_before_a_ticket_is_asked_for() -> None:
    tickets = Tickets()
    sandbox = client("https://api.test", tickets).sandboxes.get(ID)
    for size in ({"cols": 0}, {"rows": 1001}, {"cols": True}):
        with pytest.raises(AgentalError, match="whole number from 1 to 1000"):
            sandbox.terminal(**size)
    assert tickets.posted == []


@pytest.mark.parametrize("path", ["//evil.test/x", "@evil.test/x", "https://evil.test/x", "/v1/a b", "/v1\\x", ""])
def test_no_path_the_api_gives_can_leave_its_address(path: str) -> None:
    with pytest.raises(AgentalError, match="not one on its own address"):
        _core.websocket_url("https://agental.io", path)


def test_the_websocket_is_on_the_apis_own_address() -> None:
    assert _core.websocket_url("https://agental.io", PATH) == f"wss://agental.io{PATH}"
    assert _core.websocket_url("http://127.0.0.1:8741/api/", PATH) == f"ws://127.0.0.1:8741/api{PATH}"


def test_the_async_terminal_does_the_same(stand_in: StandIn) -> None:
    async def main() -> tuple[bytes, Any]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(Tickets()))
        async with AsyncAgental("key_test", base_url=stand_in.base_url, retry_delay=0, http_client=client) as agental:
            sandbox = await agental.sandboxes.get(ID)
            async with await sandbox.terminal() as terminal:
                await terminal.write("hello\n")
                await terminal.resize(100, 30)
                await terminal.write("exit 7")
                out = b""
                async for chunk in terminal:
                    out += chunk
                return out, terminal.exit

    out, ended = asyncio.run(main())
    assert out == b"hello\nsize 100x30"
    assert ended.exit_code == 7 and ended.close_code == 1000


def test_no_redirect_is_followed_which_would_carry_the_ticket_elsewhere(stand_in: StandIn) -> None:
    elsewhere = StandIn()
    try:
        stand_in.redirect = f"ws://127.0.0.1:{elsewhere.server.socket.getsockname()[1]}{PATH}"
        with pytest.raises(AgentalConnectionError, match="HTTP 301"):
            client(stand_in.base_url, Tickets()).sandboxes.get(ID).terminal()

        async def main() -> None:
            http = httpx.AsyncClient(transport=httpx.MockTransport(Tickets()))
            async with AsyncAgental("key_test", base_url=stand_in.base_url, retry_delay=0, http_client=http) as agental:
                await (await agental.sandboxes.get(ID)).terminal()

        with pytest.raises(AgentalConnectionError, match="HTTP 301"):
            asyncio.run(main())
        assert elsewhere.handshakes == []
    finally:
        elsewhere.server.shutdown()


def test_an_exit_the_guest_garbles_ends_it_without_an_error() -> None:
    state = _core.TerminalState()
    state.said(json.dumps({"type": "exit", "code": None, "signal": "x"}))
    ended = state.ended(1000, "the shell ended")
    assert (ended.exit_code, ended.signal) == (None, 0)
