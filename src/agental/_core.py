"""What the two clients share: which requests to send, when to send one again, and what to wait for.

Each piece of work is a generator. It yields a Call and is sent back the Answer to it, or
has the error thrown into it, and it yields a Sleep where it has to wait. The client that
waits and the one that awaits each run the same generators, so what is done about an
answer is written once.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import AgentalConnectionError, AgentalError, AgentalTimeoutError, ApiError, ProxyError
from .models import ExecEvent

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
            fee, after = error.get("feeMicros"), error.get("retryAfterSeconds")
            return ApiError(
                status,
                error["code"],
                error["message"],
                regions,
                fee_micros=fee if isinstance(fee, int) and not isinstance(fee, bool) else None,
                retry_after_seconds=after if isinstance(after, int) and not isinstance(after, bool) else None,
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
WAITABLE_BURST = frozenset({"burst_fee_too_high", "burst_budget_reached", "burst_in_free_month"})


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


def start_command(path: str, command: str | list[str] | tuple[str, ...], timeout_ms: int | None) -> Steps:
    started = yield from one(command_call(path, command, timeout_ms))
    return (yield from one(Call("GET", f"{path}/{started['id']}", replay=True)))


def wait_command(path: str, command_id: str, timeout: float, config: Settings) -> Steps:
    """Reads a command until it has ended."""
    deadline = time.monotonic() + timeout
    while True:
        command = yield from one(Call("GET", f"{path}/{command_id}", replay=True))
        if command["status"] != "running":
            return command
        if time.monotonic() + config.poll_interval > deadline:
            raise AgentalTimeoutError(f"Command {command_id} was still running after {timeout:g} s.")
        yield Sleep(config.poll_interval)


def run_command(path: str, command: str | list[str] | tuple[str, ...], timeout_ms: int | None, config: Settings) -> Steps:
    started = yield from start_command(path, command, timeout_ms)
    if started["status"] != "running":
        return started
    return (yield from wait_command(path, started["id"], COMMAND_TIMEOUT, config))


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
