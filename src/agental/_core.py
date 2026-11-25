"""What the two clients share: which requests to send, when to send one again, and what to wait for.

Each piece of work is a generator. It yields a Call and is sent back the Answer to it, or
has the error thrown into it, and it yields a Sleep where it has to wait. The client that
waits and the one that awaits each run the same generators, so what is done about an
answer is written once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .errors import AgentalConnectionError, AgentalError, AgentalTimeoutError, ApiError, ProxyError
from .models import Command, CommandOutputEvent, ExecEvent, TerminalExit, build

DEFAULT_BASE_URL = "https://agental.io"

#: A create answers when the machine is ready, usually in under a minute. The API says to allow it three.
CREATE_TIMEOUT = 180.0
#: An archived sandbox is brought back in as long as its disk is large.
RESTORE_TIMEOUT = 600.0
#: No command sent through the API runs longer than ten minutes.
COMMAND_TIMEOUT = 660.0
#: How long a request is given to answer, unless it is a command with a longer limit of its own.
REQUEST_TIMEOUT = 300.0
#: How many times a sandbox_migrating is waited out: a move of a running sandbox takes a minute or two.
MIGRATING_TRIES = 18


@dataclass
class Call:
    method: str
    path: str
    query: Mapping[str, str | list[str] | None] | None = None
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Whether sending it twice does what sending it once does. Only such a request is sent
    #: again by itself: a read, a create with its key, a file write. A command is not.
    replay: bool = False
    timeout: float = REQUEST_TIMEOUT
    #: Whether the answer is wanted as the bytes it came as, and is not to be read as JSON.
    raw: bool = False


@dataclass
class Sleep:
    seconds: float


@dataclass
class Answer:
    status: int
    body: Any
    headers: Mapping[str, str]


Steps = Generator["Call | Sleep", Any, Any]


@dataclass
class Settings:
    api_key: str
    base_url: str
    max_retries: int
    retry_delay: float
    poll_interval: float


def signed_in() -> tuple[str, str] | None:
    """The API and the key the agental command signed in with on this computer, or None.

    The command keeps them in ~/.config/agental/credentials.json, or under XDG_CONFIG_HOME.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.join(os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(base, "agental", "credentials.json"), encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, ValueError):
        return None
    if isinstance(saved, dict) and isinstance(saved.get("apiUrl"), str) and isinstance(saved.get("apiKey"), str):
        return saved["apiUrl"], saved["apiKey"]
    return None


def settings(api_key: str | None, base_url: str | None, max_retries: int, retry_delay: float, poll_interval: float) -> Settings:
    key = api_key or os.environ.get("AGENTAL_API_KEY", "").strip()
    url = base_url or os.environ.get("AGENTAL_API_URL", "").strip()
    if not key:
        # The agental command's sign-in, for the API it was made to and no other.
        saved = signed_in()
        if saved and (not url or url.rstrip("/") == saved[0].rstrip("/")):
            url, key = saved
    if not key:
        raise AgentalError(
            "No API key. Pass api_key, or set AGENTAL_API_KEY, or sign in on this computer with npx agental@latest."
            " Keys are made in the console, under API keys."
        )
    return Settings(key, (url or DEFAULT_BASE_URL).rstrip("/"), max_retries, retry_delay, poll_interval)


def error_of(status: int, text: str) -> AgentalError:
    """The API's own error when the answer has a code and a message in it, and a proxy's when it has not."""
    try:
        error = json.loads(text).get("error")
        if isinstance(error["code"], str) and isinstance(error["message"], str):
            room = error.get("regionsWithRoom")
            regions = [one for one in room if isinstance(one, str)] if isinstance(room, list) else None
            fee, after, refused, url = error.get("feeMicros"), error.get("retryAfterSeconds"), error.get("refusedLastHour"), error.get("limitsUrl")
            offset = error.get("offset")
            return ApiError(
                status,
                error["code"],
                error["message"],
                regions,
                fee_micros=fee if isinstance(fee, int) and not isinstance(fee, bool) else None,
                retry_after_seconds=after if isinstance(after, int) and not isinstance(after, bool) else None,
                refused_last_hour=refused if isinstance(refused, int) and not isinstance(refused, bool) else None,
                limits_url=url if isinstance(url, str) else None,
                offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
            )
    except (ValueError, AttributeError, KeyError, TypeError):
        pass
    return ProxyError(status)


