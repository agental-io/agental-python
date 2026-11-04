"""The client against answers that are scripted: what it sends, and what it does about what comes back."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from agental import (
    BURST_REFUSALS,
    Agental,
    AgentalConnectionError,
    AgentalError,
    ApiError,
    AsyncAgental,
    ExecEvent,
    HiddenVariable,
    PlainVariable,
    ProxyError,
    SendEmailAttachment,
    verify_webhook,
)
from agental import __version__ as agental_version

ID = "sbx_0123456789abcdef"


def sandbox(**changes: Any) -> dict[str, Any]:
    return {
        "id": ID,
        "name": "agent-1",
        "region": "main",
        "size": "small",
        "status": "running",
        "hostId": None,
        "vmIsolation": True,
        "resources": {"vcpu": 1, "memoryMb": 2048, "diskGb": 10},
        "error": None,
        "createdAt": "2026-10-13T00:00:00.000Z",
        "updatedAt": "2026-10-13T00:00:00.000Z",
        "stoppedAt": None,
        "ttlSeconds": None,
        "autoStopAt": None,
        "subdomain": None,
        "shared": False,
        "templateId": "tpl_0000000000000001",
        "organizationId": "org_local",
        "createdBy": None,
        "archived": False,
        "archivedAt": None,
        "migratedAt": None,
        "network": {"outbound": "open", "allow": []},
        # The API may say more than this SDK knows of.
        "somethingNew": True,
        **changes,
    }


def answer(status: int, body: Any = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers) if body is not None else httpx.Response(status, headers=headers)


def refusal(status: int, code: str) -> httpx.Response:
    return answer(status, {"error": {"code": code, "message": f"{code} it is"}})


class Script:
    """Answers requests in turn with what it was given, and keeps them to be looked at."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if not self.answers:
            raise AssertionError(f"nothing was scripted for request {len(self.sent)}")
        next_answer = self.answers.pop(0)
        if isinstance(next_answer, Exception):
            raise next_answer
        return next_answer

    def body(self, index: int) -> Any:
        return json.loads(self.sent[index].content)


def scripted(*answers: httpx.Response | Exception) -> tuple[Agental, Script]:
    script = Script(*answers)
    client = Agental("key_test", base_url="https://api.test/", retry_delay=0, poll_interval=0, http_client=httpx.Client(transport=httpx.MockTransport(script)))
    return client, script


def test_a_stop_sends_no_body_and_keep_memory_when_it_is_asked_for() -> None:
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, sandbox(status="stopped", memoryKept=False)),
        answer(200, sandbox(status="stopped", memoryKept=True)),
    )
    box = agental.sandboxes.get(ID)
    box.stop()
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("POST", f"/v1/sandboxes/{ID}/stop", b"")
    assert box.info.memory_kept is False
    box.stop(keep_memory=True)
    assert script.body(2) == {"keepMemory": True}
    assert box.info.memory_kept is True


def test_a_stop_that_keeps_memory_and_is_answered_202_reads_the_sandbox_until_it_has_stopped() -> None:
    op = {"id": "op_1", "type": "stop", "status": "pending", "sandboxId": ID, "error": None, "createdAt": "2026-10-13T00:00:00.000Z", "updatedAt": "2026-10-13T00:00:00.000Z"}
    agental, script = scripted(
        answer(200, sandbox()),
        answer(202, sandbox(), **{"operation-id": "op_1"}),
        answer(200, {"operation": op, "sandbox": sandbox()}),
        answer(200, sandbox(status="stopped", memoryKept=True)),
    )
    box = agental.sandboxes.get(ID)
    box.stop(keep_memory=True)
    assert (box.status, box.info.memory_kept) == ("stopped", True)
    assert [(one.method, one.url.path) for one in script.sent] == [
        ("GET", f"/v1/sandboxes/{ID}"),
        ("POST", f"/v1/sandboxes/{ID}/stop"),
        ("GET", "/v1/operations/op_1"),
        ("GET", f"/v1/sandboxes/{ID}"),
    ]


def test_a_create_sends_a_key_and_a_name_of_its_own() -> None:
    agental, script = scripted(answer(201, sandbox()), answer(201, sandbox()))
    made = agental.sandboxes.create(size="medium")
    assert (made.id, made.status, made.info.resources.memory_mb) == (ID, "running", 2048)
    first = script.sent[0]
    assert (first.method, first.url.path) == ("POST", "/v1/sandboxes")
    assert first.headers["authorization"] == "Bearer key_test"
    assert first.headers["idempotency-key"].startswith("sdk-")
    assert script.body(0)["name"].startswith("sbx-")
    assert script.body(0)["size"] == "medium"

    agental.sandboxes.create("mine", idempotency_key="task-0001")
    assert script.sent[1].headers["idempotency-key"] == "task-0001"
    assert script.body(1) == {"name": "mine"}


def test_every_request_says_it_is_sent_by_this_sdk_and_which_version() -> None:
    agental, script = scripted(answer(200, {"regions": []}), answer(200, {"regions": []}))
    agental.regions()
    agental.regions()
    assert [each.headers["user-agent"] for each in script.sent] == [f"agental-python/{agental_version}"] * 2

    waiting = Script(answer(200, {"regions": []}))

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(waiting))
        async with AsyncAgental("key_test", base_url="https://api.test", http_client=client) as agental:
            await agental.regions()

    asyncio.run(main())
    assert waiting.sent[0].headers["user-agent"] == f"agental-python/{agental_version}"


def detail(own: list[str]) -> dict[str, Any]:
    """What a sandbox on an allowlist template that lets out to 203.0.113.7 is held to, with these addresses of its own."""
    return {
        "outbound": "allowlist",
        "allow": ["203.0.113.7", *own],
        "template": {"templateId": "tpl_0000000000000002", "name": "coding-agent", "outbound": "allowlist", "allow": ["203.0.113.7"]},
        "own": {"allow": own},
    }


def test_a_sandbox_is_made_with_addresses_of_its_own_and_what_it_may_reach_is_read_and_its_own_replaced_by_calls_of_their_own() -> None:
    agental, script = scripted(
        answer(201, sandbox(templateId="tpl_0000000000000002", network={"outbound": "allowlist", "allow": ["203.0.113.7", "192.0.2.99"]})),
        answer(200, detail(["192.0.2.99"])),
        refusal(409, "sandbox_migrating"),
        answer(200, detail(["198.51.100.0/24"])),
        answer(200, detail([])),
        refusal(501, "network_policy_unavailable"),
    )
    # Only its own addresses are sent with a create: the rule is its template's.
    made = agental.sandboxes.create("held", template="tpl_0000000000000002", allow=["192.0.2.99"])
    assert script.body(0) == {"name": "held", "templateId": "tpl_0000000000000002", "network": {"allow": ["192.0.2.99"]}}
    assert (made.info.network.outbound, made.info.network.allow) == ("allowlist", ["203.0.113.7", "192.0.2.99"])

    # What it is held to, and what that comes from.
    read = made.network()
    assert (read.outbound, read.allow, read.own.allow) == ("allowlist", ["203.0.113.7", "192.0.2.99"], ["192.0.2.99"])
    assert (read.template.template_id, read.template.name, read.template.outbound, read.template.allow) == ("tpl_0000000000000002", "coding-agent", "allowlist", ["203.0.113.7"])
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("GET", f"/v1/sandboxes/{ID}/network", b"")

    # Its own addresses are sent, in place of the ones it had. A sandbox that is being moved to another server cannot be
    # told them, and they are sent again once it has been: sending them twice does what sending them once does.
    assert made.set_network(allow=["198.51.100.0/24"]).own.allow == ["198.51.100.0/24"]
    assert (script.sent[2].method, script.sent[2].url.path, script.body(2)) == ("PUT", f"/v1/sandboxes/{ID}/network", {"allow": ["198.51.100.0/24"]})
    assert (script.sent[3].method, script.body(3)) == ("PUT", {"allow": ["198.51.100.0/24"]})
    # What the sandbox says of itself follows, without another read: the rule it is held to, not what it comes from.
    assert (made.info.network.outbound, made.info.network.allow) == ("allowlist", ["203.0.113.7", "198.51.100.0/24"])

    # An empty list takes them all away, and the template's rule is left.
    assert made.set_network(allow=[]).allow == ["203.0.113.7"]
    assert script.body(4) == {"allow": []}

    # A rule is the template's: a sandbox takes addresses only.
    with pytest.raises(TypeError):
        made.set_network("blocked")  # type: ignore[call-arg, arg-type]
    # Where sandboxes are not held to rules the API says so, and what the sandbox says is left as it was.
    with pytest.raises(ApiError) as refused:
        made.set_network(allow=["192.0.2.99"])
    assert (refused.value.status, refused.value.code) == (501, "network_policy_unavailable")
    assert made.info.network.allow == ["203.0.113.7"]
    assert len(script.sent) == 6


def test_a_create_is_sent_again_with_the_same_key_until_the_sandbox_runs() -> None:
    agental, script = scripted(
        answer(202, {"sandbox": sandbox(status="creating"), "operation": {}}),
        httpx.Response(504, text="<html>gateway timeout</html>"),
        httpx.ConnectError("connection refused"),
        refusal(503, "uncertain_operation"),
        answer(200, sandbox()),
    )
    assert agental.sandboxes.create("agent-1").status == "running"
    assert len(script.sent) == 5
    assert len({request.headers["idempotency-key"] for request in script.sent}) == 1
    assert len({request.content for request in script.sent}) == 1


def test_a_create_that_fails_for_good_is_raised_with_the_apis_code() -> None:
    agental, script = scripted(refusal(409, "name_taken"))
    with pytest.raises(ApiError) as raised:
        agental.sandboxes.create("agent-1")
    assert (raised.value.status, raised.value.code, str(raised.value)) == (409, "name_taken", "name_taken it is")
    assert len(script.sent) == 1


def test_a_command_is_a_list_as_given_or_a_line_of_shell_and_is_never_sent_twice() -> None:
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, {"exitCode": 0, "stdout": "a\n", "stderr": "", "truncated": False}),
        answer(200, {"exitCode": 3, "stdout": "", "stderr": "no\n", "truncated": False}),
        refusal(503, "sandbox_moved"),
        httpx.Response(502, text="bad gateway"),
        httpx.ReadTimeout("timed out"),
    )
    box = agental.sandboxes.get(ID)
    assert box.exec(["echo", "a b"]).stdout == "a\n"
    assert script.body(1) == {"command": ["echo", "a b"]}
    # One that fails is an answer, not an error.
    assert box.exec("cd app && npm test", timeout=5).exit_code == 3
    assert script.body(2) == {"command": ["sh", "-c", "cd app && npm test"], "timeoutMs": 5000}

    with pytest.raises(ApiError, match="sandbox_moved"):
        box.exec("make")
    with pytest.raises(ProxyError):
        box.exec("make")
    with pytest.raises(AgentalConnectionError):
        box.exec("make")
    assert len(script.sent) == 6


def test_a_request_the_api_turned_away_as_it_restarted_is_sent_again_a_command_too() -> None:
    agental, script = scripted(
        answer(200, sandbox()),
        refusal(503, "restarting"),
        answer(200, {"exitCode": 0, "stdout": "built\n", "stderr": "", "truncated": False}),
        refusal(503, "restarting"),
        refusal(503, "restarting"),
        refusal(503, "restarting"),
        refusal(503, "restarting"),
    )
    box = agental.sandboxes.get(ID)
    assert box.exec("make").stdout == "built\n"
    assert len(script.sent) == 3
    assert script.body(2) == script.body(1)
    # It is sent again as often as any other, and no more.
    with pytest.raises(ApiError, match="restarting"):
        box.exec("make")
    assert len(script.sent) == 7


def test_a_read_is_sent_again_and_a_file_write_waits_a_move_out() -> None:
    agental, script = scripted(
        refusal(503, "sandbox_moved"),
        httpx.Response(502, text="bad gateway"),
        httpx.ConnectError("connection refused"),
        answer(200, sandbox()),
        refusal(409, "sandbox_migrating"),
        refusal(409, "sandbox_migrating"),
        answer(200, {"path": "a.txt", "bytes": 2}),
        answer(200, {"path": "a.txt", "encoding": "text", "text": "hi"}),
        answer(200, {"path": "a.bin", "bytes": 3}),
        answer(200, {"path": "a.bin", "encoding": "base64", "base64": "AP8Q"}),
    )
    box = agental.sandboxes.get(ID)
    assert len(script.sent) == 4

    assert box.files.write("a.txt", "hi").bytes == 2
    assert len(script.sent) == 7
    assert script.body(6) == {"path": "a.txt", "text": "hi"}
    assert box.files.read("a.txt") == "hi"
    assert str(script.sent[7].url) == f"https://api.test/v1/sandboxes/{ID}/files?path=a.txt"

    box.files.write("a.bin", bytes([0, 255, 16]))
    assert script.body(8) == {"path": "a.bin", "base64": "AP8Q"}
    assert box.files.read_bytes("a.bin") == bytes([0, 255, 16])
    assert script.sent[9].url.params["encoding"] == "base64"


def test_what_is_the_callers_doing_is_not_sent_again() -> None:
    agental, script = scripted(httpx.Response(404, text="<html>not found</html>"), refusal(404, "not_found"), refusal(500, "host_error"), refusal(500, "host_error"))
    with pytest.raises(ProxyError, match="base_url"):
        agental.sandboxes.list()
    with pytest.raises(ApiError, match="not_found"):
        agental.sandboxes.get("sbx_gone")
    assert len(script.sent) == 2
    # A failing host is tried once more, and no more than that.
    with pytest.raises(ApiError, match="host_error"):
        agental.limits()
    assert len(script.sent) == 4


def test_a_delete_says_which_sandbox_it_means() -> None:
    agental, script = scripted(answer(200, sandbox()), answer(204))
    agental.sandboxes.get(ID).delete()
    assert script.sent[1].method == "DELETE"
    assert script.sent[1].headers["x-agental-confirm-delete"] == ID


def test_a_start_waits_for_a_sandbox_that_is_brought_back_and_says_why_one_was_not() -> None:
    agental, script = scripted(
        answer(200, sandbox(status="stopped", archived=True)),
        answer(202, sandbox(status="restoring", archived=True)),
        answer(200, sandbox(status="restoring", archived=True)),
        answer(200, sandbox()),
        answer(202, sandbox(status="restoring")),
        answer(200, sandbox(status="stopped", error="the disk could not be read back")),
        answer(202, sandbox(status="restoring")),
    )
    box = agental.sandboxes.get(ID)
    assert box.start().status == "running"
    assert len(script.sent) == 4
    with pytest.raises(AgentalError, match="stopped, not running: the disk could not be read back"):
        box.start()
    assert box.start(wait=False).status == "restoring"


