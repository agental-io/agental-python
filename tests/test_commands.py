"""Durable commands against answers that are scripted: started with what they run with, followed by byte offsets
across cuts, written to by offset, signaled and forgotten. The API's own side is tried in the agental
repository's tests of commands."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from agental import Agental, AgentalConnectionError, AgentalError, ApiError, AsyncAgental, CommandOutputEvent, CommandOutputFrom, CommandStdinState

ID = "sbx_0123456789abcdef"
CMD = "cmd_0123456789abcdef"
COMMANDS = f"/v1/sandboxes/{ID}/commands"
MiB = 1024 * 1024


def sandbox() -> dict[str, Any]:
    return {
        "id": ID,
        "name": "agent-1",
        "region": "main",
        "size": "small",
        "status": "running",
        "resources": {"vcpu": 1, "memoryMb": 2048, "diskGb": 10},
        "createdAt": "2026-11-24T00:00:00.000Z",
        "updatedAt": "2026-11-24T00:00:00.000Z",
        "archived": False,
        "network": {"outbound": "open", "allow": []},
    }


def command(**changes: Any) -> dict[str, Any]:
    return {
        "id": CMD,
        "sandboxId": ID,
        "pid": 42,
        "status": "running",
        "exitCode": None,
        "endReason": None,
        "signal": None,
        "paused": False,
        "launched": True,
        "cwd": "/srv/app",
        "timeoutMs": None,
        "stdoutBytes": 0,
        "stderrBytes": 0,
        "outputFrom": {"stdout": 0, "stderr": 0},
        "stdin": {"open": True, "offset": 0},
        "stdout": "",
        "stderr": "",
        "startedAt": "2026-11-24T00:00:00.000Z",
        "finishedAt": None,
        **changes,
    }


def ended(**changes: Any) -> dict[str, Any]:
    return command(**{"status": "succeeded", "exitCode": 0, "endReason": "exited", "finishedAt": "2026-11-24T00:01:00.000Z", **changes})


def answer(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status, json=body) if body is not None else httpx.Response(status)


def refusal(status: int, code: str, **details: Any) -> httpx.Response:
    return answer(status, {"error": {"code": code, "message": f"{code} it is", **details}})


def events(*said: str) -> httpx.Response:
    return httpx.Response(200, content="".join(said).encode(), headers={"content-type": "text/event-stream"})


def output(stream: str, offset: int, data: bytes) -> str:
    return f'event: output\ndata: {json.dumps({"stream": stream, "offset": offset, "data": base64.b64encode(data).decode()})}\n\n'


def gap(stream: str, start: int, to: int) -> str:
    return f'event: gap\ndata: {json.dumps({"stream": stream, "from": start, "to": to})}\n\n'


def went_on(stdout: int, stderr: int) -> str:
    return f'event: continue\ndata: {json.dumps({"stdout": stdout, "stderr": stderr})}\n\n'


def end(**changes: Any) -> str:
    return f"event: end\ndata: {json.dumps(ended(**changes))}\n\n"


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

    def query(self, index: int) -> dict[str, str]:
        return dict(self.sent[index].url.params)


def scripted(*answers: httpx.Response | Exception) -> tuple[Any, Script]:
    """A sandbox's commands, with the sandbox's own read as the first request."""
    script = Script(answer(200, sandbox()), *answers)
    client = Agental("key_test", base_url="https://api.test/", retry_delay=0, poll_interval=0, http_client=httpx.Client(transport=httpx.MockTransport(script)))
    return client.sandboxes.get(ID).commands, script


def test_a_start_sends_its_folder_variables_stdin_and_no_limit_only_when_asked() -> None:
    commands, script = scripted(
        answer(201, {"id": CMD, "pid": 42, "status": "running"}),
        answer(200, command()),
        answer(201, {"id": CMD, "pid": 42, "status": "running"}),
        answer(200, command()),
    )
    started = commands.start(["npm", "test"], no_timeout=True, cwd="/srv/app", env={"CI": "1"}, stdin=True)
    assert script.body(1) == {"command": ["npm", "test"], "timeoutMs": None, "cwd": "/srv/app", "env": {"CI": "1"}, "stdin": True}
    assert (script.sent[2].method, script.sent[2].url.path) == ("GET", f"{COMMANDS}/{CMD}")
    assert started.stdin == CommandStdinState(open=True, offset=0)
    assert started.output_from == CommandOutputFrom(stdout=0, stderr=0)
    assert (started.timeout_ms, started.cwd, started.launched) == (None, "/srv/app", True)

    commands.start("make", timeout=5)
    assert script.body(3) == {"command": ["sh", "-c", "make"], "timeoutMs": 5000}

    with pytest.raises(AgentalError, match="not both"):
        commands.start("make", timeout=5, no_timeout=True)
    assert len(script.sent) == 5


def test_a_command_from_an_api_before_durable_commands_still_reads() -> None:
    old = {k: v for k, v in ended().items() if k in ("id", "sandboxId", "pid", "status", "exitCode", "stdout", "stderr", "startedAt", "finishedAt")}
    commands, _ = scripted(answer(200, old))
    got = commands.get(CMD)
    assert (got.status, got.exit_code) == ("succeeded", 0)
    assert (got.end_reason, got.stdin, got.output_from, got.stdout_bytes) == (None, None, None, None)


def test_a_list_sends_its_limit_and_its_commands_come_without_output() -> None:
    listed = {k: v for k, v in command().items() if k not in ("stdout", "stderr")}
    commands, script = scripted(answer(200, {"commands": [listed]}), answer(200, {"commands": []}))
    got = commands.list(limit=10)
    assert script.query(1) == {"limit": "10"}
    assert [(each.id, each.stdout, each.stderr) for each in got] == [(CMD, "", "")]
    assert commands.list() == []
    assert script.query(2) == {}


def test_output_gives_bytes_and_gaps_and_goes_on_after_a_cut_from_past_the_last_byte_given() -> None:
    commands, script = scripted(
        events(": heartbeat\n\n", gap("stderr", 0, 100), output("stdout", 0, b"caf\xc3"), output("stdout", 4, b"\xa9\n"), 'event: output\ndata: {"stream":"stdout","off'),
        httpx.ReadTimeout("silent"),
        events(output("stderr", 100, b"warn\n"), went_on(6, 105)),
        events(end(stdoutBytes=6, stderrBytes=105)),
    )
    got = list(commands.output(CMD))
    assert got[:4] == [
        CommandOutputEvent(type="gap", stream="stderr", offset=0, to=100),
        CommandOutputEvent(type="output", stream="stdout", offset=0, data=b"caf\xc3"),
        CommandOutputEvent(type="output", stream="stdout", offset=4, data=b"\xa9\n"),
        CommandOutputEvent(type="output", stream="stderr", offset=100, data=b"warn\n"),
    ]
    assert got[4].type == "end" and got[4].command is not None and got[4].command.stdout_bytes == 6
    assert len(got) == 5
    assert script.sent[1].headers["accept"] == "text/event-stream"
    # The half-sent event is not given, and each stream is asked again from past the last byte given.
    assert script.query(1) == {"stdout": "0", "stderr": "0", "follow": "1"}
    assert script.query(2) == {"stdout": "6", "stderr": "100", "follow": "1"}
    assert script.query(3) == {"stdout": "6", "stderr": "100", "follow": "1"}
    assert script.query(4) == {"stdout": "6", "stderr": "105", "follow": "1"}


def test_output_without_follow_gives_what_is_there_and_stops_at_continue() -> None:
    commands, script = scripted(events(output("stdout", 10, b"x"), went_on(11, 0)))
    assert [each.data for each in commands.output(CMD, stdout=10, follow=False)] == [b"x"]
    assert script.query(1) == {"stdout": "10", "stderr": "0", "follow": "0"}


def test_output_gives_up_after_five_cuts_in_a_row_with_nothing_read() -> None:
    commands, script = scripted(*[httpx.ConnectError("refused") for _ in range(6)])
    with pytest.raises(AgentalConnectionError):
        list(commands.output(CMD))
    assert len(script.sent) == 7

    # A cut after something was read starts the count again.
    commands, script = scripted(
        *[httpx.ConnectError("refused") for _ in range(4)],
        events(output("stdout", 0, b"a")),
        *[httpx.ConnectError("refused") for _ in range(4)],
        events(end()),
    )
    assert [each.type for each in commands.output(CMD)] == ["output", "end"]


def test_output_that_is_gone_or_refused_is_raised_and_not_asked_again() -> None:
    commands, script = scripted(
        events(output("stdout", 0, b"a"), 'event: gone\ndata: {"code":"output_gone"}\n\n'),
        refusal(410, "output_gone"),
        refusal(409, "sandbox_not_running"),
    )
    read: list[CommandOutputEvent] = []
    with pytest.raises(ApiError) as raised:
        for each in commands.output(CMD):
            read.append(each)
    assert raised.value.code == "output_gone"
    assert [each.data for each in read] == [b"a"]
    with pytest.raises(ApiError, match="output_gone"):
        list(commands.output(CMD))
    with pytest.raises(ApiError, match="sandbox_not_running"):
        list(commands.output(CMD))
    assert len(script.sent) == 4


def test_a_stdin_writer_reads_where_stdin_stands_and_sends_its_bytes_in_pieces_by_offset() -> None:
    data = b"a" * MiB + b"b" * 10
    commands, script = scripted(
        answer(200, command(stdin={"open": True, "offset": 7})),
        answer(200, {"offset": 7 + MiB}),
        answer(200, {"offset": 7 + MiB + 10}),
        answer(200, {"offset": 7 + MiB + 10}),
    )
    writer = commands.stdin(CMD)
    assert writer.write(data) == 7 + MiB + 10
    assert script.query(2) == {"offset": "7"}
    assert script.sent[2].headers["content-type"] == "application/octet-stream"
    assert script.sent[2].content == b"a" * MiB
    assert (script.query(3), script.sent[3].content) == ({"offset": str(7 + MiB)}, b"b" * 10)
    assert writer.close() == 7 + MiB + 10
    assert (script.sent[4].method, script.sent[4].url.path) == ("POST", f"{COMMANDS}/{CMD}/stdin/close")


def test_a_stdin_writer_sends_again_what_was_not_taken_and_from_where_the_api_says() -> None:
    commands, script = scripted(
        # A command not reading takes part, then nothing, then the stdin does not answer, then it takes the rest.
        answer(200, {"offset": 3}),
        answer(200, {"offset": 3}),
        refusal(503, "stdin_unreachable"),
        answer(200, {"offset": 10}),
        # The API has fewer bytes than the writer was told: it goes on from there, within what it was given.
        answer(200, {"offset": 16}),
        refusal(409, "stdin_offset", offset=13),
        answer(200, {"offset": 20}),
        # Before what it was given, it cannot.
        refusal(409, "stdin_offset", offset=12),
    )
    writer = commands.stdin(CMD, offset=0)
    assert writer.write("0123456789") == 10
    assert [script.query(i)["offset"] for i in range(1, 5)] == ["0", "3", "3", "3"]
    assert script.sent[4].content == b"3456789"

    assert writer.write(b"ABCDEFGHIJ") == 20
    assert [script.query(i)["offset"] for i in (5, 6, 7)] == ["10", "16", "13"]
    assert script.sent[7].content == b"DEFGHIJ"

    writer.offset = 13
    with pytest.raises(ApiError, match="stdin_offset"):
        writer.write(b"xy")
    assert len(script.sent) == 9


def test_a_stdin_refusal_that_waiting_cannot_help_is_raised() -> None:
    commands, script = scripted(refusal(409, "command_ended"), answer(200, command(stdin=None)))
    with pytest.raises(ApiError, match="command_ended"):
        commands.stdin(CMD, offset=0).write(b"x")
    with pytest.raises(ApiError, match="without stdin=True") as raised:
        commands.stdin(CMD).write(b"x")
    assert raised.value.code == "no_stdin"
    assert len(script.sent) == 3


def test_write_stdin_is_one_write_and_answers_the_offset() -> None:
    commands, script = scripted(answer(200, {"offset": 2}), refusal(409, "stdin_offset", offset=4))
    assert commands.write_stdin(CMD, b"hello", offset=0) == 2
    with pytest.raises(ApiError) as raised:
        commands.write_stdin(CMD, b"x", offset=9)
    assert raised.value.offset == 4
    with pytest.raises(AgentalError, match="1 MiB"):
        commands.write_stdin(CMD, b"x" * (MiB + 1), offset=0)
    with pytest.raises(AgentalError, match="1 byte"):
        commands.write_stdin(CMD, b"", offset=0)
    assert len(script.sent) == 3


def test_a_signal_is_sent_by_name_and_kill_sends_term_then_kill_after_the_grace() -> None:
    commands, script = scripted(
        answer(200, {"signaled": 3}),
        answer(200, {"signaled": 2}),
        answer(200, command()),
        answer(200, {"signaled": 1}),
        answer(200, ended(status="failed", exitCode=None, endReason="signaled", signal=9)),
        answer(200, {"signaled": 1}),
        answer(200, ended(status="failed", exitCode=None, endReason="signaled", signal=15)),
    )
    assert commands.signal(CMD, "SIGINT") == 3
    assert (script.sent[1].url.path, script.body(1)) == (f"{COMMANDS}/{CMD}/signal", {"signal": "INT"})
    with pytest.raises(AgentalError, match="not 'TERMINATE'"):
        commands.signal(CMD, "TERMINATE")

    killed = commands.kill(CMD, grace=0)
    assert [script.body(i)["signal"] for i in (2, 4)] == ["TERM", "KILL"]
    assert killed.signal == 9

    # One that ends within its grace is not sent KILL.
    assert commands.kill(CMD, grace=5).signal == 15
    assert len(script.sent) == 8


def test_a_delete_forgets_an_ended_command_and_a_running_one_is_refused() -> None:
    commands, script = scripted(answer(204), refusal(409, "still_running"))
    commands.delete(CMD)
    assert (script.sent[1].method, script.sent[1].url.path) == ("DELETE", f"{COMMANDS}/{CMD}")
    with pytest.raises(ApiError, match="still_running"):
        commands.delete(CMD)


def test_a_wait_with_no_limit_reads_until_it_ends() -> None:
    commands, _ = scripted(*[answer(200, command()) for _ in range(3)], answer(200, ended()))
    assert commands.wait(CMD, timeout=None).status == "succeeded"


def test_the_client_that_awaits_follows_output_and_writes_stdin_too() -> None:
    script = Script(
        answer(200, sandbox()),
        events(output("stdout", 0, b"hi")),
        events(end()),
        answer(200, {"offset": 1}),
        answer(200, {"offset": 2}),
    )

    async def main() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(script))
        async with AsyncAgental("key_test", base_url="https://api.test", retry_delay=0, poll_interval=0, http_client=client) as agental:
            commands = (await agental.sandboxes.get(ID)).commands
            assert [each.type async for each in commands.output(CMD)] == ["output", "end"]
            assert await commands.stdin(CMD, offset=0).write(b"ab") == 2

    asyncio.run(main())
    assert script.query(2) == {"stdout": "2", "stderr": "0", "follow": "1"}
    assert [script.sent[i].content for i in (3, 4)] == [b"ab", b"b"]


def test_a_continue_that_moved_nothing_waits_and_never_counts_toward_giving_up() -> None:
    # The command exited and its end is not recorded yet: the API says continue at once, until it says end.
    commands, script = scripted(*[events(went_on(0, 0)) for _ in range(8)], events(end()))
    assert [each.type for each in commands.output(CMD)] == ["end"]
    assert len(script.sent) == 10

    # Nor does it start the count of cuts again: four cuts, a continue that moved nothing, and two more cuts give up.
    commands, script = scripted(*[httpx.ConnectError("refused") for _ in range(4)], events(went_on(0, 0)), *[httpx.ConnectError("refused") for _ in range(2)])
    with pytest.raises(AgentalConnectionError):
        list(commands.output(CMD))
    assert len(script.sent) == 8

    # A continue that moved the offsets goes on at once, and starts the count again.
    commands, script = scripted(*[httpx.ConnectError("refused") for _ in range(4)], events(went_on(5, 0)), *[httpx.ConnectError("refused") for _ in range(4)], events(end()))
    assert [each.type for each in commands.output(CMD)] == ["end"]


def test_the_wait_after_a_continue_that_moved_nothing_grows_to_four_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr("agental._sync.time.sleep", waits.append)
    script = Script(answer(200, sandbox()), *[events(went_on(0, 0)) for _ in range(5)], events(end()))
    client = Agental("key_test", base_url="https://api.test/", retry_delay=1, poll_interval=0, http_client=httpx.Client(transport=httpx.MockTransport(script)))
    assert [each.type for each in client.sandboxes.get(ID).commands.output(CMD)] == ["end"]
    assert waits == [1, 2, 4, 4, 4]


def test_run_takes_what_start_takes_and_with_no_limit_waits_as_long_as_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    commands, script = scripted(
        answer(201, {"id": CMD, "pid": 42, "status": "running"}),
        answer(200, command()),
        answer(200, command()),
        answer(200, ended()),
    )
    from agental import _core

    # Past the usual 11 minutes at once: a run with no limit is not cut short by it.
    monkeypatch.setattr(_core, "COMMAND_TIMEOUT", -1.0)
    assert commands.run("make", no_timeout=True, cwd="/srv/app", env={"A": "b"}, stdin=True).status == "succeeded"
    assert script.body(1) == {"command": ["sh", "-c", "make"], "timeoutMs": None, "cwd": "/srv/app", "env": {"A": "b"}, "stdin": True}


def test_kill_gives_five_seconds_unless_told_otherwise() -> None:
    import inspect

    from agental import AsyncCommands, Commands

    assert inspect.signature(Commands.kill).parameters["grace"].default == 5.0
    assert inspect.signature(AsyncCommands.kill).parameters["grace"].default == 5.0