def wait_before(failure: AgentalError, attempt: int, config: Settings, *, replay: bool) -> float | None:
    """How long to wait before a failed request is sent again, or None when it is not to be.

    The waits are the ones the API asks for: a request cut off by a move is sent again at
    once, a move under way is waited out, and a proxy or a failing host is given ten times
    the shortest wait. Only a request that may be sent twice is sent again, except one the
    API turned away as it restarted: nothing was done with that one, a command neither.
    """
    if isinstance(failure, ApiError) and failure.code == "restarting":
        return config.retry_delay if attempt < config.max_retries else None
    if not replay:
        return None
    if isinstance(failure, ApiError):
        if failure.code == "sandbox_migrating":
            return config.retry_delay * 10 if attempt < MIGRATING_TRIES else None
        if failure.code == "sandbox_moved":
            return config.retry_delay if attempt < config.max_retries else None
        if failure.code in ("host_error", "internal_error"):
            return config.retry_delay * 10 if attempt < 1 else None
        return None
    if attempt >= config.max_retries:
        return None
    if isinstance(failure, ProxyError):
        return config.retry_delay * 10 if failure.status >= 500 else None
    return config.retry_delay * 2**attempt if isinstance(failure, AgentalConnectionError) else None


def argv(command: str | list[str] | tuple[str, ...]) -> list[str]:
    """A list is the arguments exactly as given, with no shell. A string is a line of shell, run by sh -c."""
    return ["sh", "-c", command] if isinstance(command, str) else list(command)


_TAG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def tag_segment(tag: str) -> str:
    """A tag as a part of a path: lowercased and trimmed as the API would, and refused here when it is not one,
    so that no word, "..", say, can make the path another."""
    word = tag.strip().lower()
    if not _TAG.fullmatch(word):
        raise AgentalError(f"A tag is lowercase letters, digits and hyphens, 1 to 63 characters, not starting or ending with a hyphen, not {tag!r}.")
    return word


def made(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def iso(when: str | datetime | None) -> str | None:
    return when.isoformat() if isinstance(when, datetime) else when


def command_call(path: str, command: str | list[str] | tuple[str, ...], timeout_ms: int | None) -> Call:
    body: dict[str, Any] = {"command": argv(command)}
    if timeout_ms is not None:
        body["timeoutMs"] = timeout_ms
    return Call("POST", path, body=body, timeout=max(REQUEST_TIMEOUT, (timeout_ms or 0) / 1000 + 30))


def file_body(path: str, contents: str | bytes) -> dict[str, str]:
    if isinstance(contents, str):
        return {"path": path, "text": contents}
    return {"path": path, "base64": base64.b64encode(contents).decode("ascii")}


def one(call: Call) -> Steps:
    """A single request, and the JSON it was answered with."""
    answer: Answer = yield call
    return answer.body


def answered(call: Call) -> Steps:
    """A single request, and the whole answer: its status and headers as well as its JSON."""
    answer: Answer = yield call
    return answer


#: The burst refusals a wait may help: the start costs nothing once the meter has fallen.
WAITABLE_BURST = frozenset({"burst_fee_too_high", "burst_budget_reached"})


def burst_headers(max_fee_micros: int | None) -> dict[str, str]:
    """The header a start is sent with to cap what it pays in burst fees, when one is asked for."""
    if max_fee_micros is None:
        return {}
    if isinstance(max_fee_micros, bool) or not isinstance(max_fee_micros, int) or max_fee_micros < 0:
        raise AgentalError(f"max_burst_fee_micros is a whole number of millionths of a US dollar, 0 or more, not {max_fee_micros!r}.")
    return {"Agental-Max-Burst-Fee": str(max_fee_micros)}


def burst_fee_of(headers: Mapping[str, str]) -> int | None:
    """What a start paid in burst fees, as its answer says, or None where it does not."""
    value = headers.get("agental-burst-fee")
    return int(value) if value is not None and value.isdigit() else None


def burst_waited(start: Callable[[], Steps], max_wait: float | None) -> Steps:
    """Does a start, and when it is refused for its burst and max_wait allows it, waits until it would cost nothing and does it once more."""
    try:
        return (yield from start())
    except ApiError as error:
        after = error.retry_after_seconds
        if max_wait is None or error.code not in WAITABLE_BURST or after is None or after > max_wait:
            raise
    yield Sleep(float(after))
    return (yield from start())


def make_sandbox(path: str, body: dict[str, Any], key: str, timeout: float, config: Settings, max_fee_micros: int | None = None) -> Steps:
    """Asks for a sandbox, by a create or a fork, and waits until it is running. It returns the sandbox and what it paid in burst fees.

    A 202 means the machine was asked for and is not confirmed yet, and so does an
    uncertain operation: the same request is sent again with the same key until it answers
    with the sandbox.
    """
    headers = {"Idempotency-Key": key, **burst_headers(max_fee_micros)}
    deadline = time.monotonic() + timeout
    # The first answer says what the start paid. One sent again with the same key started nothing more.
    fee: int | None = None
    while True:
        try:
            answer: Answer = yield Call("POST", path, body=body, headers=headers, replay=True)
            if fee is None:
                fee = burst_fee_of(answer.headers)
            if answer.status != 202:
                return answer.body, fee
        except ApiError as error:
            if error.code != "uncertain_operation":
                raise
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f'The sandbox was not running after {timeout:g} s. Ask again with idempotency_key "{key}" to get the same one.')
        yield Sleep(config.poll_interval)