def test_an_archive_is_waited_for_and_one_that_fails_says_what_its_operation_says() -> None:
    agental, _ = scripted(
        answer(200, sandbox(status="stopped")),
        answer(202, sandbox(status="stopped"), **{"operation-id": "op_1"}),
        answer(200, {"operation": {"status": "pending", "error": None}, "sandbox": None}),
        answer(200, sandbox(status="stopped", archived=True)),
        answer(202, sandbox(status="stopped"), **{"operation-id": "op_2"}),
        answer(200, {"operation": {"status": "failed", "error": "the bucket did not answer"}, "sandbox": None}),
    )
    box = agental.sandboxes.get(ID)
    assert box.archive().info.archived is True
    with pytest.raises(AgentalError, match="was not archived: the bucket did not answer"):
        box.archive()


def stream(text: str) -> httpx.Response:
    return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


def test_a_streamed_command_gives_its_output_then_what_it_exited_with() -> None:
    agental, script = scripted(
        answer(200, sandbox()),
        stream('event: stdout\ndata: {"data":"one\\n"}\n\nevent: stderr\r\ndata: {"data":"two"}\r\n\r\nevent: exit\ndata: {"exitCode":7}\n\n'),
        stream('event: stdout\ndata: {"data":"one"}\n\nevent: error\ndata: the sandbox was moved\n\n'),
        stream('event: stdout\ndata: {"data":"one"}\n\n'),
        refusal(409, "sandbox_not_running"),
    )
    box = agental.sandboxes.get(ID)
    assert list(box.exec_stream(["sh", "-c", "echo one; echo two >&2; exit 7"])) == [
        ExecEvent("stdout", "one\n"),
        ExecEvent("stderr", "two"),
        ExecEvent("exit", exit_code=7),
    ]
    assert script.sent[1].headers["accept"] == "text/event-stream"
    with pytest.raises(AgentalError, match="cut off: the sandbox was moved"):
        list(box.exec_stream("make"))
    with pytest.raises(AgentalConnectionError):
        list(box.exec_stream("make"))
    with pytest.raises(ApiError, match="sandbox_not_running"):
        list(box.exec_stream("make"))


def test_a_fork_a_snapshot_and_a_port_ask_for_what_the_api_needs_of_them() -> None:
    snap = {"id": "snap_1", "sandboxId": ID, "name": "before", "createdAt": "", "browsable": False, "size": "small", "sandboxDeleted": False}
    route = {"id": "rt_1", "sandboxId": ID, "port": 8080, "title": "port-8080", "public": True, "hidden": False, "proxyPath": "/p", "registered": True, "listening": False, "proxy": "available", "url": "https://abc.agental.site"}
    agental, script = scripted(
        answer(200, sandbox()),
        answer(201, sandbox(id="sbx_fork000000000000", name="second-try")),
        answer(201, snap),
        answer(200, {"sandbox": sandbox(updatedAt="later"), "snapshot": snap, "discarded": ["snap_2"]}),
        answer(201, route),
        answer(200, sandbox(ttlSeconds=None)),
    )
    box = agental.sandboxes.get(ID)
    fork = box.fork("second-try")
    assert fork.id == "sbx_fork000000000000"
    assert script.sent[1].url.path == f"/v1/sandboxes/{ID}/fork"
    assert script.sent[1].headers["idempotency-key"]
    assert script.body(1) == {"name": "second-try"}

    assert box.snapshot("before").id == "snap_1"
    restored = box.restore("snap_1")
    assert (restored.snapshot.name, restored.discarded) == ("before", ["snap_2"])
    assert box.info.updated_at == "later"

    assert box.expose(8080).url == "https://abc.agental.site"
    assert script.body(4) == {"port": 8080, "title": "port-8080", "public": True}

    # None is a value of its own here: the deployment's default.
    box.update(ttl_seconds=None)
    assert script.body(5) == {"ttlSeconds": None}


def stop_of(**changes: Any) -> dict[str, Any]:
    return {"sandboxId": ID, "status": "running", "autoStopAt": "2026-10-13T02:00:00.000Z", "secondsLeft": 7200, "ttlSeconds": 7200, "setBy": "ttl", **changes}


def test_a_sandbox_is_made_with_a_time_limit_and_the_stop_of_its_run_is_read_and_moved_to_a_time_or_by_seconds_from_now() -> None:
    agental, script = scripted(
        answer(201, sandbox(ttlSeconds=7200, autoStopAt="2026-10-13T02:00:00.000Z")),
        answer(201, sandbox(id="sbx_default000000000")),
        answer(201, sandbox(id="sbx_template00000000")),
        answer(200, stop_of()),
        answer(200, stop_of(autoStopAt="2026-10-13T01:00:00.000Z", secondsLeft=3600, setBy="account")),
        httpx.ConnectError("connection refused"),
        answer(200, stop_of(autoStopAt="2026-10-13T05:00:00.000Z", secondsLeft=18_000, setBy="account")),
        refusal(400, "auto_stop_too_soon"),
        refusal(400, "auto_stop_too_far"),
        refusal(409, "invalid_state"),
    )

    # Its time limit as given, None for the deployment's whatever its template says, and left out its template's.
    box = agental.sandboxes.create("timed", ttl_seconds=7200)
    assert script.body(0) == {"name": "timed", "ttlSeconds": 7200}
    assert box.info.ttl_seconds == 7200
    agental.sandboxes.create("default-time", ttl_seconds=None)
    assert script.body(1) == {"name": "default-time", "ttlSeconds": None}
    agental.sandboxes.create("template-time")
    assert script.body(2) == {"name": "template-time"}

    stop = box.auto_stop()
    assert (stop.auto_stop_at, stop.seconds_left, stop.ttl_seconds, stop.set_by) == ("2026-10-13T02:00:00.000Z", 7200, 7200, "ttl")
    assert (script.sent[3].method, script.sent[3].url.path, script.sent[3].content) == ("GET", f"/v1/sandboxes/{ID}/auto-stop", b"")

    # Seconds from now, and what the sandbox says of its stop follows without another read.
    moved = box.set_auto_stop(in_seconds=3600)
    assert (moved.set_by, moved.seconds_left) == ("account", 3600)
    assert (script.sent[4].method, script.sent[4].url.path, script.body(4)) == ("PUT", f"/v1/sandboxes/{ID}/auto-stop", {"inSeconds": 3600})
    assert box.info.auto_stop_at == "2026-10-13T01:00:00.000Z"

    # A time, a datetime here. A move whose answer is lost is sent again: it moves the stop to the same place.
    box.set_auto_stop(at=datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc))
    assert script.body(5) == script.body(6) == {"at": "2026-10-13T05:00:00+00:00"}
    assert box.info.auto_stop_at == "2026-10-13T05:00:00.000Z"

    # Too soon, too far, or a sandbox that is not running: each as the API said it, none sent again, and the stop as it was.
    with pytest.raises(ApiError) as soon:
        box.set_auto_stop(at="2026-10-13T00:00:30Z")
    assert (soon.value.status, soon.value.code, script.body(7)) == (400, "auto_stop_too_soon", {"at": "2026-10-13T00:00:30Z"})
    with pytest.raises(ApiError, match="auto_stop_too_far"):
        box.set_auto_stop(in_seconds=9_999_999)
    with pytest.raises(ApiError) as stopped:
        box.set_auto_stop(in_seconds=3600)
    assert (stopped.value.status, stopped.value.code) == (409, "invalid_state")
    assert box.info.auto_stop_at == "2026-10-13T05:00:00.000Z"
    assert len(script.sent) == 10

    # A time or seconds from now, one of them: anything else is refused before it is sent.
    with pytest.raises(ValueError):
        box.set_auto_stop()
    with pytest.raises(ValueError):
        box.set_auto_stop(at="2026-10-13T05:00:00Z", in_seconds=3600)
    assert len(script.sent) == 10


def test_a_sandboxs_time_limit_is_taken_away_and_what_it_says_of_its_stop_follows() -> None:
    agental, script = scripted(
        answer(201, sandbox(ttlSeconds=7200)),
        answer(200, stop_of(autoStopAt=None, secondsLeft=None, ttlSeconds=None, setBy=None)),
    )
    box = agental.sandboxes.create("timed", ttl_seconds=7200)
    cleared = box.clear_auto_stop()
    assert (cleared.auto_stop_at, cleared.ttl_seconds, cleared.set_by) == (None, None, None)
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("DELETE", f"/v1/sandboxes/{ID}/auto-stop", b"")
    assert (box.info.auto_stop_at, box.info.ttl_seconds) == (None, None)


def test_the_client_that_awaits_makes_a_sandbox_with_a_time_limit_and_reads_and_moves_its_stop_the_same() -> None:
    script = Script(
        answer(201, sandbox(ttlSeconds=600)),
        answer(200, stop_of(ttlSeconds=600)),
        answer(200, stop_of(autoStopAt="2026-10-13T00:30:00.000Z", secondsLeft=1800, ttlSeconds=600, setBy="account")),
    )

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            box = await agental.sandboxes.create("timed", ttl_seconds=600)
            assert (await box.auto_stop()).set_by == "ttl"
            assert (await box.set_auto_stop(in_seconds=1800)).seconds_left == 1800
            assert box.info.auto_stop_at == "2026-10-13T00:30:00.000Z"

    asyncio.run(main())
    assert [(each.method, each.url.path) for each in script.sent] == [("POST", "/v1/sandboxes"), ("GET", f"/v1/sandboxes/{ID}/auto-stop"), ("PUT", f"/v1/sandboxes/{ID}/auto-stop")]
    assert (script.body(0)["ttlSeconds"], script.body(2)) == (600, {"inSeconds": 1800})


MAIL = f"/v1/sandboxes/{ID}/email"
MAIL_DOMAIN = "sbx-0123456789abcdef.k7m2x9qd.agental.email"


def message(**changes: Any) -> dict[str, Any]:
    return {
        "id": "eml_9c41d0a7b2e65f38",
        "sandboxId": ID,
        "direction": "received",
        "from": "dana@example.org",
        "to": f"support@{MAIL_DOMAIN}",
        "subject": "Re: invoice 1042",
        "sizeBytes": 512,
        "attachments": [{"filename": "invoice.pdf", "contentType": "application/pdf", "sizeBytes": 16}],
        "checks": {"spf": "pass", "dkim": "pass", "dmarc": "pass"},
        "spamScore": 1.2,
        "sentByProgram": False,
        "createdAt": "2026-10-14T00:00:00.000Z",
        "state": None,
        "stateDetail": None,
        **changes,
    }


def test_a_sandboxs_mail_is_read_turned_on_and_turned_off_by_calls_of_their_own() -> None:
    off = {"enabled": False, "enabledAt": None, "domain": MAIL_DOMAIN}
    on = {"enabled": True, "enabledAt": "2026-10-14T00:00:00.000Z", "domain": MAIL_DOMAIN}
    agental, script = scripted(answer(200, sandbox()), answer(200, off), httpx.Response(502, text="bad gateway"), answer(200, on), answer(200, off), refusal(501, "mail_not_configured"))
    box = agental.sandboxes.get(ID)

    state = box.email.state()
    assert (state.enabled, state.enabled_at, state.domain) == (False, None, MAIL_DOMAIN)
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("GET", MAIL, b"")

    # Turning it on twice does what turning it on once does, so it is asked again when a proxy answers in the API's place.
    turned_on = box.email.enable()
    assert (turned_on.enabled, turned_on.enabled_at, turned_on.domain) == (True, "2026-10-14T00:00:00.000Z", MAIL_DOMAIN)
    assert (script.sent[2].method, script.sent[2].url.path, script.sent[2].content) == ("PUT", MAIL, b"")
    assert (script.sent[3].method, script.sent[3].url.path) == ("PUT", MAIL)

    assert box.email.disable().enabled is False
    assert (script.sent[4].method, script.sent[4].url.path, script.sent[4].content) == ("DELETE", MAIL, b"")

    # Where no mail is offered the API says so, in its own code.
    with pytest.raises(ApiError) as refused:
        box.email.enable()
    assert (refused.value.status, refused.value.code) == (501, "mail_not_configured")
    assert len(script.sent) == 6


def test_messages_are_listed_a_page_at_a_time_walked_page_by_page_read_fetched_as_transmitted_and_deleted() -> None:
    first = message()
    second = message(id="eml_0000000000000002", subject="older")
    third = message(id="eml_0000000000000003", subject="oldest")
    full = {**first, "messageIdHeader": "<abc123@example.org>", "headers": [{"name": "subject", "value": "Re: invoice 1042"}], "text": "The invoice is attached.", "html": None}
    # Bytes that are no text at all come back as they are.
    transmitted = b"From: \xff\x00\r\n"
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, {"messages": [first], "nextCursor": "c1"}),
        answer(200, {"messages": [second], "nextCursor": None}),
        answer(200, {"messages": [first, second], "nextCursor": "c2"}),
        answer(200, {"messages": [third], "nextCursor": None}),
        answer(200, full),
        httpx.Response(200, content=transmitted, headers={"content-type": "message/rfc822"}),
        answer(204),
        refusal(404, "not_found"),
    )
    box = agental.sandboxes.get(ID)

    page = box.email.list()
    assert ([each.id for each in page.messages], page.next_cursor) == (["eml_9c41d0a7b2e65f38"], "c1")
    listed = page.messages[0]
    # from is a word of Python's, so what the API calls from is from_ here.
    assert (listed.from_, listed.to, listed.direction, listed.spam_score, listed.sent_by_program, listed.state) == ("dana@example.org", f"support@{MAIL_DOMAIN}", "received", 1.2, False, None)
    assert (listed.attachments[0].filename, listed.attachments[0].content_type, listed.attachments[0].size_bytes) == ("invoice.pdf", "application/pdf", 16)
    assert (listed.checks.spf, listed.checks.dkim, listed.checks.dmarc) == ("pass", "pass", "pass")
    assert (script.sent[1].method, str(script.sent[1].url)) == ("GET", f"https://api.test{MAIL}/messages")
    assert [each.subject for each in box.email.list(cursor="c1", limit=1).messages] == ["older"]
    assert str(script.sent[2].url) == f"https://api.test{MAIL}/messages?cursor=c1&limit=1"

    # Every page is asked for in turn, each with the cursor the one before it gave, until one gives none.
    assert [each.id for each in box.email.all(limit=2)] == ["eml_9c41d0a7b2e65f38", "eml_0000000000000002", "eml_0000000000000003"]
    assert str(script.sent[3].url) == f"https://api.test{MAIL}/messages?limit=2"
    assert str(script.sent[4].url) == f"https://api.test{MAIL}/messages?cursor=c2&limit=2"

    read = box.email.get("eml_9c41d0a7b2e65f38")
    assert (read.from_, read.subject, read.message_id_header, read.text, read.html) == ("dana@example.org", "Re: invoice 1042", "<abc123@example.org>", "The invoice is attached.", None)
    assert (read.headers[0].name, read.headers[0].value) == ("subject", "Re: invoice 1042")
    assert (script.sent[5].method, script.sent[5].url.path) == ("GET", f"{MAIL}/messages/eml_9c41d0a7b2e65f38")

    assert box.email.raw("eml_9c41d0a7b2e65f38") == transmitted
    assert (script.sent[6].method, script.sent[6].url.path, script.sent[6].headers["accept"]) == ("GET", f"{MAIL}/messages/eml_9c41d0a7b2e65f38/raw", "message/rfc822")

    box.email.delete("eml_9c41d0a7b2e65f38")
    assert (script.sent[7].method, script.sent[7].url.path) == ("DELETE", f"{MAIL}/messages/eml_9c41d0a7b2e65f38")
    # A delete is not asked twice: the second answer would be that there is no such message.
    with pytest.raises(ApiError, match="not_found"):
        box.email.delete("eml_9c41d0a7b2e65f38")
    assert len(script.sent) == 9