def until_running(path: str, info: dict[str, Any], timeout: float, config: Settings) -> Steps:
    """Reads the sandbox until it is running. It raises for one that is stopped, with why a start of it failed when one did."""
    deadline = time.monotonic() + timeout
    while True:
        status = info["status"]
        if status == "running":
            return info
        if status not in ("creating", "restoring", "migrating"):
            why = f": {info['error']}" if info.get("error") else "."
            raise AgentalError(f"Sandbox {info['id']} is {status}, not running{why}")
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Sandbox {info['id']} was still {status} after {timeout:g} s.")
        yield Sleep(config.poll_interval)
        info = yield from one(Call("GET", path, replay=True))


def start(path: str, wait: bool, timeout: float, config: Settings, max_fee_micros: int | None = None) -> Steps:
    """Starts a sandbox, and waits until it is running unless told not to. It returns the sandbox and what the start paid in burst fees."""
    fee: int | None = None
    try:
        answer: Answer = yield Call("POST", f"{path}/start", headers=burst_headers(max_fee_micros), replay=True)
        info, fee = answer.body, burst_fee_of(answer.headers)
    except ApiError as error:
        # Whether the start worked is not known yet: the sandbox's status will say.
        if error.code != "uncertain_operation":
            raise
        info = yield from one(Call("GET", path, replay=True))
    if not wait:
        return info, fee
    return (yield from until_running(path, info, timeout, config)), fee


def archive(path: str, wait: bool, timeout: float, config: Settings) -> Steps:
    answer: Answer = yield Call("POST", f"{path}/archive", replay=True)
    info = answer.body
    if not wait:
        return info
    operation_id = answer.headers.get("operation-id")
    deadline = time.monotonic() + timeout
    while not info["archived"]:
        if operation_id:
            operation = (yield from one(Call("GET", f"/v1/operations/{operation_id}", replay=True)))["operation"]
            if operation["status"] == "failed":
                raise AgentalError(f"Sandbox {info['id']} was not archived: {operation.get('error') or 'the archive failed'}")
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Sandbox {info['id']} was not archived after {timeout:g} s.")
        yield Sleep(config.poll_interval)
        info = yield from one(Call("GET", path, replay=True))
    return info


def stop(path: str, keep_memory: bool, timeout: float, config: Settings) -> Steps:
    answer: Answer = yield Call("POST", f"{path}/stop", body={"keepMemory": True} if keep_memory else None, replay=True)
    info = answer.body
    if answer.status != 202:
        return info
    # Its memory is still being written, and the stop goes on: read it until it has stopped.
    operation_id = answer.headers.get("operation-id")
    deadline = time.monotonic() + timeout
    while info["status"] != "stopped":
        if operation_id:
            operation = (yield from one(Call("GET", f"/v1/operations/{operation_id}", replay=True)))["operation"]
            if operation["status"] == "failed":
                raise AgentalError(f"Sandbox {info['id']} was not stopped: {operation.get('error') or 'the stop failed'}")
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Sandbox {info['id']} was still writing its memory after {timeout:g} s. The stop goes on.")
        yield Sleep(config.poll_interval)
        info = yield from one(Call("GET", path, replay=True))
    return info