def test_a_message_is_sent_with_a_key_of_its_own_asked_again_when_the_answer_is_lost_and_a_refusal_is_not() -> None:
    taken = message(id="eml_00000000000000aa", direction="sent", to="ada@example.com", subject="The build passed", attachments=[], sentByProgram=True, state="handed_over", **{"from": f"reports@{MAIL_DOMAIN}"})
    agental, script = scripted(
        answer(200, sandbox()),
        httpx.ConnectError("connection refused"),
        answer(200, taken),
        answer(202, taken),
        refusal(403, "recipient_not_allowed"),
        refusal(409, "mail_off"),
        refusal(429, "mail_limit_reached"),
    )
    box = agental.sandboxes.get(ID)

    said = box.email.send(
        "The build passed",
        "All 812 tests passed.",
        from_="reports",
        from_name="Nightly report",
        reply_to="eml_9c41d0a7b2e65f38",
        attachments=[SendEmailAttachment("log.txt", "b2sK", content_type="text/plain"), SendEmailAttachment("raw.bin", "AP8Q")],
    )
    assert (said.id, said.direction, said.from_, said.to, said.state) == ("eml_00000000000000aa", "sent", f"reports@{MAIL_DOMAIN}", "ada@example.com", "handed_over")
    assert len(script.sent) == 3
    assert (script.sent[1].method, script.sent[1].url.path) == ("POST", f"{MAIL}/messages")
    # Nobody is named: the message is for the account's own address, which the API knows. What was not said is not sent.
    assert script.body(1) == {
        "subject": "The build passed",
        "text": "All 812 tests passed.",
        "from": "reports",
        "fromName": "Nightly report",
        "replyTo": "eml_9c41d0a7b2e65f38",
        "attachments": [{"filename": "log.txt", "contentBase64": "b2sK", "contentType": "text/plain"}, {"filename": "raw.bin", "contentBase64": "AP8Q"}],
    }
    assert script.sent[1].headers["idempotency-key"].startswith("sdk-")
    assert script.sent[2].headers["idempotency-key"] == script.sent[1].headers["idempotency-key"]
    assert script.sent[2].content == script.sent[1].content

    # The caller's own key goes as the header, and is no part of the message.
    box.email.send("Again", "", to="ada@example.com", idempotency_key="nightly-2026-10-14")
    assert script.sent[3].headers["idempotency-key"] == "nightly-2026-10-14"
    assert script.body(3) == {"subject": "Again", "text": "", "to": "ada@example.com"}

    for status, code in ((403, "recipient_not_allowed"), (409, "mail_off"), (429, "mail_limit_reached")):
        with pytest.raises(ApiError) as refused:
            box.email.send("Hello", "hi", to="stranger@example.org")
        assert (refused.value.status, refused.value.code) == (status, code)
    assert len(script.sent) == 7


def test_a_sandboxs_mail_is_reached_by_its_id_without_reading_the_sandbox_and_the_accounts_mail_is_read() -> None:
    account = {
        "handle": "k7m2x9qd",
        "domain": "k7m2x9qd.agental.email",
        "limits": {"receivedPerDay": 1000, "receivedBytesPerDay": 1_000_000_000, "keptBytes": 5_000_000_000, "sentPerHour": 30, "sentPerDay": 200, "sentBytesPerDay": 1_000_000_000},
        "today": {"received": 2, "receivedBytes": 1024, "sent": 1, "sentBytes": 300},
        "keptBytes": 1324,
    }
    agental, script = scripted(answer(200, {"messages": [message()], "nextCursor": None}), refusal(403, "key_action_forbidden"), answer(200, account))
    # A key with email.read and email.send on one sandbox may ask for nothing but that sandbox's mail, so the first
    # request made is for the mail itself.
    mail = agental.sandboxes.email(ID)
    assert len(script.sent) == 0
    assert mail.list().messages[0].subject == "Re: invoice 1042"
    assert (script.sent[0].method, script.sent[0].url.path) == ("GET", f"{MAIL}/messages")
    with pytest.raises(ApiError) as limited:
        agental.sandboxes.get(ID)
    assert (limited.value.status, limited.value.code) == (403, "key_action_forbidden")

    read = agental.email()
    assert (read.handle, read.domain, read.kept_bytes) == ("k7m2x9qd", "k7m2x9qd.agental.email", 1324)
    assert (read.limits.received_per_day, read.limits.kept_bytes, read.limits.sent_per_hour, read.limits.sent_per_day) == (1000, 5_000_000_000, 30, 200)
    assert (read.today.received, read.today.received_bytes, read.today.sent, read.today.sent_bytes) == (2, 1024, 1, 300)
    assert (script.sent[2].method, script.sent[2].url.path) == ("GET", "/v1/email")


def quote(size: str, fee: int = 0, **changes: Any) -> dict[str, Any]:
    return {"size": size, "count": 1, "feeMicros": fee, "freeInSeconds": 0, "overRunningCount": False, **changes}


def burst(**changes: Any) -> dict[str, Any]:
    return {
        "enforced": True,
        "plan": "plan100",
        "limitsFrom": "plan",
        "limitsPlan": "plan100",
        "runningUnits": 100,
        "freeBurstUnits": 100,
        "runningMb": 8192,
        "capMb": 409600,
        "freeMb": 409600,
        "meterMb": 8192,
        "freeNowMb": 401408,
        "overMb": 0,
        "halfLifeMinutes": 15,
        "feeMicrosPerGib": 20000,
        "mayStart": True,
        "freeMonth": False,
        "spentMicros": 0,
        "budgetMicros": 50_000_000,
        "budgetSet": False,
        "defaultBudgetMicros": 50_000_000,
        "nextStart": [quote("tiny"), quote("small"), quote("medium")],
        "quote": None,
        **changes,
    }


def test_the_burst_is_read_with_a_quote_and_its_budget_is_set_and_cleared() -> None:
    agental, script = scripted(
        answer(200, burst()),
        answer(200, burst(quote=quote("medium", 640_000, count=30, freeInSeconds=1800))),
        answer(200, burst(budgetMicros=0, budgetSet=True)),
        httpx.ConnectError("lost"),
        answer(200, burst()),
    )
    read = agental.burst()
    assert (read.running_units, read.free_burst_units, read.quote, read.next_start[2].size) == (100, 100, None, "medium")
    assert (script.sent[0].method, script.sent[0].url.path, script.sent[0].url.query) == ("GET", "/v1/burst", b"")
    quoted = agental.burst(size="medium", count=30).quote
    assert quoted is not None and (quoted.fee_micros, quoted.free_in_seconds, quoted.over_running_count) == (640_000, 1800, False)
    assert script.sent[1].url.query == b"size=medium&count=30"

    assert agental.set_burst_budget(0).budget_micros == 0
    assert (script.sent[2].method, script.sent[2].url.path, script.body(2)) == ("PUT", "/v1/burst/budget", {"budgetMicros": 0})
    # Setting a budget twice sets it once, so a lost answer is sent again.
    assert agental.set_burst_budget(None).budget_set is False
    assert script.body(4) == {"budgetMicros": None}


def test_a_create_fork_and_start_send_the_most_they_may_pay_and_say_what_they_paid() -> None:
    agental, script = scripted(
        answer(201, sandbox(), **{"Agental-Burst-Fee": "32000"}),
        answer(201, sandbox(id="sbx_fork000000000000"), **{"Agental-Burst-Fee": "0"}),
        answer(200, sandbox(), **{"Agental-Burst-Fee": "8000"}),
        answer(200, sandbox()),
    )
    made = agental.sandboxes.create(max_burst_fee_micros=50_000)
    assert (script.sent[0].headers["agental-max-burst-fee"], made.burst_fee_micros) == ("50000", 32_000)
    fork = made.fork(max_burst_fee_micros=0)
    assert (script.sent[1].url.path, script.sent[1].headers["agental-max-burst-fee"], fork.burst_fee_micros) == (f"/v1/sandboxes/{ID}/fork", "0", 0)
    made.start(max_burst_fee_micros=10_000)
    assert (script.sent[2].headers["agental-max-burst-fee"], made.burst_fee_micros) == ("10000", 8000)
    # Left out, no cap is sent, and an answer without the header says nothing of a fee.
    made.start()
    assert ("agental-max-burst-fee" in script.sent[3].headers, made.burst_fee_micros) == (False, None)
    with pytest.raises(AgentalError, match="whole number"):
        agental.sandboxes.create(max_burst_fee_micros=-1)
    assert len(script.sent) == 4


def burst_refusal(code: str, fee: int, after: int | None) -> httpx.Response:
    headers = {} if after is None else {"Retry-After": str(after)}
    return answer(429, {"error": {"code": code, "message": f"{code} it is", "feeMicros": fee, "retryAfterSeconds": after}}, **headers)


def test_a_start_refused_for_its_burst_says_what_it_would_cost_and_when_it_would_cost_nothing() -> None:
    agental, script = scripted(burst_refusal("burst_fee_too_high", 64_000, 240), burst_refusal("running_limit_reached", 0, None), refusal(409, "name_taken"))
    with pytest.raises(ApiError) as fee:
        agental.sandboxes.create(max_burst_fee_micros=0)
    assert (fee.value.status, fee.value.code, fee.value.fee_micros, fee.value.retry_after_seconds) == (429, "burst_fee_too_high", 64_000, 240)
    assert fee.value.code in BURST_REFUSALS
    with pytest.raises(ApiError) as running:
        agental.sandboxes.create()
    assert (running.value.code, running.value.fee_micros, running.value.retry_after_seconds) == ("running_limit_reached", 0, None)
    with pytest.raises(ApiError) as other:
        agental.sandboxes.create()
    assert (other.value.fee_micros, other.value.retry_after_seconds) == (None, None)
    assert len(script.sent) == 3


def test_asked_to_a_start_refused_for_its_burst_waits_until_it_would_cost_nothing_and_is_tried_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("agental._sync.time.sleep", slept.append)
    agental, script = scripted(
        burst_refusal("burst_fee_too_high", 64_000, 30),
        answer(201, sandbox(), **{"Agental-Burst-Fee": "0"}),
        burst_refusal("burst_budget_reached", 64_000, 60),
        answer(201, sandbox(id="sbx_fork000000000000")),
        burst_refusal("burst_in_free_month", 8000, 5),
        answer(200, sandbox()),
    )
    made = agental.sandboxes.create(max_burst_fee_micros=0, max_burst_wait=60, idempotency_key="task-0002")
    assert made.burst_fee_micros == 0
    # The second try is the same create, with the same key and cap.
    assert (script.sent[1].headers["idempotency-key"], script.sent[1].headers["agental-max-burst-fee"]) == ("task-0002", "0")
    made.fork(max_burst_wait=60)
    made.start(max_burst_wait=60)
    assert script.sent[5].url.path == f"/v1/sandboxes/{ID}/start"
    assert slept == [30, 60, 5]


def test_a_start_is_not_tried_again_for_its_burst_unless_asked_past_the_wait_allowed_more_than_once_or_for_the_running_count(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("agental._sync.time.sleep", slept.append)
    agental, script = scripted(
        burst_refusal("burst_fee_too_high", 64_000, 0),
        burst_refusal("burst_fee_too_high", 64_000, 240),
        burst_refusal("burst_fee_too_high", 64_000, None),
        burst_refusal("running_limit_reached", 0, 0),
        burst_refusal("burst_budget_reached", 64_000, 0),
        burst_refusal("burst_budget_reached", 64_000, 0),
    )
    for wait in (None, 60, 60, 60, 60):
        with pytest.raises(ApiError):
            agental.sandboxes.create(max_burst_wait=wait)
    assert (len(script.sent), slept) == (6, [0])


def test_the_client_that_awaits_waits_out_a_burst_and_reads_the_burst_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("agental._async.asyncio.sleep", no_sleep)
    script = Script(answer(200, burst()), burst_refusal("burst_fee_too_high", 64_000, 12), answer(201, sandbox(), **{"Agental-Burst-Fee": "0"}), answer(200, burst(spentMicros=0)))

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            assert (await agental.burst()).budget_micros == 50_000_000
            box = await agental.sandboxes.create(max_burst_fee_micros=0, max_burst_wait=30)
            assert box.burst_fee_micros == 0
            assert (await agental.set_burst_budget(None)).spent_micros == 0

    asyncio.run(main())
    assert slept == [12]
    assert [(each.method, each.url.path) for each in script.sent] == [("GET", "/v1/burst"), ("POST", "/v1/sandboxes"), ("POST", "/v1/sandboxes"), ("PUT", "/v1/burst/budget")]


def test_the_usage_and_the_policy_say_what_mail_there_was_and_whether_there_is_any() -> None:
    usage = {"since": "2026-10-01T00:00:00.000Z", "until": "2026-10-14T00:00:00.000Z", "seconds": 60, "listCostMicros": 270, "currency": "usd", "cost": None, "billing": "not_configured"}
    by_sandbox = {"sandboxId": ID, "name": "agent-1", "received": 3, "sent": 1, "costMicros": 2000}
    policy = {"signup": "open", "forks": True, "ssh": False, "email": {"receiving": True, "sending": False, "domain": "agental.email"}}
    agental, _ = scripted(
        answer(200, sandbox()),
        answer(200, {**usage, "sandboxId": ID, "durationMs": 60_000, "mail": {"received": 3, "sent": 1, "costMicros": 2000}}),
        answer(200, {**usage, "mail": {"received": 3, "sent": 1, "costMicros": 2000, "sandboxes": [by_sandbox]}, "sandboxes": [], "keys": [], "days": []}),
        answer(200, policy),
    )
    of_one = agental.sandboxes.get(ID).usage().mail
    assert (of_one.received, of_one.sent, of_one.cost_micros) == (3, 1, 2000)
    of_all = agental.usage().mail
    assert (of_all.received, of_all.sent, of_all.cost_micros) == (3, 1, 2000)
    assert (of_all.sandboxes[0].sandbox_id, of_all.sandboxes[0].name, of_all.sandboxes[0].cost_micros) == (ID, "agent-1", 2000)
    offered = agental.policy().email
    assert (offered.receiving, offered.sending, offered.domain) == (True, False, "agental.email")


def test_the_policy_says_whether_a_variable_can_be_kept_out_of_its_sandbox() -> None:
    agental, _ = scripted(answer(200, {"signup": "open", "networkPolicy": True, "hiddenVariables": True}), answer(200, {"signup": "open", "hiddenVariables": False}))
    assert agental.policy().hidden_variables is True
    assert agental.policy().hidden_variables is False


def test_usage_takes_several_keys_and_reads_each_day_apart_and_each_hour() -> None:
    usage = {"since": "2026-10-01T00:00:00.000Z", "until": "2026-10-14T00:00:00.000Z", "seconds": 3600, "listCostMicros": 18000, "currency": "usd", "cost": None, "billing": "not_configured"}
    day = {
        "day": "2026-10-09",
        "seconds": 3600,
        "listCostMicros": 18000,
        "stopped": {"seconds": 7200, "listCostMicros": 1620},
        "snapshots": {"chargedSeconds": 0, "listCostMicros": 0},
        "mail": {"received": 2, "sent": 0, "costMicros": 1000},
    }
    hour = {"hour": "2026-10-09T02:00:00.000Z", "sandboxes": 2, "seconds": 3600, "listCostMicros": 18000}
    agental, script = scripted(
        answer(200, {**usage, "mail": {"received": 2, "sent": 0, "costMicros": 1000, "sandboxes": []}, "sandboxes": [], "keys": [], "days": [day], "hours": [hour]}),
        answer(200, {**usage, "mail": {"received": 0, "sent": 0, "costMicros": 0, "sandboxes": []}, "sandboxes": [], "keys": [], "days": []}),
    )
    read = agental.usage(api_key_id=["key_1", "console"])
    assert script.sent[0].url.params["apiKeyId"] == "key_1,console"
    first = read.days[0]
    assert first.stopped is not None and first.mail is not None and first.snapshots is not None
    assert (first.stopped.list_cost_micros, first.mail.cost_micros, first.snapshots.charged_seconds) == (1620, 1000, 0)
    assert read.hours is not None
    assert (read.hours[0].hour, read.hours[0].sandboxes) == ("2026-10-09T02:00:00.000Z", 2)
    # An API from before hours answers without them.
    older = agental.usage(api_key_id="key_1")
    assert script.sent[1].url.params["apiKeyId"] == "key_1"
    assert older.hours is None


def test_the_client_that_awaits_reads_and_sends_mail_the_same() -> None:
    transmitted = b"From: \xff\x00\r\n"
    taken = message(id="eml_00000000000000aa", direction="sent", state="accepted")
    script = Script(
        answer(200, {"enabled": True, "enabledAt": "2026-10-14T00:00:00.000Z", "domain": MAIL_DOMAIN}),
        answer(200, {"messages": [message()], "nextCursor": "c1"}),
        answer(200, {"messages": [message(id="eml_0000000000000002")], "nextCursor": None}),
        httpx.Response(200, content=transmitted, headers={"content-type": "message/rfc822"}),
        httpx.ConnectError("connection refused"),
        answer(200, taken),
        answer(200, {"handle": None, "domain": None, "limits": {}, "today": {}, "keptBytes": 0}),
    )

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            mail = agental.sandboxes.email(ID)
            assert (await mail.enable()).enabled is True
            assert [each.id async for each in mail.all()] == ["eml_9c41d0a7b2e65f38", "eml_0000000000000002"]
            assert await mail.raw("eml_9c41d0a7b2e65f38") == transmitted
            assert (await mail.send("The build passed", "All 812 tests passed.")).state == "accepted"
            assert (await agental.email()).handle is None

    asyncio.run(main())
    assert len(script.sent) == 7
    assert (script.sent[0].method, script.sent[0].url.path) == ("PUT", MAIL)
    assert script.sent[2].url.params["cursor"] == "c1"
    assert script.sent[4].headers["idempotency-key"] == script.sent[5].headers["idempotency-key"]
    assert script.body(5) == {"subject": "The build passed", "text": "All 812 tests passed."}


def reading(**changes: Any) -> dict[str, Any]:
    return {
        "at": "2026-10-14T16:00:00.000Z",
        "cpuUtilization": 0.25,
        "memoryUsedBytes": 536870912,
        "memoryTotalBytes": 2147483648,
        "diskUsedBytes": 1073741824,
        "diskTotalBytes": 10737418240,
        "networkReceivedBytesPerSecond": 1200,
        "networkSentBytesPerSecond": 300,
        "diskReadBytesPerSecond": 0,
        "diskWrittenBytesPerSecond": 4096,
        **changes,
    }


def test_a_sandboxs_readings_every_sandboxs_at_one_moment_and_how_many_were_running_are_asked_for_with_what_was_given() -> None:
    read = {"sandboxId": ID, "since": "2026-10-14T15:00:00.000Z", "until": "2026-10-14T16:00:00.000Z", "stepSeconds": 60, "keptDays": 15, "points": [reading(cpuUtilization=None)], "complete": False}
    moment = {
        "at": "2026-10-14T09:35:00.000Z",
        "sandboxes": [
            {"sandboxId": ID, "name": "agent-1", "size": "small", "reading": reading()},
            {"sandboxId": "sbx_stopped000000000", "name": "agent-2", "size": "medium", "reading": None},
        ],
        "complete": False,
    }
    counted = {"since": "2026-10-13T16:00:00.000Z", "until": "2026-10-14T16:00:00.000Z", "stepSeconds": 300, "points": [{"at": "2026-10-14T16:00:00.000Z", "running": 2}]}
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, read),
        answer(200, read),
        answer(200, moment),
        answer(200, moment),
        answer(200, counted),
        answer(200, counted),
        refusal(501, "metrics_unavailable"),
        refusal(501, "metrics_unavailable"),
        refusal(503, "metrics_unreadable"),
        refusal(400, "invalid_request"),
    )
    box = agental.sandboxes.get(ID)

    # With nothing given nothing is asked for, and the API reads the stretch it reads by itself.
    whole = box.metrics()
    assert (script.sent[1].method, str(script.sent[1].url)) == ("GET", f"https://api.test/v1/sandboxes/{ID}/metrics")
    assert (whole.sandbox_id, whole.since, whole.until, whole.step_seconds, whole.kept_days) == (ID, "2026-10-14T15:00:00.000Z", "2026-10-14T16:00:00.000Z", 60, 15)
    # A datetime is sent as the moment it is, and a string as it was given.
    some = box.metrics(since=datetime(2026, 10, 14, 15, tzinfo=timezone.utc), until="2026-10-14T16:00:00Z", step_seconds=60)
    assert str(script.sent[2].url) == f"https://api.test/v1/sandboxes/{ID}/metrics?since=2026-10-14T15%3A00%3A00%2B00%3A00&until=2026-10-14T16%3A00%3A00Z&stepSeconds=60"
    # A figure the server did not have is None, and a server that did not answer is said as the API said it.
    assert (some.points[0].at, some.points[0].cpu_utilization, some.points[0].memory_used_bytes) == ("2026-10-14T16:00:00.000Z", None, 536870912)
    assert some.points[0].disk_written_bytes_per_second == 4096
    assert some.complete is False

    now = agental.metrics()
    assert (script.sent[3].method, str(script.sent[3].url)) == ("GET", "https://api.test/v1/metrics")
    first, stopped = now.sandboxes
    assert (first.sandbox_id, first.name, first.size) == (ID, "agent-1", "small")
    assert first.reading is not None and first.reading.cpu_utilization == 0.25
    # A sandbox that was stopped then has no reading.
    assert (stopped.name, stopped.reading) == ("agent-2", None)
    assert now.complete is False
    agental.metrics(at=datetime(2026, 10, 14, 9, 35, tzinfo=timezone.utc))
    assert str(script.sent[4].url) == "https://api.test/v1/metrics?at=2026-10-14T09%3A35%3A00%2B00%3A00"

    ran = agental.running_over_time()
    assert (script.sent[5].method, str(script.sent[5].url)) == ("GET", "https://api.test/v1/metrics/running")
    assert (ran.step_seconds, ran.points[0].at, ran.points[0].running) == (300, "2026-10-14T16:00:00.000Z", 2)
    agental.running_over_time(since="2026-10-13T16:00:00Z", until=datetime(2026, 10, 14, 16, tzinfo=timezone.utc), step_seconds=300)
    assert str(script.sent[6].url) == "https://api.test/v1/metrics/running?since=2026-10-13T16%3A00%3A00Z&until=2026-10-14T16%3A00%3A00%2B00%3A00&stepSeconds=300"

    # Where sandboxes are not read the API says so. Servers that did not answer are not asked again by the SDK.
    with pytest.raises(ApiError) as unread:
        box.metrics()
    assert (unread.value.status, unread.value.code) == (501, "metrics_unavailable")
    with pytest.raises(ApiError) as unread:
        agental.metrics()
    assert (unread.value.status, unread.value.code) == (501, "metrics_unavailable")
    with pytest.raises(ApiError) as silent:
        box.metrics()
    assert (silent.value.status, silent.value.code) == (503, "metrics_unreadable")
    assert len(script.sent) == 10
    with pytest.raises(ApiError) as wrong:
        agental.running_over_time(since="2026-10-14T16:00:00Z", until="2026-10-14T15:00:00Z")
    assert wrong.value.status == 400


def collector(**changes: Any) -> dict[str, Any]:
    return {
        "endpoint": "https://otlp.example.com",
        "headers": ["x-key"],
        "lastExportAt": None,
        "lastError": None,
        "createdAt": "2026-10-14T09:00:00.000Z",
        "updatedAt": "2026-10-14T09:00:00.000Z",
        **changes,
    }


def test_a_collector_is_read_set_tried_and_removed_by_calls_of_their_own() -> None:
    failed = collector(lastExportAt="2026-10-14T09:01:00.000Z", lastError={"message": "The collector answered 403.", "status": 403, "at": "2026-10-14T09:02:00.000Z"})
    agental, script = scripted(
        answer(200, {"telemetry": None}),
        answer(200, {"telemetry": collector()}),
        httpx.ConnectError("connection refused"),
        answer(200, {"telemetry": collector(headers=[])}),
        answer(200, {"telemetry": failed}),
        answer(200, {"ok": False, "status": 401, "message": "The collector answered 401."}),
        httpx.ConnectError("connection refused"),
        answer(204),
        refusal(404, "not_found"),
        refusal(501, "telemetry_unavailable"),
    )

    # None is set at first.
    assert agental.get_telemetry() is None
    assert (script.sent[0].method, str(script.sent[0].url), script.sent[0].content) == ("GET", "https://api.test/v1/telemetry", b"")

    # The headers' values are sent, and only their names come back.
    made = agental.set_telemetry("https://otlp.example.com", headers={"x-key": "secret"})
    assert (made.endpoint, made.headers, made.last_export_at, made.last_error) == ("https://otlp.example.com", ["x-key"], None, None)
    assert (made.created_at, made.updated_at) == ("2026-10-14T09:00:00.000Z", "2026-10-14T09:00:00.000Z")
    assert (script.sent[1].method, script.sent[1].url.path, script.body(1)) == ("PUT", "/v1/telemetry", {"endpoint": "https://otlp.example.com", "headers": {"x-key": "secret"}})
    # Without headers none are sent. A set whose answer is lost is sent again: setting a collector twice does what
    # setting it once does.
    assert agental.set_telemetry("https://otlp.example.com").headers == []
    assert (script.sent[2].method, script.body(2), script.body(3)) == ("PUT", {"endpoint": "https://otlp.example.com"}, {"endpoint": "https://otlp.example.com"})

    # How the last export went is said as the API said it.
    read = agental.get_telemetry()
    assert read is not None and read.last_export_at == "2026-10-14T09:01:00.000Z"
    assert read.last_error is not None
    assert (read.last_error.message, read.last_error.status, read.last_error.at) == ("The collector answered 403.", 403, "2026-10-14T09:02:00.000Z")

    # A try sends nothing of the caller's, and a collector that refused it is an answer, not an error.
    tried = agental.test_telemetry()
    assert (tried.ok, tried.status, tried.message) == (False, 401, "The collector answered 401.")
    assert (script.sent[5].method, script.sent[5].url.path, script.sent[5].content) == ("POST", "/v1/telemetry/test", b"")
    # One whose answer is lost is not sent again, because each try sends the collector an export.
    with pytest.raises(AgentalConnectionError):
        agental.test_telemetry()
    assert len(script.sent) == 7

    agental.delete_telemetry()
    assert (script.sent[7].method, script.sent[7].url.path, script.sent[7].content) == ("DELETE", "/v1/telemetry", b"")
    # When none is set there is nothing to remove, and the API says so.
    with pytest.raises(ApiError) as missing:
        agental.delete_telemetry()
    assert (missing.value.status, missing.value.code) == (404, "not_found")
    # Where nothing is sent to a collector the API says that too.
    with pytest.raises(ApiError) as unavailable:
        agental.get_telemetry()
    assert (unavailable.value.status, unavailable.value.code) == (501, "telemetry_unavailable")
    assert len(script.sent) == 10