def start_command(path: str, command: str | list[str] | tuple[str, ...], timeout_ms: int | None, extra: dict[str, Any] | None = None) -> Steps:
    """Starts a command and reads it. extra is what the start is sent with besides the command and its time limit:
    cwd, env, stdin, or timeoutMs None for no limit."""
    call = command_call(path, command, timeout_ms)
    call.body.update(extra or {})
    started = yield from one(call)
    return (yield from one(Call("GET", f"{path}/{quote(started['id'], safe='')}", replay=True)))


def start_extra(timeout: float | None, no_timeout: bool, cwd: str | None, env: Mapping[str, str] | None, stdin: bool) -> dict[str, Any]:
    """What a start is sent with besides the command and a time limit it was given, or an error that says what is wrong."""
    extra: dict[str, Any] = {}
    if no_timeout:
        if timeout is not None:
            raise AgentalError("A command has a timeout or no_timeout=True, not both.")
        extra["timeoutMs"] = None
    if cwd is not None:
        extra["cwd"] = cwd
    if env is not None:
        extra["env"] = dict(env)
    if stdin:
        extra["stdin"] = True
    return extra


def wait_command(path: str, command_id: str, timeout: float | None, config: Settings) -> Steps:
    """Reads a command until it has ended: within timeout seconds, or for as long as it takes with None."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        command = yield from one(Call("GET", f"{path}/{quote(command_id, safe='')}", replay=True))
        if command["status"] != "running":
            return command
        if deadline is not None and time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Command {command_id} was still running after {timeout:g} s.")
        yield Sleep(config.poll_interval)


#: The signals a command can be sent.
COMMAND_SIGNALS = frozenset({"TERM", "KILL", "INT", "HUP", "QUIT", "USR1", "USR2", "STOP", "CONT"})
#: How long a command is waited for after KILL: it ends at once, unless its sandbox does not answer.
KILL_WAIT = 30.0


def signal_command(path: str, command_id: str, signal: str) -> Steps:
    """Sends a signal to every process of a command. It answers how many it reached."""
    name = signal.upper().removeprefix("SIG")
    if name not in COMMAND_SIGNALS:
        raise AgentalError(f"A command is sent one of {', '.join(sorted(COMMAND_SIGNALS))}, not {signal!r}.")
    answer = yield from one(Call("POST", f"{path}/{quote(command_id, safe='')}/signal", body={"signal": name}))
    return int(answer["signaled"])


def kill_command(path: str, command_id: str, grace: float, config: Settings) -> Steps:
    """Sends TERM, waits up to grace seconds for the command to end, then sends KILL and waits for it to end."""
    if grace < 0:
        raise AgentalError(f"grace is seconds, 0 or more, not {grace!r}.")
    yield from signal_command(path, command_id, "TERM")
    deadline = time.monotonic() + grace
    while True:
        command = yield from one(Call("GET", f"{path}/{quote(command_id, safe='')}", replay=True))
        if command["status"] != "running":
            return command
        if time.monotonic() + config.poll_interval >= deadline:
            break
        yield Sleep(config.poll_interval)
    yield from signal_command(path, command_id, "KILL")
    return (yield from wait_command(path, command_id, KILL_WAIT, config))


#: The most bytes the API takes in one write of a command's stdin.
STDIN_PIECE = 1024 * 1024


def stdin_call(path: str, command_id: str, data: bytes, offset: int) -> Call:
    """One write of a command's stdin, at offset. Sending it twice is safe: bytes already accepted are skipped."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise AgentalError(f"offset is a whole number of bytes, 0 or more, not {offset!r}.")
    if not 1 <= len(data) <= STDIN_PIECE:
        raise AgentalError(f"A write of stdin is 1 byte to 1 MiB, not {len(data)} bytes.")
    return Call("POST", f"{path}/{quote(command_id, safe='')}/stdin", query={"offset": str(offset)}, body=bytes(data), replay=True)