def test_billing_is_read_and_a_top_up_or_a_plan_is_bought_by_the_checkout_call_which_is_never_sent_twice() -> None:
    standing = {
        "provider": "stripe",
        "testMode": True,
        "enforced": True,
        "autoReload": {"dollars": 50},
        "canAutoReload": True,
        "status": "active",
        "plan": {"id": "plan500", "dollarsPerMonth": 500, "includedMicros": 500_000_000, "sandboxesAtOnce": 1000, "sizes": ["tiny", "small", "medium", "large"], "burst": {"tier": 2, "perQuarterMb": 65536, "perHourMb": 196608}},
        "period": {"start": "2026-10-01T00:00:00.000Z", "end": "2026-11-01T00:00:00.000Z"},
        "freeMonth": None,
        "includedMicros": 500_000_000,
        "usedMicros": 2_000_000_000,
        "discount": {"micros": 15_000_000, "percent": 1.5, "next": {"fromMicros": 5_000_000_000, "percent": 3}},
        "mailMicros": 0,
        "topUpMicros": 215_000_000,
        "sandboxesAtOnce": 1000,
        "cancelAtPeriodEnd": False,
        "hadFreeMonth": True,
        "canRun": True,
        "reason": None,
    }
    prices = {
        "currency": "usd",
        "interval": "hour",
        "label": "list price",
        "note": "These are list prices per hour.",
        "plans": [{"size": "small", "vcpu": 2, "memoryMb": 4096, "diskGb": 40, "listPriceMicrosPerHour": 18000, "currency": "usd", "label": "list price"}],
        "tiers": [{"id": "plan20", "dollarsPerMonth": 20, "includedMicros": 20_000_000, "sandboxesAtOnce": 100, "sizes": ["tiny", "small"], "burst": {"tier": 0, "perQuarterMb": 16384, "perHourMb": 49152}}],
        # One as an API older than the bonus sends it, and one with it.
        "topUps": [{"id": "topup50", "dollars": 50, "micros": 50_000_000}, {"id": "topup500", "dollars": 500, "micros": 537_000_000, "bonusMicros": 37_000_000, "bonusPercent": 7.4}],
        "topUpRange": {"minDollars": 15, "maxDollars": 100_000},
        "topUpBonus": {"fromDollars": 200, "fromPercent": 2, "toDollars": 10_000, "toPercent": 25},
        "volumeDiscount": [{"fromMicros": 0, "percent": 0}, {"fromMicros": 1_000_000_000, "percent": 1.5}],
        "freeMonth": {"days": 30, "planId": "plan20"},
    }
    agental, script = scripted(
        answer(200, prices),
        answer(200, standing),
        answer(200, {"url": "https://pay.example/topup777", "changed": False}),
        httpx.ConnectError("connection refused"),
        answer(200, {"url": None, "changed": True}),
    )

    listed = agental.prices()
    # What an older API still sends, the volume discount and the plans' tiers, is left unread.
    assert listed.top_ups[0].dollars == 50 and not hasattr(listed, "volume_discount")
    assert (listed.top_ups[0].bonus_micros, listed.top_ups[1].micros, listed.top_ups[1].bonus_micros, listed.top_ups[1].bonus_percent) == (None, 537_000_000, 37_000_000, 7.4)
    assert listed.top_up_range is not None and (listed.top_up_range.min_dollars, listed.top_up_range.max_dollars) == (15, 100_000)
    assert listed.top_up_bonus is not None and (listed.top_up_bonus.from_dollars, listed.top_up_bonus.to_percent) == (200, 25)
    assert (listed.plans[0].list_price_micros_per_hour, listed.free_month.plan_id) == (18000, "plan20")
    assert listed.tiers[0].sizes == ["tiny", "small"]
    assert not hasattr(listed.tiers[0], "burst") and not hasattr(listed.tiers[0], "sandboxes_at_once")
    assert (script.sent[0].method, script.sent[0].url.path) == ("GET", "/v1/billing/plans")
    read = agental.billing()
    assert read.plan is not None and read.plan.included_micros == 500_000_000
    assert read.top_up_micros == 215_000_000 and not hasattr(read, "discount")
    assert read.auto_reload is not None and (read.auto_reload.dollars, read.can_auto_reload) == (50, True)
    assert (script.sent[1].method, script.sent[1].url.path) == ("GET", "/v1/billing")

    bought = agental.checkout(top_up=777)
    assert (bought.url, bought.changed) == ("https://pay.example/topup777", False)
    assert (script.sent[2].method, script.sent[2].url.path, script.body(2)) == ("POST", "/v1/billing/checkout", {"topUp": 777})
    # A checkout whose answer is lost is not sent again: a plan change is made at once.
    with pytest.raises(AgentalConnectionError):
        agental.checkout(plan="plan100")
    assert len(script.sent) == 4
    assert agental.checkout(plan="plan100").changed is True
    assert script.body(4) == {"plan": "plan100"}
    with pytest.raises(ValueError):
        agental.checkout(plan="plan100", top_up=50)
    assert len(script.sent) == 5


def test_the_auto_reload_is_set_and_turned_off_by_one_call_which_sends_the_dollars_or_none() -> None:
    agental, script = scripted(answer(200, {"autoReload": {"dollars": 50}}), answer(200, {"autoReload": None}))
    on = agental.set_auto_reload(50)
    assert on.auto_reload is not None and on.auto_reload.dollars == 50
    assert (script.sent[0].method, script.sent[0].url.path, script.body(0)) == ("PUT", "/v1/billing/auto-reload", {"dollars": 50})
    assert agental.set_auto_reload(None).auto_reload is None
    assert script.body(1) == {"dollars": None}


def test_a_desktop_is_turned_on_with_the_screen_it_is_given_read_and_turned_off() -> None:
    on = {"enabled": True, "enabledAt": "2026-10-14T08:00:00.000Z", "running": True, "width": 1280, "height": 800, "display": ":0", "url": "https://3f9c2a17b8d04e6a51c0.agental.site/vnc.html?autoconnect=1&resize=scale&reconnect=1"}
    off = {"enabled": False, "enabledAt": None, "running": False, "width": None, "height": None, "display": None, "url": None}
    agental, script = scripted(answer(200, sandbox()), answer(200, on), answer(200, {**on, "width": 1920, "height": 1080}), answer(200, on), answer(200, off), refusal(409, "desktop_unavailable"))
    box = agental.sandboxes.get(ID)

    # With no size, no body is sent, and the API gives the screen the size it has or a new one's.
    assert box.desktop.on().url == on["url"]
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("PUT", f"/v1/sandboxes/{ID}/desktop", b"")
    assert box.desktop.on(width=1920, height=1080).width == 1920
    assert script.body(2) == {"width": 1920, "height": 1080}
    # Half a size is no size, and is refused before anything is sent.
    with pytest.raises(ValueError):
        box.desktop.on(width=1920)

    assert box.desktop.get().display == ":0"
    assert script.sent[3].method == "GET"
    assert box.desktop.off().enabled is False
    assert script.sent[4].method == "DELETE"

    # A machine that has no desktop is said as the API said it.
    with pytest.raises(ApiError) as refused:
        box.desktop.on()
    assert refused.value.code == "desktop_unavailable"
    assert len(script.sent) == 6


def test_a_browser_is_turned_on_with_or_without_a_window_read_and_turned_off() -> None:
    on = {"enabled": True, "enabledAt": "2026-10-14T08:00:00.000Z", "running": True, "headless": True, "cdpUrl": "wss://3f9c2a17b8d04e6a51c0.agental.site/devtools/browser", "localCdpUrl": "http://127.0.0.1:9222"}
    off = {"enabled": False, "enabledAt": None, "running": False, "headless": None, "cdpUrl": None, "localCdpUrl": None}
    agental, script = scripted(answer(200, sandbox()), answer(200, on), answer(200, {**on, "headless": False}), answer(200, on), answer(200, off), refusal(409, "desktop_not_running"))
    box = agental.sandboxes.get(ID)

    # With nothing said, no body is sent, and the API says whether Chrome has a window.
    assert box.browser.on().cdp_url == on["cdpUrl"]
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("PUT", f"/v1/sandboxes/{ID}/browser", b"")
    assert box.browser.on(headless=False).headless is False
    assert script.body(2) == {"headless": False}

    assert box.browser.get().local_cdp_url == "http://127.0.0.1:9222"
    assert script.sent[3].method == "GET"
    assert box.browser.off().enabled is False
    assert script.sent[4].method == "DELETE"

    # A browser that cannot be on the desktop is said as the API said it.
    with pytest.raises(ApiError) as refused:
        box.browser.on(headless=False)
    assert refused.value.code == "desktop_not_running"
    assert len(script.sent) == 6


def test_the_screen_is_pictured_as_bytes_and_the_pointer_and_keyboard_are_each_used_by_one_call_never_sent_twice() -> None:
    png = b"\x89PNG\r\n\x1a\n\x01\x02\x03"
    shot = {"mediaType": "image/png", "width": 1280, "height": 800, "base64": base64.b64encode(png).decode()}
    agental, script = scripted(answer(200, sandbox()), answer(200, shot), *[answer(204) for _ in range(7)], httpx.Response(502, text="<html>bad gateway</html>"))
    box = agental.sandboxes.get(ID)
    at = f"/v1/sandboxes/{ID}/desktop"

    assert box.desktop.screenshot() == png
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].url.query) == ("GET", f"{at}/screenshot", b"encoding=base64")

    box.desktop.click(640, 400)
    box.desktop.click(640, 400, button="right", count=2)
    box.desktop.move(0, 799)
    box.desktop.drag((10, 20), (300, 400))
    box.desktop.scroll(640, 400, dy=-3)
    box.desktop.type("hello, world")
    box.desktop.key("ctrl+l")
    assert [(sent.method, sent.url.path[len(at) :], json.loads(sent.content)) for sent in script.sent[2:]] == [
        ("POST", "/click", {"x": 640, "y": 400, "button": "left", "count": 1}),
        ("POST", "/click", {"x": 640, "y": 400, "button": "right", "count": 2}),
        ("POST", "/move", {"x": 0, "y": 799}),
        ("POST", "/drag", {"from": {"x": 10, "y": 20}, "to": {"x": 300, "y": 400}, "button": "left"}),
        ("POST", "/scroll", {"x": 640, "y": 400, "dx": 0, "dy": -3}),
        ("POST", "/type", {"text": "hello, world"}),
        ("POST", "/key", {"keys": "ctrl+l"}),
    ]

    # A click a proxy cut off may have been made, so it is not made again.
    with pytest.raises(ProxyError):
        box.desktop.click(1, 1)
    assert len(script.sent) == 10


def test_a_picture_is_asked_for_as_the_options_say_the_clipboard_is_read_and_written_and_links_are_made_listed_and_taken_away() -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    shot = {"mediaType": "image/jpeg", "width": 320, "height": 200, "region": {"x": 100, "y": 50, "width": 640, "height": 400}, "base64": base64.b64encode(jpeg).decode()}
    link = {"id": "dln_4f1c2a9b0d3e7c55", "access": "view", "url": "https://8c41e07d2b9f6a1355de.routes.test/vnc.html?view_only=1", "createdAt": "2026-10-14T10:00:00.000Z", "expiresAt": "2026-10-14T10:10:00.000Z", "cutOnExpiry": True}
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, shot),
        answer(200, shot),
        answer(200, {"text": "copied — 世界"}),
        answer(204),
        answer(201, link),
        answer(200, {"links": [link]}),
        answer(204),
        httpx.Response(502, text="<html>bad gateway</html>"),
    )
    box = agental.sandboxes.get(ID)
    at = f"/v1/sandboxes/{ID}/desktop"

    assert box.desktop.screenshot(format="jpeg", quality=60, region=(100, 50, 640, 400), scale=0.5, cursor=False) == jpeg
    asked = script.sent[1].url
    assert asked.path == f"{at}/screenshot"
    assert dict(asked.params) == {"encoding": "base64", "format": "jpeg", "quality": "60", "scale": "0.5", "cursor": "false", "x": "100", "y": "50", "width": "640", "height": "400"}
    described = box.desktop.screenshot_base64(format="jpeg")
    assert (described.media_type, described.width, described.region.x, described.region.width) == ("image/jpeg", 320, 100, 640)

    assert box.desktop.clipboard() == "copied — 世界"
    assert (script.sent[3].method, script.sent[3].url.path) == ("GET", f"{at}/clipboard")
    box.desktop.set_clipboard("a long text")
    assert (script.sent[4].method, script.sent[4].url.path, json.loads(script.sent[4].content)) == ("PUT", f"{at}/clipboard", {"text": "a long text"})

    made = box.desktop.create_link("view")
    assert (made.id, made.access, made.cut_on_expiry) == (link["id"], "view", True)
    assert (script.sent[5].method, script.sent[5].url.path, json.loads(script.sent[5].content)) == ("POST", f"{at}/links", {"access": "view"})
    assert [each.id for each in box.desktop.links()] == [link["id"]]
    box.desktop.delete_link(link["id"])
    assert (script.sent[7].method, script.sent[7].url.path) == ("DELETE", f"{at}/links/{link['id']}")

    # A link a proxy cut off may have been made, so it is not made again.
    with pytest.raises(ProxyError):
        box.desktop.create_link("control", expires_in=3600, cut_on_expiry=False)
    assert json.loads(script.sent[8].content) == {"access": "control", "expiresIn": 3600, "cutOnExpiry": False}
    assert len(script.sent) == 9


def test_a_client_with_no_key_says_where_one_comes_from(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTAL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTAL_API_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(AgentalError, match="AGENTAL_API_KEY.*npx agental@latest"):
        Agental()
    monkeypatch.setenv("AGENTAL_API_KEY", "key_from_env")
    with Agental() as agental:
        assert agental.sandboxes is not None


def test_with_no_key_given_a_client_uses_the_agental_commands_sign_in_for_that_api_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTAL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTAL_API_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "agental").mkdir()
    (tmp_path / "agental" / "credentials.json").write_text(json.dumps({"apiUrl": "http://127.0.0.1:8742", "apiKey": "ag_signed_in_here"}))
    from agental import _core

    kept = _core.settings(None, None, 3, 1.0, 2.0)
    assert (kept.base_url, kept.api_key) == ("http://127.0.0.1:8742", "ag_signed_in_here")
    # A key given is the one used, at agental.io unless the API is named too.
    given = _core.settings("ag_given", None, 3, 1.0, 2.0)
    assert (given.base_url, given.api_key) == ("https://agental.io", "ag_given")
    # The kept key goes to its own API and no other.
    monkeypatch.setenv("AGENTAL_API_URL", "https://other.example")
    with pytest.raises(AgentalError, match="No API key"):
        Agental()


def test_a_webhook_is_taken_for_agentals_only_with_the_signature_its_secret_gives_its_body() -> None:
    body = json.dumps({"event": "sandbox.stopped", "data": {"id": "sbx_1"}})
    signature = "sha256=" + hmac.new(b"whsec_1", body.encode(), hashlib.sha256).hexdigest()
    assert verify_webhook(body, signature, "whsec_1")
    assert verify_webhook(body.encode(), signature, "whsec_1")
    assert not verify_webhook(body + " ", signature, "whsec_1")
    assert not verify_webhook(body, signature, "whsec_2")
    assert not verify_webhook(body, signature.removeprefix("sha256="), "whsec_1")
    assert not verify_webhook(body, None, "whsec_1")


def test_the_client_that_awaits_does_the_same() -> None:
    script = Script(
        answer(202, {"sandbox": sandbox(status="creating"), "operation": {}}),
        answer(201, sandbox()),
        answer(200, {"exitCode": 0, "stdout": "a\n", "stderr": "", "truncated": False}),
        stream('event: stdout\ndata: {"data":"one"}\n\nevent: exit\ndata: {"exitCode":0}\n\n'),
        refusal(409, "sandbox_migrating"),
        answer(200, {"path": "a.txt", "bytes": 2}),
        answer(204),
    )

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            box = await agental.sandboxes.create("agent-1")
            assert box.status == "running"
            assert (await box.exec("echo a")).stdout == "a\n"
            assert [event async for event in box.exec_stream("echo one")] == [ExecEvent("stdout", "one"), ExecEvent("exit", exit_code=0)]
            assert (await box.files.write("a.txt", "hi")).bytes == 2
            await box.delete()

    asyncio.run(main())
    assert len(script.sent) == 7
    assert script.sent[0].headers["idempotency-key"] == script.sent[1].headers["idempotency-key"]


def template(**changes: Any) -> dict[str, Any]:
    return {
        "id": "tpl_0000000000000002",
        "name": "coding-agent",
        "default": False,
        "start": {"size": "small", "snapshotId": None},
        "variables": ["API_TOKEN"],
        "hidden": [],
        "files": [".npmrc"],
        "network": {"outbound": "open", "allow": []},
        "revision": 3,
        "sandboxes": 1,
        "createdAt": "2026-10-14T00:00:00.000Z",
        "updatedAt": "2026-10-14T00:00:00.000Z",
        **changes,
    }


def on_template(**changes: Any) -> dict[str, Any]:
    return {"sandboxId": ID, "templateId": "tpl_0000000000000002", "name": "coding-agent", "default": False, "upToDate": False, "takenAt": "", "variables": [], "hidden": [], "files": [], **changes}


def test_a_sandbox_is_made_from_a_template_and_put_on_another() -> None:
    listed = {"templates": [template(id="tpl_0000000000000001", name="default", default=True), template()]}
    agental, script = scripted(
        answer(200, listed),
        answer(201, sandbox(templateId="tpl_0000000000000002")),
        answer(200, on_template()),
        answer(200, on_template(templateId="tpl_0000000000000001", name="default", default=True, upToDate=True, variables=["TOKEN"])),
        answer(200, listed),
    )
    found = agental.templates.find("coding-agent")
    assert found is not None and (found.name, found.is_default, found.info.start.size) == ("coding-agent", False, "small")
    made = agental.sandboxes.create("on-it", template=found)
    assert script.body(1) == {"name": "on-it", "templateId": "tpl_0000000000000002"}
    assert made.info.template_id == "tpl_0000000000000002"
    assert made.template().up_to_date is False
    assert (script.sent[2].method, script.sent[2].url.path) == ("GET", f"/v1/sandboxes/{ID}/template")
    moved = made.set_template("tpl_0000000000000001")
    assert script.body(3) == {"templateId": "tpl_0000000000000001"}
    assert (moved.up_to_date, moved.variables, made.info.template_id) == (True, ["TOKEN"], "tpl_0000000000000001")
    assert agental.templates.default().id == "tpl_0000000000000001"


def test_secrets_are_read_one_at_a_time_and_a_file_is_named_by_its_path_in_the_query() -> None:
    agental, script = scripted(
        answer(201, template()),
        answer(200, {"names": ["API_TOKEN"]}),
        answer(200, {"name": "API_TOKEN", "value": "s3cret"}),
        answer(200, {"name": "API_TOKEN", "value": ""}),
        answer(204),
        answer(200, {"paths": [".config/gh/hosts.yml"]}),
        answer(200, {"path": ".config/gh/hosts.yml", "text": "token: x"}),
        answer(200, {"path": ".config/gh/hosts.yml", "text": "token: y"}),
        answer(204),
        answer(200, sandbox()),
        answer(200, {"name": "OWN", "value": "mine"}),
    )
    made = agental.templates.create("coding-agent")
    assert (script.sent[0].method, script.sent[0].url.path, script.body(0)) == ("POST", "/v1/templates", {"name": "coding-agent"})
    base = "/v1/templates/tpl_0000000000000002/secrets"
    assert made.secrets.variables() == ["API_TOKEN"]
    assert made.secrets.get("API_TOKEN") == "s3cret"
    assert script.sent[2].url.path == f"{base}/variables/API_TOKEN"
    made.secrets.set("API_TOKEN", "")
    assert (script.sent[3].method, script.body(3)) == ("PUT", {"value": ""})
    made.secrets.delete("API_TOKEN")
    assert (script.sent[4].method, script.sent[4].url.path) == ("DELETE", f"{base}/variables/API_TOKEN")
    assert made.secrets.files() == [".config/gh/hosts.yml"]
    assert made.secrets.read_file(".config/gh/hosts.yml") == "token: x"
    assert (script.sent[6].url.path, script.sent[6].url.params["path"]) == (f"{base}/files", ".config/gh/hosts.yml")
    assert made.secrets.write_file("./.config/gh/hosts.yml", "token: y") == ".config/gh/hosts.yml"
    made.secrets.delete_file(".config/gh/hosts.yml")
    assert (script.sent[8].method, script.sent[8].url.params["path"]) == ("DELETE", ".config/gh/hosts.yml")

    box = agental.sandboxes.get(ID)
    assert box.secrets.get("OWN") == "mine"
    assert script.sent[10].url.path == f"/v1/sandboxes/{ID}/secrets/variables/OWN"


def test_a_variable_is_kept_out_of_the_sandbox_by_setting_it_with_hosts_its_hosts_are_changed_alone_and_its_value_is_never_given_back() -> None:
    stand_in = "agental_hidden_7hq2xk9mw4rz3st8vn1bq5c0dle6fy2p"
    kept = {"name": "GITHUB_TOKEN", "hidden": True, "hosts": ["api.github.com", "github.com"], "standIn": stand_in}
    listed = {"names": ["API_TOKEN", "GITHUB_TOKEN"], "hidden": ["GITHUB_TOKEN"]}
    agental, script = scripted(
        answer(200, template(variables=["API_TOKEN", "GITHUB_TOKEN"], hidden=["GITHUB_TOKEN"])),
        httpx.ConnectError("connection refused"),
        answer(200, kept),
        answer(200, listed),
        answer(200, listed),
        answer(200, kept),
        answer(200, kept),
        answer(200, {"name": "API_TOKEN", "hidden": False, "value": "s3cret"}),
        answer(200, {**kept, "hosts": ["api.github.com"]}),
        refusal(409, "not_hidden"),
        refusal(400, "invalid_host"),
        refusal(501, "hidden_variables_unavailable"),
        answer(200, {"name": "GITHUB_TOKEN", "hidden": False, "value": "ghp_plain"}),
        answer(200, sandbox()),
        answer(200, {**kept, "name": "OWN"}),
    )
    made = agental.templates.get("tpl_0000000000000002")
    assert made.info.hidden == ["GITHUB_TOKEN"]
    base = "/v1/templates/tpl_0000000000000002/secrets"

    # The value goes with its hosts, and what comes back is its stand-in, never the value. One whose answer is lost is
    # sent again: the stand-in stays the same when the value is replaced.
    set_out = made.secrets.set("GITHUB_TOKEN", "ghp_example", hosts=["api.github.com", "github.com"])
    assert set_out == HiddenVariable("GITHUB_TOKEN", True, ["api.github.com", "github.com"], stand_in)
    assert (script.sent[1].method, script.sent[1].url.path, script.body(1)) == ("PUT", f"{base}/variables/GITHUB_TOKEN", {"value": "ghp_example", "hosts": ["api.github.com", "github.com"]})
    assert script.body(2) == script.body(1)

    assert made.secrets.variables() == ["API_TOKEN", "GITHUB_TOKEN"]
    assert made.secrets.hidden() == ["GITHUB_TOKEN"]

    # One variable is either of two kinds, and hidden says which.
    read = made.secrets.variable("GITHUB_TOKEN")
    assert isinstance(read, HiddenVariable) and read.hidden is True
    assert (read.hosts, read.stand_in, hasattr(read, "value")) == (["api.github.com", "github.com"], stand_in, False)
    # get() gives a value, and a variable kept out has none to give.
    with pytest.raises(AgentalError, match="GITHUB_TOKEN is kept out of the sandbox") as raised:
        made.secrets.get("GITHUB_TOKEN")
    assert not isinstance(raised.value, ApiError)
    assert made.secrets.variable("API_TOKEN") == PlainVariable("API_TOKEN", False, "s3cret")

    # Its hosts alone: the value is not sent again.
    assert made.secrets.set_hosts("GITHUB_TOKEN", ["api.github.com"]).hosts == ["api.github.com"]
    assert (script.sent[8].method, script.sent[8].url.path, script.body(8)) == ("PUT", f"{base}/variables/GITHUB_TOKEN/hosts", {"hosts": ["api.github.com"]})

    # A plain variable has no hosts to change, a host that cannot be one is refused, and so are hosts where variables
    # cannot be kept out: each as the API said it.
    with pytest.raises(ApiError) as plain:
        made.secrets.set_hosts("API_TOKEN", ["api.github.com"])
    assert (plain.value.status, plain.value.code) == (409, "not_hidden")
    with pytest.raises(ApiError) as wide:
        made.secrets.set("WIDE", "x", hosts=["*.com"])
    assert (wide.value.status, wide.value.code) == (400, "invalid_host")
    with pytest.raises(ApiError) as unoffered:
        made.secrets.set("GITHUB_TOKEN", "x", hosts=["api.github.com"])
    assert (unoffered.value.status, unoffered.value.code) == (501, "hidden_variables_unavailable")

    # Set without hosts it is plain from then on, and its commands read its value.
    assert made.secrets.set("GITHUB_TOKEN", "ghp_plain") == PlainVariable("GITHUB_TOKEN", False, "ghp_plain")
    assert script.body(12) == {"value": "ghp_plain"}

    # A sandbox's own the same way.
    box = agental.sandboxes.get(ID)
    assert box.secrets.set("OWN", "mine", hosts=["api.github.com"]).stand_in == stand_in
    assert (script.sent[14].url.path, script.body(14)) == (f"/v1/sandboxes/{ID}/secrets/variables/OWN", {"value": "mine", "hosts": ["api.github.com"]})
    assert len(script.sent) == 15


def test_the_client_that_awaits_keeps_a_variable_out_of_the_sandbox_the_same() -> None:
    kept = {"name": "GITHUB_TOKEN", "hidden": True, "hosts": ["api.github.com"], "standIn": "agental_hidden_7hq2xk9mw4rz3st8vn1bq5c0dle6fy2p"}
    script = Script(
        answer(200, template()),
        answer(200, kept),
        answer(200, {"names": ["GITHUB_TOKEN"], "hidden": ["GITHUB_TOKEN"]}),
        answer(200, kept),
        answer(200, {**kept, "hosts": ["github.com"]}),
    )

    async def main() -> None:
        async with AsyncAgental("key_test", base_url="https://api.test/", retry_delay=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(script))) as agental:
            made = await agental.templates.get("tpl_0000000000000002")
            assert (await made.secrets.set("GITHUB_TOKEN", "ghp_example", hosts=["api.github.com"])).stand_in == kept["standIn"]
            assert await made.secrets.hidden() == ["GITHUB_TOKEN"]
            with pytest.raises(AgentalError, match="kept out of the sandbox"):
                await made.secrets.get("GITHUB_TOKEN")
            assert (await made.secrets.set_hosts("GITHUB_TOKEN", ["github.com"])).hosts == ["github.com"]

    asyncio.run(main())
    assert [(each.method, each.url.path) for each in script.sent][-1] == ("PUT", "/v1/templates/tpl_0000000000000002/secrets/variables/GITHUB_TOKEN/hosts")


def test_a_variable_from_an_api_that_does_not_say_whether_it_is_kept_out_is_plain() -> None:
    agental, _ = scripted(answer(200, template()), answer(200, {"name": "API_TOKEN", "value": "s3cret"}), answer(200, {"names": ["API_TOKEN"]}))
    made = agental.templates.get("tpl_0000000000000002")
    assert made.secrets.variable("API_TOKEN") == PlainVariable("API_TOKEN", False, "s3cret")
    assert made.secrets.hidden() == []


def test_a_templates_start_sandboxes_and_apply_ask_for_what_the_api_needs() -> None:
    agental, script = scripted(
        answer(200, template()),
        answer(200, {"size": "large", "snapshotId": "snap_1", "ttlSeconds": None}),
        answer(200, {"sandboxes": [{"id": ID, "name": "agent-1", "status": "running", "upToDate": False, "takenAt": "", "allow": []}]}),
        answer(200, {"templateId": "tpl_0000000000000002", "revision": 4, "applied": [ID], "failed": []}),
        answer(200, {"templateId": "tpl_0000000000000002", "revision": 4, "applied": [], "failed": []}),
        refusal(409, "template_in_use"),
    )
    made = agental.templates.get("tpl_0000000000000002")
    start = made.set_start("large", snapshot_id="snap_1")
    assert (start.size, start.snapshot_id, made.info.start.size) == ("large", "snap_1", "large")
    assert (script.sent[1].method, script.body(1)) == ("PUT", {"size": "large", "snapshotId": "snap_1"})
    assert made.sandboxes()[0].up_to_date is False
    assert made.apply([ID]).applied == [ID]
    assert script.body(3) == {"sandboxIds": [ID]}
    made.apply()
    assert (script.sent[4].url.path, script.body(4)) == ("/v1/templates/tpl_0000000000000002/apply", {})
    with pytest.raises(ApiError) as refused:
        made.delete()
    assert refused.value.code == "template_in_use"


def test_a_templates_start_is_sent_whole_with_ttl_seconds_when_it_is_given_and_without_when_it_is_left_out() -> None:
    start = "/v1/templates/tpl_0000000000000002/start"
    agental, script = scripted(
        answer(200, template()),
        answer(200, {"size": "medium", "snapshotId": None, "ttlSeconds": 7200}),
        answer(200, {"size": "medium", "snapshotId": "snap_1", "ttlSeconds": None}),
        refusal(400, "ttl_too_long"),
        answer(200, {"size": "small", "snapshotId": None, "ttlSeconds": None}),
    )
    made = agental.templates.get("tpl_0000000000000002")

    # Given, it is sent, and so is None: the deployment's default.
    timed = made.set_start("medium", ttl_seconds=7200)
    assert (timed.size, timed.snapshot_id, timed.ttl_seconds, made.info.start.ttl_seconds) == ("medium", None, 7200, 7200)
    assert (script.sent[1].method, script.sent[1].url.path, script.body(1)) == ("PUT", start, {"size": "medium", "ttlSeconds": 7200})
    made.set_start("medium", snapshot_id="snap_1", ttl_seconds=None)
    assert script.body(2) == {"size": "medium", "snapshotId": "snap_1", "ttlSeconds": None}

    # Over the deployment's ceiling: refused as the API said it, not sent again, and the start as it was.
    with pytest.raises(ApiError) as refused:
        made.set_start("medium", ttl_seconds=2_592_000)
    assert (refused.value.status, refused.value.code) == (400, "ttl_too_long")
    assert (made.info.start.snapshot_id, made.info.start.ttl_seconds) == ("snap_1", None)

    # Left out, it is not sent, and the API takes the deployment's default.
    assert made.set_start("small").ttl_seconds is None
    assert (script.sent[4].method, script.body(4)) == ("PUT", {"size": "small"})
    # Nothing is read first.
    assert [each.method for each in script.sent[1:]] == ["PUT", "PUT", "PUT", "PUT"]