def write_stdin(path: str, command_id: str, data: bytes, offset: int, config: Settings) -> Steps:
    """Writes all of data to a command's stdin from offset, and answers the offset after it as the API says it.

    Data is sent in pieces of at most 1 MiB. A command that is not reading takes part of a piece,
    and the rest is sent again until it has all been taken, so this waits for as long as the
    command does not read: it ends when the command ends, with command_ended. A refusal with
    stdin_offset goes on from where the API says, as long as that is within data; a
    stdin_unreachable sends the same bytes again, max_retries times in a row at most.
    """
    start = offset
    end = offset + len(data)
    at = offset
    unreachable = 0
    while at < end:
        piece = data[at - start : at - start + STDIN_PIECE]
        try:
            answer = yield from one(stdin_call(path, command_id, piece, at))
        except ApiError as error:
            if error.code == "stdin_offset" and error.offset is not None and start <= error.offset < at:
                at = error.offset
                continue
            if error.code == "stdin_unreachable" and unreachable < config.max_retries:
                unreachable += 1
                yield Sleep(config.retry_delay * 2 ** (unreachable - 1))
                continue
            raise
        unreachable = 0
        taken = int(answer["offset"])
        if taken <= at:
            # It took nothing in the API's 20 seconds: the command is not reading yet.
            yield Sleep(config.poll_interval)
        at = max(at, taken)
    return at


#: The most seconds the API holds a call for a job's run to end: 90, as the proxy in front of it gives up at 100.
JOB_WAIT_HELD = 90


def run_job(path: str, body: dict[str, Any] | None, wait: float | None, config: Settings) -> Steps:
    """Makes a job's run at path, as run and run again do: answered as it begins, or, with wait, held by the API until it ends and then read until it has.

    It answers the run, and with wait whether it came with its output: 200 is the run ended
    within the hold. One still running when the wait is out raises AgentalTimeoutError.
    """
    if wait is None:
        return (yield from one(Call("POST", path, body=body))), False
    deadline = time.monotonic() + wait
    held = max(0, min(JOB_WAIT_HELD, int(wait)))
    answer: Answer = yield Call("POST", path, query={"wait": str(held)}, body=body, timeout=held + REQUEST_TIMEOUT)
    if answer.status == 200:
        return answer.body, True
    started = answer.body
    run_path = f"/v1/jobs/{quote(started['jobId'], safe='')}/runs/{quote(started['id'], safe='')}"
    return (yield from wait_job_run(run_path, wait, config, deadline)), True


def wait_job_run(path: str, timeout: float, config: Settings, deadline: float | None = None) -> Steps:
    """Reads a job's run, with its output, until it has ended: within timeout seconds, or by deadline where one is given."""
    if deadline is None:
        deadline = time.monotonic() + timeout
    while True:
        run = yield from one(Call("GET", path, replay=True))
        if run["outcome"] != "running":
            return run
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Run {run['id']} was still running after {timeout:g} s.")
        yield Sleep(config.poll_interval)


def run_command(
    path: str, command: str | list[str] | tuple[str, ...], timeout_ms: int | None, config: Settings, extra: dict[str, Any] | None = None
) -> Steps:
    """Starts a command and reads it until it has ended: for at most COMMAND_TIMEOUT, or as long as it takes for one with no limit."""
    started = yield from start_command(path, command, timeout_ms, extra)
    if started["status"] != "running":
        return started
    no_limit = extra is not None and "timeoutMs" in extra and extra["timeoutMs"] is None
    return (yield from wait_command(path, started["id"], None if no_limit else COMMAND_TIMEOUT, config))