def test_a_templates_outbound_rule_is_read_and_replaced_by_calls_of_its_own_and_an_apply_says_which_sandboxes_did_not_take_it() -> None:
    listed = {"outbound": "allowlist", "allow": ["203.0.113.7", "198.51.100.0/24"]}
    agental, script = scripted(
        answer(200, template()),
        answer(200, {"outbound": "open", "allow": []}),
        httpx.ConnectError("connection refused"),
        answer(200, listed),
        answer(200, {"outbound": "blocked", "allow": []}),
        refusal(409, "too_many_addresses"),
        refusal(501, "network_policy_unavailable"),
        answer(200, {"sandboxes": [{"id": ID, "name": "agent-1", "status": "running", "upToDate": False, "takenAt": "", "allow": ["192.0.2.99"]}]}),
        answer(200, {"templateId": "tpl_0000000000000002", "revision": 5, "applied": [], "failed": [{"sandboxId": ID, "error": "the ACL could not be written"}]}),
    )
    made = agental.templates.get("tpl_0000000000000002")
    assert (made.info.network.outbound, made.info.network.allow) == ("open", [])
    rule = made.network()
    assert (rule.outbound, rule.allow) == ("open", [])
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].content) == ("GET", "/v1/templates/tpl_0000000000000002/network", b"")

    # The whole rule is sent. One whose answer is lost is sent again: sending the rule it has changes nothing.
    held = made.set_network("allowlist", allow=["203.0.113.7", "198.51.100.0/24"])
    assert (held.outbound, held.allow) == ("allowlist", ["203.0.113.7", "198.51.100.0/24"])
    assert (script.sent[2].method, script.sent[2].url.path, script.body(2)) == ("PUT", "/v1/templates/tpl_0000000000000002/network", listed)
    assert (script.sent[3].method, script.body(3)) == ("PUT", listed)
    # What the template says of itself follows, without another read.
    assert made.info.network.allow == ["203.0.113.7", "198.51.100.0/24"]
    assert made.set_network("blocked").outbound == "blocked"
    assert script.body(4) == {"outbound": "blocked"}

    # A rule that comes to too many addresses with a sandbox's own, and one asked for where sandboxes are not held to
    # rules, are said as the API said them, and the template says what it said before.
    with pytest.raises(ApiError) as crowded:
        made.set_network("allowlist", allow=["203.0.113.7"])
    assert (crowded.value.status, crowded.value.code) == (409, "too_many_addresses")
    with pytest.raises(ApiError) as unheld:
        made.set_network("allowlist", allow=["203.0.113.7"])
    assert (unheld.value.status, unheld.value.code) == (501, "network_policy_unavailable")
    assert made.info.network.outbound == "blocked"

    # Each sandbox on it with its own addresses, and an apply that names the ones whose server did not take the rule.
    assert made.sandboxes()[0].allow == ["192.0.2.99"]
    applied = made.apply()
    assert applied.applied == []
    assert [(each.sandbox_id, each.error) for each in applied.failed] == [(ID, "the ACL could not be written")]
    assert len(script.sent) == 9


def hook(**changes: Any) -> dict[str, Any]:
    return {
        "id": "wh_5b0e1d7c93a24f68",
        "url": "https://example.com/hooks/agental",
        "events": ["sandbox.stopped"],
        "description": "build bot",
        "createdAt": "2026-10-15T09:00:00.000Z",
        "updatedAt": "2026-10-15T09:00:00.000Z",
        "deliveries": [
            {"id": "dlv_1", "event": "sandbox.stopped", "status": "failed", "httpStatus": None, "error": "No answer.", "createdAt": "2026-10-15T09:01:00.000Z", "eventId": "evt_a", "attempt": 1},
        ],
        **changes,
    }


def test_webhooks_are_listed_added_read_changed_given_a_new_secret_and_deleted_by_calls_of_their_own() -> None:
    agental, script = scripted(
        answer(200, {"webhooks": [hook()]}),
        answer(201, hook(signingSecret="whsec_one", deliveries=[])),
        answer(200, hook()),
        answer(200, hook(events=["*"])),
        answer(200, hook(signingSecret="whsec_two")),
        answer(204),
    )

    listed = agental.list_webhooks()
    assert [each.id for each in listed] == ["wh_5b0e1d7c93a24f68"]
    # A try is read as it was made: nothing answered it, and it was the first try of its event.
    tried = listed[0].deliveries[0]
    assert (tried.http_status, tried.error, tried.event_id, tried.attempt) == (None, "No answer.", "evt_a", 1)
    assert listed[0].signing_secret is None
    assert (script.sent[0].method, script.sent[0].url.path) == ("GET", "/v1/webhooks")

    # The secret is in the answer to the create, the one time it is given.
    made = agental.create_webhook("https://example.com/hooks/agental", events=["sandbox.stopped"], description="build bot")
    assert made.signing_secret == "whsec_one"
    assert (script.sent[1].method, script.sent[1].url.path, script.body(1)) == (
        "POST",
        "/v1/webhooks",
        {"url": "https://example.com/hooks/agental", "events": ["sandbox.stopped"], "description": "build bot"},
    )

    assert agental.get_webhook("wh_5b0e1d7c93a24f68").description == "build bot"
    assert (script.sent[2].method, script.sent[2].url.path) == ("GET", "/v1/webhooks/wh_5b0e1d7c93a24f68")

    # Only what is changed is sent.
    assert agental.update_webhook("wh_5b0e1d7c93a24f68", events=["*"]).events == ["*"]
    assert (script.sent[3].method, script.sent[3].url.path, script.body(3)) == ("PATCH", "/v1/webhooks/wh_5b0e1d7c93a24f68", {"events": ["*"]})

    assert agental.rotate_webhook_secret("wh_5b0e1d7c93a24f68").signing_secret == "whsec_two"
    assert (script.sent[4].method, script.sent[4].url.path) == ("POST", "/v1/webhooks/wh_5b0e1d7c93a24f68/rotate")

    agental.delete_webhook("wh_5b0e1d7c93a24f68")
    assert (script.sent[5].method, script.sent[5].url.path) == ("DELETE", "/v1/webhooks/wh_5b0e1d7c93a24f68")
    assert len(script.sent) == 6


def api_key(**changes: Any) -> dict[str, Any]:
    return {
        "id": "key_38b536dc2eabdb52",
        "name": "ci-github",
        "prefix": "ag_1f2e3d4c5",
        "createdAt": "2026-10-16T09:00:00.000Z",
        "seesAll": False,
        "preset": "ci",
        "actions": ["sandbox.read", "sandbox.create", "exec"],
        "sandboxIds": [],
        "templateIds": ["tpl_coding_agent"],
        "expiresAt": None,
        "allowFrom": ["203.0.113.0/24"],
        "lastUsedAt": "2026-10-16T09:30:00.000Z",
        "lastUsedFrom": "203.0.113.7",
        **changes,
    }