class StreamReader:
    """Turns the lines of a command's stream into its events."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self.exited = False

    def line(self, line: str) -> ExecEvent | None:
        """What a line completes: an event, or nothing yet. A blank line ends an event."""
        if line.startswith("event:"):
            self._event = line[6:].strip()
            return None
        if line.startswith("data:"):
            self._data.append(line[5:].removeprefix(" "))
            return None
        if line != "":
            return None
        event, data = self._event, "\n".join(self._data)
        self._event, self._data = "", []
        if event == "error":
            raise AgentalError(f"The command was cut off: {data}")
        if event in ("stdout", "stderr"):
            return ExecEvent(type=event, data=str(json.loads(data).get("data", "")))  # type: ignore[arg-type]
        if event == "exit":
            self.exited = True
            return ExecEvent(type="exit", exit_code=int(json.loads(data)["exitCode"]))
        return None

    def ended(self) -> None:
        if not self.exited:
            raise AgentalConnectionError("The stream ended before the command said what it exited with. It may still be running.")


class EventLines:
    """Turns the lines of a stream of server-sent events into (event, data) pairs. Comments, the heartbeats, give nothing."""

    def __init__(self) -> None:
        self._event = ""
        self._data: list[str] = []

    def line(self, line: str) -> tuple[str, str] | None:
        """What a line completes: an event and its data, or nothing yet. A blank line ends an event."""
        if line.startswith("event:"):
            self._event = line[6:].strip()
            return None
        if line.startswith("data:"):
            self._data.append(line[5:].removeprefix(" "))
            return None
        if line != "":
            return None
        if not self._event and not self._data:
            return None
        event, data = self._event or "message", "\n".join(self._data)
        self._event, self._data = "", []
        return event, data


#: How many times in a row output() connects again after its stream was cut without anything read.
OUTPUT_RECONNECTS = 5
#: The longest output() waits before it opens the stream again after a continue that moved no offset: the command
#: has exited and its end is not recorded yet, which the API does within its 30-second sweep.
OUTPUT_EMPTY_CONTINUE_WAIT = 4.0
#: The API's codes for a read that may work when sent again: it restarted, or the sandbox moved or is moving.
_RESUMABLE_CODES = frozenset({"restarting", "sandbox_moved", "sandbox_migrating", "host_error", "internal_error"})


def output_resumable(failure: AgentalError) -> bool:
    """Whether a stream of output that failed so is worth opening again: a cut, a proxy's or a host's failure, a restart."""
    if isinstance(failure, ApiError):
        return failure.code in _RESUMABLE_CODES
    if isinstance(failure, ProxyError):
        return failure.status >= 500
    return isinstance(failure, AgentalConnectionError)


#: The longest a stream of output may be silent before it is taken as cut: the API sends a comment every 15 seconds.
OUTPUT_SILENCE = 60.0


class OutputReader:
    """What a command's output stream has said, and where to go on from when it is cut.

    at holds the next byte wanted of each stream. Once the stream closes, ended says the command
    ended, continued that the API closed it on purpose: anything else was a cut, to be resumed from at.
    """

    def __init__(self, stdout: int, stderr: int) -> None:
        for what, value in (("stdout", stdout), ("stderr", stderr)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AgentalError(f"{what} is a byte offset, a whole number 0 or more, not {value!r}.")
        self.at = {"stdout": stdout, "stderr": stderr}
        self.ended = False
        self.continued = False

    def call(self, path: str, command_id: str, follow: bool) -> Call:
        """The request that reads the output from where this reader is."""
        query = {"stdout": str(self.at["stdout"]), "stderr": str(self.at["stderr"]), "follow": "1" if follow else "0"}
        return Call("GET", f"{path}/{quote(command_id, safe='')}/output", query=query, timeout=OUTPUT_SILENCE)

    def connected(self) -> None:
        """A new stream begins: what the last one said of how it closed no longer holds."""
        self.continued = False

    def event(self, event: str, data: str) -> CommandOutputEvent | None:
        """What an event says, as one to give the caller, or None for one that only moves where to go on from."""
        if event == "output":
            said = json.loads(data)
            stream, offset, raw = said["stream"], int(said["offset"]), base64.b64decode(said["data"])
            self.at[stream] = max(self.at[stream], offset + len(raw))
            return CommandOutputEvent(type="output", stream=stream, offset=offset, data=raw)
        if event == "gap":
            said = json.loads(data)
            stream, start, to = said["stream"], int(said["from"]), int(said["to"])
            self.at[stream] = max(self.at[stream], to)
            return CommandOutputEvent(type="gap", stream=stream, offset=start, to=to)
        if event == "continue":
            said = json.loads(data)
            self.at = {"stdout": max(self.at["stdout"], int(said["stdout"])), "stderr": max(self.at["stderr"], int(said["stderr"]))}
            self.continued = True
            return None
        if event == "end":
            self.ended = True
            return CommandOutputEvent(type="end", command=build(Command, json.loads(data)))
        if event == "gone":
            raise ApiError(410, "output_gone", "The command's sandbox no longer holds its output: get() gives the last of it, as last saved.")
        if event == "error":
            raise AgentalConnectionError(f"The output stream was cut: {data}")
        return None


#: The WebSocket subprotocol a sandbox's terminal speaks.
TERMINAL_PROTOCOL = "agental.terminal.v1"
#: The subprotocol a ticket is sent as: this, then the ticket. A ticket is never put in a URL.
TICKET_PROTOCOL_PREFIX = "ticket."
#: The subprotocol the ticket's verifier is sent as: this, then the verifier.
VERIFIER_PROTOCOL_PREFIX = "verifier."


def terminal_pkce() -> tuple[str, str]:
    """A verifier, kept in memory alone, and its S256 challenge (RFC 7636), which a ticket is asked with.

    The ticket then opens the terminal only with the verifier, which no answer carries, from whatever
    address the WebSocket leaves by: not every network sends a request and a WebSocket out by one.
    """
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


#: The most the API takes in one message of what is typed: longer input is sent in pieces this size.
TERMINAL_INPUT_BYTES = 64 * 1024


def terminal_size(cols: int, rows: int) -> dict[str, int]:
    """The body a terminal is asked for with, or an error that says what its size may be."""
    for what, value in (("cols", cols), ("rows", rows)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise AgentalError(f"A terminal's {what} is a whole number from 1 to 1000, not {value!r}.")
    return {"cols": cols, "rows": rows}


#: A kept shell's id.
KEPT_TERMINAL_ID = re.compile(r"pty_[0-9a-f]{16}")


def kept_terminal_id(terminal_id: Any, what: str) -> str:
    """A kept shell's id, or an error that says what one is."""
    if not isinstance(terminal_id, str) or not KEPT_TERMINAL_ID.fullmatch(terminal_id):
        raise AgentalError(f"{what} takes a kept shell's id, as pty_0123456789abcdef, not {str(terminal_id)[:100]!r}.")
    return terminal_id


def terminal_keep(keep: bool, attach: str | None) -> dict[str, Any]:
    """What a ticket is asked with besides its size: keep, or the kept shell to open again."""
    if attach is not None:
        if keep:
            raise AgentalError("A terminal is a new one, kept or not, or a kept one opened again: give keep or attach, not both.")
        return {"terminal": kept_terminal_id(attach, "attach")}
    return {"keep": True} if keep else {}


def websocket_url(base_url: str, path: str) -> str:
    """The ws: or wss: URL of a path on the API's own address.

    Only a path is taken, so that no answer can send the WebSocket, and the ticket in it, to another host.
    """
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or re.search(r"[\\\s]", path):
        raise AgentalError(f"The API gave the terminal a path that is not one on its own address: {str(path)[:200]!r}.")
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + path
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :].rstrip("/") + path
    raise AgentalError(f"A terminal is opened on an http: or https: API, not {base_url!r}.")


def _whole(value: Any) -> int | None:
    """A whole number the guest said, which is the tenant's: None for anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TerminalState:
    """What a terminal's text messages have said: whether it started, how the shell exited, and the API's error."""

    def __init__(self) -> None:
        self.started = False
        self.exit: dict[str, Any] | None = None
        self.error: dict[str, str] | None = None
        #: The kept shell this is, as its started message names it: None for one that is not kept.
        self.terminal_id: str | None = None
        self.n: int | None = None

    def said(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "started":
            terminal_id, n = message.get("terminal"), _whole(message.get("n"))
            if isinstance(terminal_id, str) and KEPT_TERMINAL_ID.fullmatch(terminal_id) and n is not None and n >= 1:
                self.terminal_id, self.n = terminal_id, n
            self.started = True
        elif kind == "exit":
            self.exit = message
        elif kind == "error":
            self.error = {"code": str(message.get("code", "terminal")), "message": str(message.get("message", ""))}

    def ended(self, close_code: int | None, reason: str | None) -> TerminalExit:
        exit = self.exit or {}
        return TerminalExit(
            exit_code=_whole(exit.get("code")) if self.exit is not None else None,
            signal=_whole(exit.get("signal")) or 0,
            timed_out=exit.get("timedOut") is True,
            close_code=close_code if close_code is not None else 1006,
            reason=reason or "",
            error_code=self.error["code"] if self.error else None,
            error_message=self.error["message"] if self.error else None,
        )


def terminal_refusal(status: int | None, body: bytes) -> str:
    """Why a terminal's handshake was refused, in words, from the API's answer. Never the ticket."""
    if status is None:
        return "the connection failed"
    try:
        error = json.loads(body).get("error", {})
        message = str(error.get("message", "")).strip()
    except (ValueError, AttributeError):
        message = ""
    return f"HTTP {status}" + (f": {message[:300]}" if message else "")