def test_api_keys_are_listed_made_read_given_new_addresses_counted_and_deleted_by_calls_of_their_own() -> None:
    key_usage = {
        "apiKeyId": "key_38b536dc2eabdb52",
        "createdAt": "2026-10-16T09:00:00.000Z",
        "ageMs": 3_600_000,
        "since": "2026-10-16T00:00:00.000Z",
        "until": "2026-10-16T10:00:00.000Z",
        "sandboxes": 2,
        "seconds": 1800,
        "listCostMicros": 9000,
        "currency": "usd",
        "cost": None,
        "billing": "not_configured",
    }
    mail_only = api_key(preset=None, actions=["email.read", "email.send"], sandboxIds=[ID], templateIds=[], expiresAt="2026-11-15T00:00:00.000Z")
    agental, script = scripted(
        answer(200, {"apiKeys": [api_key()]}),
        answer(201, {**api_key(), "secret": "ag_1f2e3d4c5b6a"}),
        answer(201, {**mail_only, "secret": "ag_9e8d7c6b5a4f"}),
        answer(200, api_key()),
        answer(200, api_key(allowFrom=[])),
        answer(200, key_usage),
        answer(204),
    )

    listed = agental.api_keys.list()
    assert [(each.id, each.preset, each.template_ids, each.allow_from, each.last_used_from) for each in listed] == [
        ("key_38b536dc2eabdb52", "ci", ["tpl_coding_agent"], ["203.0.113.0/24"], "203.0.113.7")
    ]
    assert (script.sent[0].method, script.sent[0].url.path) == ("GET", "/v1/api-keys")

    # The secret is in the answer to the create, the one time it is given, and only what was given is sent.
    made = agental.api_keys.create("ci-github", preset="ci", template_ids=["tpl_coding_agent"], allow_from=["203.0.113.0/24"])
    assert (made.secret, made.id) == ("ag_1f2e3d4c5b6a", "key_38b536dc2eabdb52")
    assert (script.sent[1].method, script.sent[1].url.path, script.body(1)) == (
        "POST",
        "/v1/api-keys",
        {"name": "ci-github", "preset": "ci", "templateIds": ["tpl_coding_agent"], "allowFrom": ["203.0.113.0/24"]},
    )
    # A create is never sent again by itself: a second one would be a second key.
    assert "idempotency-key" not in script.sent[1].headers

    # The actions named in place of a preset, and a datetime for when it stops working.
    made = agental.api_keys.create("mail-agent", actions=["email.read", "email.send"], sandbox_ids=[ID], expires_at=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert (made.preset, made.actions, made.sandbox_ids, made.expires_at) == (None, ["email.read", "email.send"], [ID], "2026-11-15T00:00:00.000Z")
    assert script.body(2) == {"name": "mail-agent", "actions": ["email.read", "email.send"], "sandboxIds": [ID], "expiresAt": "2026-11-15T00:00:00+00:00"}

    assert agental.api_keys.get("key_38b536dc2eabdb52").name == "ci-github"
    assert (script.sent[3].method, script.sent[3].url.path) == ("GET", "/v1/api-keys/key_38b536dc2eabdb52")

    # An empty list is anywhere.
    assert agental.api_keys.set_allow_from("key_38b536dc2eabdb52", []).allow_from == []
    assert (script.sent[4].method, script.sent[4].url.path, script.body(4)) == ("PUT", "/v1/api-keys/key_38b536dc2eabdb52/allow-from", {"allowFrom": []})

    counted = agental.api_keys.usage("key_38b536dc2eabdb52", since="2026-10-16T00:00:00Z")
    assert (counted.api_key_id, counted.since, counted.sandboxes, counted.seconds, counted.list_cost_micros) == ("key_38b536dc2eabdb52", "2026-10-16T00:00:00.000Z", 2, 1800, 9000)
    assert (script.sent[5].method, script.sent[5].url.path, dict(script.sent[5].url.params)) == (
        "GET",
        "/v1/api-keys/key_38b536dc2eabdb52/usage",
        {"since": "2026-10-16T00:00:00Z"},
    )

    agental.api_keys.delete("key_38b536dc2eabdb52")
    assert (script.sent[6].method, script.sent[6].url.path) == ("DELETE", "/v1/api-keys/key_38b536dc2eabdb52")
    assert len(script.sent) == 7


def test_a_key_is_made_with_a_preset_or_actions_and_its_refusals_come_with_the_apis_codes() -> None:
    agental, script = scripted(refusal(401, "key_expired"), refusal(403, "key_address_forbidden"), refusal(403, "key_exceeds_caller"), refusal(400, "template_required"))

    # Both, or neither, is refused before anything is sent.
    with pytest.raises(ValueError):
        agental.api_keys.create("both", preset="ci", actions=["exec"])
    with pytest.raises(ValueError):
        agental.api_keys.create("neither")
    assert script.sent == []

    def refused(status: int, code: str, error: pytest.ExceptionInfo[ApiError]) -> bool:
        return (error.value.status, error.value.code) == (status, code)

    with pytest.raises(ApiError) as expired:
        agental.api_keys.list()
    assert refused(401, "key_expired", expired)
    with pytest.raises(ApiError) as elsewhere:
        agental.api_keys.get("key_38b536dc2eabdb52")
    assert refused(403, "key_address_forbidden", elsewhere)
    with pytest.raises(ApiError) as stronger:
        agental.api_keys.create("wider", preset="admin")
    assert refused(403, "key_exceeds_caller", stronger)
    # A key limited to several templates, none of them the default, has to name one to make a sandbox.
    with pytest.raises(ApiError) as unnamed:
        agental.sandboxes.create("where")
    assert refused(400, "template_required", unnamed)
    assert len(script.sent) == 4


def test_the_client_that_awaits_makes_and_lists_keys_the_same() -> None:
    script = Script(answer(201, {**api_key(), "secret": "ag_1f2e3d4c5b6a"}), answer(200, {"apiKeys": [api_key()]}))

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            assert (await agental.api_keys.create("ci-github", preset="ci")).secret == "ag_1f2e3d4c5b6a"
            assert [each.name for each in await agental.api_keys.list()] == ["ci-github"]

    asyncio.run(main())
    assert script.body(0) == {"name": "ci-github", "preset": "ci"}
    assert len(script.sent) == 2


def test_a_create_sends_one_region_or_the_regions_to_try_and_a_full_regions_refusal_says_where_there_is_room() -> None:
    full = answer(409, {"error": {"code": "capacity_exceeded", "message": "No room in eu.", "regionsWithRoom": ["us"]}})
    listed = answer(200, {"regions": [{"name": "eu", "label": "Europe", "about": "Servers in the EEA or the UK.", "default": True, "sizesWithRoom": ["small"]}]})
    agental, script = scripted(answer(201, sandbox()), answer(201, sandbox()), full, listed)
    agental.sandboxes.create("one", region="us")
    assert script.body(0)["region"] == "us"
    agental.sandboxes.create("two", region=["eu", "us"])
    assert ("region" in script.body(1), script.body(1)["regions"]) == (False, ["eu", "us"])
    with pytest.raises(ApiError) as refused:
        agental.sandboxes.create("three", region="eu")
    assert (refused.value.code, refused.value.regions_with_room) == ("capacity_exceeded", ["us"])
    regions = agental.regions()
    assert (regions[0].name, regions[0].default, regions[0].sizes_with_room) == ("eu", True, ["small"])


def test_ssh_is_turned_on_read_and_off_and_its_keys_listed_added_and_removed() -> None:
    on = {"enabled": True, "enabledAt": "2026-10-26T08:00:00.000Z", "address": "203.0.113.7:22041", "host": "203.0.113.7", "port": 22041, "user": "root", "hostKey": "ssh-ed25519 AAAAC3Nza"}
    off = {"enabled": False, "enabledAt": None, "address": None, "host": None, "port": None, "user": None, "hostKey": None}
    key = {"id": "3q2-7wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "fingerprint": "SHA256:3q2+7w", "type": "ssh-ed25519", "comment": "me@laptop"}
    agental, script = scripted(answer(200, sandbox()), answer(200, on), answer(200, on), answer(200, {"keys": [key]}), answer(201, key), answer(204), answer(200, off))
    box = agental.sandboxes.get(ID)
    at = f"/v1/sandboxes/{ID}/ssh"

    assert box.ssh.on().port == 22041
    assert (script.sent[1].method, script.sent[1].url.path) == ("PUT", at)
    assert box.ssh.get().host_key == "ssh-ed25519 AAAAC3Nza"
    assert script.sent[2].method == "GET"
    assert [one.comment for one in box.ssh.keys()] == ["me@laptop"]
    assert (script.sent[3].method, script.sent[3].url.path) == ("GET", f"{at}/keys")
    assert box.ssh.add_key("ssh-ed25519 AAAAC3Nza me@laptop").id == key["id"]
    assert (script.sent[4].method, script.body(4)) == ("POST", {"publicKey": "ssh-ed25519 AAAAC3Nza me@laptop"})
    box.ssh.remove_key(key["id"])
    assert (script.sent[5].method, script.sent[5].url.raw_path.decode()) == ("DELETE", f"{at}/keys/3q2-7wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert box.ssh.off().enabled is False
    assert script.sent[6].method == "DELETE"


def test_a_stuck_sandbox_is_recovered_by_one_call_with_the_burst_fee_it_may_pay_and_the_one_it_paid() -> None:
    agental, script = scripted(answer(200, sandbox(status="error")), answer(200, sandbox(status="running"), **{"agental-burst-fee": "1200"}))
    box = agental.sandboxes.get(ID)
    box.recover(max_burst_fee_micros=5000)
    assert (script.sent[1].method, script.sent[1].url.path, script.sent[1].headers.get("agental-max-burst-fee")) == ("POST", f"/v1/sandboxes/{ID}/recover", "5000")
    assert (box.status, box.burst_fee_micros) == ("running", 1200)


def test_who_the_key_belongs_to_and_the_accounts_port_addresses_listed_and_hidden() -> None:
    me = {"id": "usr_1", "name": "Ian", "email": "ian@example.com", "role": "owner", "activeOrganizationId": "org_1", "closed": None}
    route = {"id": "hr_1", "sandboxId": ID, "port": 3000, "title": "port-3000", "public": True, "hidden": False, "proxyPath": "/v1/host-routes/hr_1/proxy", "registered": True, "listening": True, "proxy": "https://x.agental.site", "url": "https://x.agental.site"}
    agental, script = scripted(answer(200, me), answer(200, {"routes": [route]}), answer(200, {**route, "hidden": True}))
    assert agental.me().email == "ian@example.com"
    assert script.sent[0].url.path == "/v1/me"
    assert [one.id for one in agental.host_routes()] == ["hr_1"]
    assert script.sent[1].url.path == "/v1/host-routes"
    assert agental.hide_host_route("hr_1").hidden is True
    assert (script.sent[2].method, script.sent[2].url.path) == ("POST", "/v1/host-routes/hr_1/hide")


def test_data_retention_is_read_and_set_to_a_period_or_until_the_sandbox_is_deleted() -> None:
    kept = {
        "keepDays": 30,
        "enabled": True,
        "choices": [1, 7, 30, 90],
        "defaultKeepDays": 30,
        "lastCleared": {"at": "2026-11-10T12:00:00.000Z", "commands": 3, "conversations": 1, "events": 9},
        "retained": [{"what": "Sandbox metrics", "kept": "15 days"}],
    }
    agental, script = scripted(answer(200, kept), answer(200, {**kept, "keepDays": 7}), answer(200, {**kept, "keepDays": None, "enabled": False}))

    read = agental.get_data_retention()
    assert (read.keep_days, read.choices, read.default_keep_days) == (30, [1, 7, 30, 90], 30)
    assert read.last_cleared is not None and read.last_cleared.events == 9
    assert read.retained[0].kept == "15 days"
    assert (script.sent[0].method, str(script.sent[0].url)) == ("GET", "https://api.test/v1/data-retention")

    assert agental.set_data_retention(7).keep_days == 7
    assert (script.sent[1].method, script.body(1)) == ("PATCH", {"keepDays": 7})
    assert agental.set_data_retention(None).enabled is False
    assert script.body(2) == {"keepDays": None}


def test_a_sandbox_is_made_found_changed_and_tagged_by_its_tags_and_metadata() -> None:
    labelled = sandbox(tags=["claude", "run-7f3a"], ownTags=["run-7f3a"], metadata={"repo": "acme/web", "team": "infra"}, ownMetadata={"repo": "acme/web"})
    agental, script = scripted(
        answer(201, labelled),
        answer(200, {"sandboxes": [labelled]}),
        answer(200, labelled),
        answer(200, {**labelled, "tags": ["claude", "run-7f3a", "pr-412"]}),
        answer(200, labelled),
    )
    made = agental.sandboxes.create("agent-1", tags=["run-7f3a"], metadata={"repo": "acme/web"})
    assert script.body(0) == {"name": "agent-1", "tags": ["run-7f3a"], "metadata": {"repo": "acme/web"}}
    assert (made.tags, made.metadata, made.info.own_tags) == (["claude", "run-7f3a"], {"repo": "acme/web", "team": "infra"}, ["run-7f3a"])

    assert len(agental.sandboxes.list(tag=["run-7f3a", "claude"], metadata={"repo": "acme/web"})) == 1
    params = script.sent[1].url.params
    assert (params.get_list("tag"), params.get("metadata[repo]")) == (["run-7f3a", "claude"], "acme/web")

    made.update(tags=["run-7f3a"], metadata={"repo": None})
    assert (script.sent[2].method, script.body(2)) == ("PATCH", {"tags": ["run-7f3a"], "metadata": {"repo": None}})
    made.add_tag("pr-412")
    assert (script.sent[3].method, script.sent[3].url.path, made.tags) == ("PUT", f"/v1/sandboxes/{ID}/tags/pr-412", ["claude", "run-7f3a", "pr-412"])
    made.remove_tag("pr-412")
    assert (script.sent[4].method, script.sent[4].url.path) == ("DELETE", f"/v1/sandboxes/{ID}/tags/pr-412")


def test_a_template_is_made_with_tags_and_metadata_changed_and_tagged() -> None:
    labelled = template(tags=["team-infra"], metadata={"team": "infra"})
    agental, script = scripted(*(answer(201 if i == 0 else 200, labelled) for i in range(5)))
    made = agental.templates.create("coding-agent", tags=["team-infra"], metadata={"team": "infra"})
    assert script.body(0) == {"name": "coding-agent", "tags": ["team-infra"], "metadata": {"team": "infra"}}
    assert (made.tags, made.metadata) == (["team-infra"], {"team": "infra"})
    made.update(metadata={"team": None})
    assert (script.sent[1].method, script.body(1)) == ("PATCH", {"metadata": {"team": None}})
    made.add_tag("claude")
    assert (script.sent[2].method, script.sent[2].url.path) == ("PUT", f"/v1/templates/{made.id}/tags/claude")
    made.remove_tag("claude")
    assert (script.sent[3].method, script.sent[3].url.path) == ("DELETE", f"/v1/templates/{made.id}/tags/claude")
    made.rename("other")
    assert script.body(4) == {"name": "other"}


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "", "-x", "no spaces"])
def test_a_tag_that_is_not_one_is_refused_before_anything_is_sent_so_no_word_can_make_its_path_another(bad: str) -> None:
    agental, script = scripted(answer(201, sandbox()), answer(201, template()))
    made = agental.sandboxes.create("agent-1")
    tpl = agental.templates.create("coding-agent")
    for call in (made.remove_tag, made.add_tag, tpl.remove_tag, tpl.add_tag):
        with pytest.raises(AgentalError):
            call(bad)
    assert len(script.sent) == 2


def test_a_teams_members_and_invites_are_read_and_changed_through_organizations() -> None:
    own = {"id": "org_own", "name": "Ada", "personal": True, "role": "owner", "membersAllowed": False}
    acme = {"id": "org_acme", "name": "Acme", "personal": False, "role": "owner", "membersAllowed": True}
    cy = {"userId": "usr_cy", "name": "Cy", "email": "cy@example.com", "role": "member", "addedAt": "2026-10-31T10:00:00.000Z", "addedBy": "usr_ada"}
    invite = {
        "id": "inv_1",
        "email": "dee@example.com",
        "role": "admin",
        "status": "open",
        "invitedBy": "usr_ada",
        "createdAt": "2026-10-31T10:00:00.000Z",
        "expiresAt": "2026-11-07T10:00:00.000Z",
        "delivery": "sent",
    }
    agental, script = scripted(
        answer(200, {"organizations": [own, acme]}),
        answer(200, {"id": "usr_ada", "activeOrganizationId": "org_acme"}),
        answer(200, {"organizations": [own, acme]}),
        answer(201, {"organization": {**acme, "id": "org_beta", "name": "Beta", "createdAt": "2026-10-31T12:00:00.000Z"}, "created": True}),
        answer(200, {"members": [cy]}),
        answer(200, {"userId": "usr_cy", "role": "admin"}),
        answer(200, {"revokedKeys": 2}),
        answer(201, invite),
        answer(200, {"invites": [invite]}),
        answer(204),
    )
    assert [each.name for each in agental.organizations.list()] == ["Ada", "Acme"]
    assert agental.organizations.current().id == "org_acme"
    assert agental.organizations.create_team("Beta").id == "org_beta"
    assert script.body(3) == {"kind": "team", "name": "Beta"}
    assert agental.organizations.members("org_acme")[0].email == "cy@example.com"
    assert agental.organizations.set_role("org_acme", "usr_cy", "admin") == "admin"
    assert (script.sent[5].method, script.sent[5].url.path, script.body(5)) == ("PATCH", "/v1/organizations/org_acme/members/usr_cy", {"role": "admin"})
    assert agental.organizations.remove_member("org_acme", "usr_cy", revoke_keys=True) == 2
    assert (script.sent[6].method, str(script.sent[6].url)) == ("DELETE", "https://api.test/v1/organizations/org_acme/members/usr_cy?revokeKeys=true")
    assert agental.organizations.invite("dee@example.com", role="admin").id == "inv_1"
    assert script.body(7) == {"email": "dee@example.com", "role": "admin"}
    assert [each.id for each in agental.organizations.invites()] == ["inv_1"]
    agental.organizations.revoke_invite("inv_1")
    assert (script.sent[9].method, script.sent[9].url.path) == ("DELETE", "/v1/invites/inv_1")


def test_a_sandboxs_schedules_are_listed_made_read_changed_run_their_runs_read_and_deleted() -> None:
    run = {"id": "run_1", "scheduleId": "sch_1", "sandboxId": ID, "dueAt": "2026-11-03T16:00:00.000Z", "startedAt": None, "finishedAt": None, "outcome": "running", "reason": None, "exitCode": None, "commandId": None, "startedSandbox": False, "stoppedSandbox": None}
    schedule = {
        "id": "sch_1", "sandboxId": ID, "name": "scrape", "command": "python scrape.py", "every": "1h", "cron": None, "timeZone": "UTC",
        "timeoutMs": 600000, "ifStopped": "start", "keepMemory": False, "overlap": "skip", "enabled": True,
        "nextRunAt": "2026-11-03T17:00:00.000Z", "lastRun": run, "createdAt": "2026-11-03T16:00:00.000Z", "updatedAt": "2026-11-03T16:00:00.000Z",
    }
    agental, script = scripted(
        answer(200, sandbox()),
        answer(200, {"schedules": [schedule]}),
        answer(201, schedule),
        answer(200, schedule),
        answer(200, {**schedule, "enabled": False, "nextRunAt": None}),
        answer(202, run),
        answer(200, {"runs": [run]}),
        answer(204),
    )
    box = agental.sandboxes.get(ID)
    at = f"/v1/sandboxes/{ID}/schedules"

    listed = box.schedules.list()
    assert (listed[0].id, listed[0].last_run.outcome if listed[0].last_run else None) == ("sch_1", "running")
    assert (script.sent[1].method, script.sent[1].url.path) == ("GET", at)
    assert box.schedules.create("python scrape.py", every="1h", name="scrape", if_stopped="start", keep_memory=True, timeout=1800).every == "1h"
    assert (script.sent[2].method, script.body(2)) == ("POST", {"command": "python scrape.py", "every": "1h", "name": "scrape", "timeoutMs": 1800000, "ifStopped": "start", "keepMemory": True})
    assert box.schedules.get("sch_1").time_zone == "UTC"
    assert (script.sent[3].method, script.sent[3].url.path) == ("GET", f"{at}/sch_1")
    assert box.schedules.update("sch_1", enabled=False).next_run_at is None
    assert (script.sent[4].method, script.body(4)) == ("PATCH", {"enabled": False})
    assert box.schedules.run("sch_1").started_sandbox is False
    assert (script.sent[5].method, script.sent[5].url.path) == ("POST", f"{at}/sch_1/run")
    assert [one.id for one in box.schedules.runs("sch_1", limit=5)] == ["run_1"]
    assert (script.sent[6].method, script.sent[6].url.path, script.sent[6].url.query) == ("GET", f"{at}/sch_1/runs", b"limit=5")
    box.schedules.delete("sch_1")
    assert (script.sent[7].method, script.sent[7].url.path) == ("DELETE", f"{at}/sch_1")


def test_a_schedule_that_runs_once_is_asked_to_delete_its_sandbox_after_and_its_run_says_it_did() -> None:
    run = {"id": "run_1", "scheduleId": "sch_1", "sandboxId": ID, "dueAt": "2026-11-04T09:00:20.000Z", "startedAt": "2026-11-04T09:00:21.000Z", "finishedAt": "2026-11-04T09:00:30.000Z", "outcome": "succeeded", "reason": None, "exitCode": 0, "commandId": "cmd_1", "startedSandbox": False, "stoppedSandbox": None, "deletedSandbox": True}
    schedule = {
        "id": "sch_1", "sandboxId": ID, "name": "last", "command": "./finish.sh", "every": None, "cron": None, "at": "2026-11-04T09:00:00.000Z", "timeZone": "UTC",
        "timeoutMs": 600000, "ifStopped": "start", "keepMemory": False, "overlap": "skip", "deleteAfter": True, "enabled": True,
        "nextRunAt": None, "pausedReason": None, "lastRun": run, "createdAt": "2026-11-03T16:00:00.000Z", "updatedAt": "2026-11-03T16:00:00.000Z",
    }
    agental, script = scripted(answer(200, sandbox()), answer(201, schedule), answer(200, {"runs": [run]}))
    box = agental.sandboxes.get(ID)
    made = box.schedules.create("./finish.sh", at="2026-11-04T09:00:00Z", delete_after=True)
    assert made.delete_after is True
    assert script.body(1) == {"command": "./finish.sh", "at": "2026-11-04T09:00:00Z", "deleteAfter": True}
    assert box.schedules.runs("sch_1")[0].deleted_sandbox is True
