"""What the API answers with, as it names it, in snake_case. Times are ISO 8601 strings."""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

Size = Literal["tiny", "small", "medium", "large"]
#: What a sandbox may reach outside itself: everything, nothing, or only the addresses it is given.
Outbound = Literal["open", "blocked", "allowlist"]
#: Something an API key may be made to do, or "*" for everything, now and later.
ApiKeyAction = Literal[
    "sandbox.read",
    "sandbox.create",
    "sandbox.fork",
    "sandbox.update",
    "sandbox.auto-stop",
    "sandbox.stop",
    "sandbox.resume",
    "sandbox.delete",
    "sandbox.delete-own",
    "exec",
    "file.read",
    "file.write",
    "agent.prompt",
    "ssh",
    "desktop",
    "host",
    "network.write",
    "secret.read",
    "secret.write",
    "email.read",
    "email.send",
    "email.manage",
    "snapshot.read",
    "snapshot.write",
    "template.read",
    "template.write",
    "key.manage",
    "account.read",
    "account.admin",
    "members.read",
    "members.admin",
    "*",
]
#: A named set of actions an API key can be made with.
ApiKeyPreset = Literal["read-only", "full-sandbox", "ci", "admin"]

T = TypeVar("T")


def _camel(name: str) -> str:
    # A field named after a word of Python's own, as from_ is, ends in an underscore the API's name has not.
    return re.sub(r"_([a-z])", lambda found: found.group(1).upper(), name.rstrip("_"))


def _shaped(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        inner = [each for each in typing.get_args(hint) if each is not type(None)]
        return _shaped(inner[0], value) if len(inner) == 1 else value
    if origin is list:
        return [_shaped(typing.get_args(hint)[0], each) for each in value]
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return build(hint, value)
    return value


def build(model: type[T], data: dict[str, Any]) -> T:
    """A model from the API's JSON. What the API sends that the model does not name is left out."""
    hints = typing.get_type_hints(model)
    return model(**{field.name: _shaped(hints[field.name], data.get(_camel(field.name))) for field in dataclasses.fields(model)})  # type: ignore[arg-type]


def json_of(model: Any) -> dict[str, Any]:
    """The API's JSON from a model. What is None is left out: the API takes a field that is not sent, and not one that is null."""
    data: dict[str, Any] = {}
    for field in dataclasses.fields(model):
        value = getattr(model, field.name)
        if value is None:
            continue
        if isinstance(value, list):
            value = [json_of(each) if dataclasses.is_dataclass(each) else each for each in value]
        elif dataclasses.is_dataclass(value):
            value = json_of(value)
        data[_camel(field.name)] = value
    return data


@dataclass
class MachineResources:
    vcpu: int
    memory_mb: int
    disk_gb: int


@dataclass
class SandboxNetwork:
    """What a sandbox may reach outside itself: its template's rule with its own addresses on top."""

    #: open: everything. blocked: nothing. allowlist: only the addresses in allow.
    outbound: str
    #: IPv4 addresses and ranges: its template's, then its own. Empty unless outbound is allowlist.
    allow: list[str]


@dataclass
class Egress:
    """Where a sandbox's web traffic leaves: from its server, or through a residential address in a country.

    Sent to create(), set_egress() and Template.set_egress(), and read back from them.
    """

    #: box: from the server the sandbox is on, for nothing. residential: its TCP to ports 80 and 443 leaves through a
    #: residential address in country, at $3 a GB both directions counted, from the account's credits; its UDP to
    #: port 443 is refused, so HTTP/3 falls back to TCP.
    via: Literal["box", "residential"]
    #: With residential, the country, as two letters of ISO 3166-1 such as "de" or "us". None with box.
    country: str | None = None
    #: With residential, "sticky" keeps one address for the sandbox, "rotating" gives each connection another. Left
    #: out, sticky. None with box.
    session: Literal["sticky", "rotating"] | None = None


@dataclass
class EgressRefusal:
    """Why we refused an account residential egress, and when."""

    reason: str
    at: str


@dataclass
class EgressUseCase:
    """What an account told us its sandboxes will use residential addresses for, once for all of them."""

    text: str
    submitted_at: str


@dataclass
class EgressAccount:
    """Whether the account may send sandboxes out residential, what it still needs, and what it has spent and may spend this month.

    Amounts are millionths of a US dollar.
    """

    #: Whether this deployment offers residential egress at all, as policy().residential_egress says.
    offered: bool
    #: Whether a sandbox of the account may go out residential now: offered, nothing missing, and not refused.
    enabled: bool
    #: What it still needs: "plan", Fleet ($100 a month) or larger; "card", a card saved; "use_case", set_egress_use_case().
    missing: list[str]
    #: Set when we refused it residential egress: its sandboxes' residential traffic is refused until we lift it.
    refused: EgressRefusal | None
    #: What it told us its sandboxes will use residential addresses for, or None.
    use_case: EgressUseCase | None
    #: What a GB costs, both directions counted: 3000000 is $3.
    micros_per_gb: int
    #: What its residential traffic has cost this month. It is part of what billing() says it used.
    spent_micros: int
    #: The most its residential traffic costs in a month. Past it, residential connections are refused.
    budget_micros: int
    #: Whether it set its budget. False: it is default_budget_micros.
    budget_set: bool
    default_budget_micros: int
    #: Why its residential connections are refused now, or None: "refused", "out_of_credit", "budget_reached" or "plan".
    #: It is checked each minute.
    blocked: str | None


@dataclass
class TemplateNetwork:
    """What a sandbox on a template may reach outside itself, before its own addresses."""

    #: open: everything. blocked: nothing. allowlist: only the addresses in allow.
    outbound: str
    #: IPv4 addresses and ranges. Empty unless outbound is allowlist.
    allow: list[str]


@dataclass
class SandboxNetworkTemplate:
    """A sandbox's template's outbound rule, as the sandbox has it."""

    template_id: str
    name: str
    outbound: str
    allow: list[str]


@dataclass
class SandboxNetworkOwn:
    """The addresses a sandbox may reach on top of its template's rule."""

    allow: list[str]


@dataclass
class SandboxNetworkDetail:
    """What a sandbox may reach outside itself, and what that comes from."""

    #: The rule it is held to now, as network on the sandbox says it.
    outbound: str
    #: The addresses an allowlist lets out to: its template's, then its own.
    allow: list[str]
    #: Its template's rule as it was when the sandbox last started, was put on the template, or was named by apply().
    template: SandboxNetworkTemplate
    #: They add to an allowlist, make a blocked template's rule an allowlist of them, and change nothing while the template is open.
    own: SandboxNetworkOwn


@dataclass
class SandboxActivity:
    """What a running sandbox is doing, as the service last read it, about once a minute."""

    #: low-cpu (an hour under 5% CPU, observed: a heuristic, as an agent waiting on a model uses almost none),
    #: disk-full, memory-full (90% or more), not-read (its server has not answered for 3 minutes).
    states: list[str]
    #: Its CPU now, and at its busiest in the last hour, 0 to 1 of its vCPUs.
    cpu_now: float | None
    cpu_max_last_hour: float | None
    #: How many of the last hour's minutes were read, since its current run began on its current server.
    observed_minutes: int
    memory_used_bytes: int | None
    memory_total_bytes: int | None
    disk_used_bytes: int | None
    disk_total_bytes: int | None
    read_at: str


@dataclass
class SandboxInfo:
    """A sandbox as the API reports it."""

    id: str
    name: str
    region: str
    size: str
    #: running: use it. stopped: start it. creating, restoring or migrating: wait.
    status: str
    host_id: str | None
    vm_isolation: bool
    resources: MachineResources
    #: What went wrong, or None. On a stopped sandbox, why a start of it failed.
    error: str | None
    created_at: str
    updated_at: str
    stopped_at: str | None
    #: Seconds it runs after a start before it is stopped. None is the deployment's default.
    ttl_seconds: int | None
    #: When it is to be stopped: the time set_auto_stop() moved the stop of this run to, or else its last start and ttl_seconds. None when it is not running.
    auto_stop_at: str | None
    subdomain: str | None
    shared: bool
    #: The template it is on.
    template_id: str | None
    #: The organization it belongs to, which pays for it.
    organization_id: str
    #: The account that made it. None for the dev key's, and once that account is erased.
    created_by: str | None
    #: True when its disk is kept in storage. A start brings it back, which takes longer.
    archived: bool
    archived_at: str | None
    #: When it was last stopped and started again to be moved to another server. Only on older sandboxes: none is moved now.
    migrated_at: str | None
    #: What it may reach outside itself.
    network: SandboxNetwork
    #: Its template's tags and its own, each once. None from an API older than it.
    tags: list[str] | None = None
    #: Its template's metadata, with its own value for a key in place of the template's. None from an API older than it.
    metadata: dict[str, str] | None = None
    #: Of its tags, the ones it has itself. None from an API older than it.
    own_tags: list[str] | None = None
    #: Of its metadata, the keys it sets itself. None from an API older than it.
    own_metadata: dict[str, str] | None = None
    #: True while it is stopped with its memory kept: a start puts back every process as it was. None from an API older than it.
    memory_kept: bool | None = None
    #: Why the memory its last stop kept was not put back by its last start, which started it from its disk. None when it was, or none was kept.
    memory_lost: str | None = None
    #: What it is doing, as last read: None when it is not running, not read yet, or metrics are not read here.
    activity: SandboxActivity | None = None


@dataclass
class SandboxAutoStop:
    """When a running sandbox is to be stopped, how many seconds that is from now, and what decided it."""

    sandbox_id: str
    status: str
    #: None when it is not running, or has no time limit.
    auto_stop_at: str | None
    #: Seconds from now until then. None with auto_stop_at.
    seconds_left: int | None
    #: Seconds each start runs before it is stopped, unless the stop is moved. None is the deployment's default.
    ttl_seconds: int | None
    #: ttl: its last start and ttl_seconds. account: a move by one of its account's keys, or the console.
    #: sandbox: a move by the sandbox itself. None with auto_stop_at.
    set_by: Literal["ttl", "account", "sandbox"] | None


@dataclass
class TemplateStart:
    """What a sandbox made from a template starts as."""

    size: str
    #: The snapshot it starts from, or None for a new machine.
    snapshot_id: str | None
    #: Seconds a sandbox made from it runs after each start before it is stopped. None is the deployment's default, and so it is from an API older than it.
    ttl_seconds: int | None = None


@dataclass
class TemplateInfo:
    """A template as the API reports it: its start, its outbound rule, and the names of its variables and files, never their values."""

    id: str
    name: str
    #: Whether it is the account's default, which a sandbox is on when none is chosen.
    default: bool
    start: TemplateStart
    variables: list[str]
    #: Of its variables, the ones kept out of the sandbox.
    hidden: list[str]
    files: list[str]
    #: What a sandbox on it may reach outside itself, before its own addresses.
    network: TemplateNetwork
    #: Raised by every change to its variables, files and outbound rule.
    revision: int
    #: How many sandboxes are on it.
    sandboxes: int
    created_at: str
    updated_at: str
    #: What a sandbox made from it is given for SSH as it is made. None from an API older than it.
    ssh: TemplateSsh | None = None
    #: Tags every sandbox on it has, on top of its own. None from an API older than it.
    tags: list[str] | None = None
    #: Metadata every sandbox on it has, for the keys it does not set itself. None from an API older than it.
    metadata: dict[str, str] | None = None


@dataclass
class SandboxTemplate:
    """Which template a sandbox is on, and whether it has the template's secrets as they are now."""

    sandbox_id: str
    template_id: str
    name: str
    default: bool
    #: False until it next starts, or apply() names it, after a change to the template.
    up_to_date: bool
    taken_at: str
    #: The names of the variables it has from the template. Its own come on top of these.
    variables: list[str]
    #: Of those, the ones kept out of the sandbox, as it has them.
    hidden: list[str]
    files: list[str]


@dataclass
class PlainVariable:
    """A variable whose commands read its value."""

    name: str
    #: Always False: hidden says which of the two a variable is.
    hidden: Literal[False]
    value: str


@dataclass
class HiddenVariable:
    """A variable kept out of the sandbox. Its value is never given back, to its owner or anyone.

    Its commands see stand_in in its place, and the proxy on the sandbox's server puts the
    value in place of the stand-in on HTTPS requests to its hosts, and nowhere else.
    """

    name: str
    #: Always True: hidden says which of the two a variable is.
    hidden: Literal[True]
    #: Where its value may be sent: hostnames, and wildcards as *.example.com, which cover every name under example.com and not example.com itself.
    hosts: list[str]
    #: What its commands see in its place. It stays the same when the value is replaced, and sent anywhere but its hosts it is worth nothing.
    stand_in: str


#: A variable of a template or of a sandbox: a plain one with its value, or one kept out of the sandbox with its hosts and its stand-in.
SecretVariable = PlainVariable | HiddenVariable


@dataclass
class TemplateSandbox:
    """A sandbox on a template."""

    id: str
    name: str
    status: str
    up_to_date: bool
    taken_at: str | None
    #: The addresses it may reach on top of the template's outbound rule.
    allow: list[str]


@dataclass
class ApplyFailure:
    """A sandbox whose server did not take its template's outbound rule, and what went wrong."""

    sandbox_id: str
    error: str


@dataclass
class TemplateApplied:
    template_id: str
    revision: int
    #: The sandboxes that were brought up to date.
    applied: list[str]
    #: The sandboxes whose server did not take the new outbound rule. Each is held to the rule it had, and is not up to date.
    failed: list[ApplyFailure]


@dataclass
class ExecResult:
    """What a command ended with. A command that failed is still an answer: read exit_code."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


@dataclass
class ExecEvent:
    """Output of a command as it arrives, then what it exited with.

    ``type`` is ``stdout`` or ``stderr`` with ``data``, then ``exit`` with ``exit_code``.
    """

    type: Literal["stdout", "stderr", "exit"]
    data: str = ""
    exit_code: int | None = None


@dataclass
class TerminalExit:
    """How a sandbox's terminal ended.

    ``close_code`` is 1000 when the shell ended, and then ``exit_code`` says what it exited with.
    4003: what let it be opened no longer does (a key taken away, a session ended, the sandbox
    stopped). 4008: nothing was typed for 30 minutes. 4011: it could not be opened, or its sandbox
    stopped answering. 1006: the connection was lost.
    """

    #: What the shell exited with, or None when the terminal was closed before the shell said.
    exit_code: int | None
    #: The signal that ended the shell, 0 when none did.
    signal: int
    #: Whether the shell was ended because a terminal is kept open at most 12 hours.
    timed_out: bool
    #: The WebSocket's close code.
    close_code: int
    #: The close reason the API gave, in words.
    reason: str
    #: The code of the error the API said before it closed the terminal, when it said one.
    error_code: str | None = None
    #: That error's message.
    error_message: str | None = None


@dataclass
class Command:
    """A command left running in the background, and its output so far."""

    id: str
    sandbox_id: str
    pid: int | None
    #: running, succeeded, failed or timed_out.
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: str
    finished_at: str | None


@dataclass
class ScheduleRun:
    """One run of a schedule: when it was due, how it ended, and whether it started and stopped its sandbox."""

    id: str
    schedule_id: str
    sandbox_id: str
    due_at: str
    #: When its command began. None for one that did not run.
    started_at: str | None
    finished_at: str | None
    #: running; succeeded, failed or timed_out, as its command ended; replaced, skipped, missed, could_not_start or interrupted.
    outcome: str
    #: Why it ended as it did, in words, where the outcome does not say.
    reason: str | None
    exit_code: int | None
    #: Its command, whose output Sandbox.commands.get() reads.
    command_id: str | None
    #: Whether this run started its sandbox, which was stopped.
    started_sandbox: bool
    #: disk or memory: how this run stopped the sandbox it started. None when it did not stop it.
    stopped_sandbox: str | None
    #: Whether this run deleted its sandbox after its command exited 0, as its schedule's delete_after asks. False from an API older than that.
    deleted_sandbox: bool = False


@dataclass
class Schedule:
    """A command a sandbox runs on a schedule, with its last run."""

    id: str
    sandbox_id: str
    name: str
    #: What it runs, through sh -c.
    command: str
    #: How often, as 15m, 1h or 1d, counted from when it was made. None when cron says.
    every: str | None
    #: When, as a five-field cron expression in time_zone. None when every or at says.
    cron: str | None
    #: The one time it runs, for a schedule that runs once. None for one that runs again and again.
    at: str | None
    time_zone: str
    #: How long a run may take, in milliseconds, as the API says it. create() and update() take it in seconds, as timeout.
    timeout_ms: int
    #: start: a run starts a stopped sandbox, and stops it again after. skip: a run that finds it stopped is skipped.
    if_stopped: str
    #: Whether a run that started the sandbox stops it with its memory kept.
    keep_memory: bool
    #: skip, replace or parallel: what a run does while the last one still goes.
    overlap: str
    enabled: bool
    #: When it next runs. None while it is paused, and for one that ran once.
    next_run_at: str | None
    #: Why it paused itself, after runs one after another failed. None for one paused by hand, or running.
    paused_reason: str | None
    last_run: ScheduleRun | None
    created_at: str
    updated_at: str
    #: Whether its run deletes the sandbox after, where its command exits 0. Only a schedule that runs once has it. False from an API older than that.
    delete_after: bool = False


@dataclass
class Snapshot:
    id: str
    sandbox_id: str
    name: str
    created_at: str
    browsable: bool
    size: str | None
    sandbox_deleted: bool


@dataclass
class HostRoute:
    """A port of a sandbox, and the address it is reached at."""

    id: str
    sandbox_id: str
    port: int
    title: str
    public: bool
    hidden: bool
    proxy_path: str
    registered: bool
    listening: bool
    proxy: str
    #: The public address. None when the route is not public or public addresses are off.
    url: str | None


@dataclass
class DesktopState:
    """Whether a sandbox has a desktop turned on, the size of its screen, and where it is watched."""

    #: Whether a desktop is turned on for the sandbox. It stays so through a stop and a start.
    enabled: bool
    enabled_at: str | None
    #: Whether the desktop is running in the sandbox's machine now.
    running: bool
    width: int | None
    height: int | None
    #: What DISPLAY is to be for a command that opens a window on it. None while it is off.
    display: str | None
    #: Where it is watched and used, in a browser. Keep it as a password is kept. None while it is not running.
    url: str | None


@dataclass
class SshState:
    """Whether SSH into a sandbox is on, and where to connect while it is."""

    #: Whether SSH is turned on for the sandbox. It stays so through a stop and a start.
    enabled: bool
    enabled_at: str | None
    #: host:port, as ssh -p takes them apart. None while it is off.
    address: str | None
    host: str | None
    port: int | None
    #: The user to log in as: root. None while it is off.
    user: str | None
    #: The machine's host key, to pin in known_hosts. None while it is off.
    host_key: str | None


@dataclass
class SshKey:
    """A key that may log in to a sandbox as root."""

    #: What remove_key() takes.
    id: str
    fingerprint: str
    type: str
    #: What the key's line says after it, as user@host. None when it says nothing.
    comment: str | None


@dataclass
class TemplateSsh:
    """What a sandbox made from a template is given for SSH as it is made."""

    #: Whether SSH is turned on for it as it is made, as Sandbox.ssh.on() would.
    enabled: bool
    enabled_at: str | None
    #: The keys let in to its machine as it is made, in the order they were added.
    keys: list[SshKey]


@dataclass
class ClosedAccount:
    """When an account was closed, and when it will be erased unless it is reopened before then."""

    closed_at: str
    erase_at: str


@dataclass
class Me:
    """Who a key or session belongs to."""

    id: str
    name: str
    email: str | None
    role: str
    active_organization_id: str
    #: When the account was closed and will be erased. None while it is open.
    closed: ClosedAccount | None


#: A role in a team. An owner is made from a member who is there; an invite asks for member or admin.
TeamRole = Literal["owner", "admin", "member"]


@dataclass
class Organization:
    """An organization the key's person is in: their own, or a team, with their role in it."""

    id: str
    name: str
    personal: bool
    #: The person's role in it.
    role: TeamRole | None
    #: Whether its plan lets it have members besides its owners: the $500 and $2000 plans.
    members_allowed: bool


@dataclass
class Team:
    """A team, with the caller's role in it."""

    id: str
    name: str
    personal: bool
    created_at: str
    role: TeamRole


@dataclass
class Member:
    """Someone in a team, with their role and when they were added."""

    user_id: str
    name: str | None
    email: str | None
    role: TeamRole
    added_at: str
    added_by: str | None


@dataclass
class Invite:
    """An invitation to a team, open for seven days."""

    id: str
    email: str
    role: Literal["admin", "member"]
    #: open, accepted, revoked or expired.
    status: str
    invited_by: str | None
    created_at: str
    expires_at: str
    #: sent when it was emailed, email_not_configured where mail is not set up and the link is given by hand.
    delivery: str


@dataclass
class BrowserState:
    """Whether a sandbox's browser is turned on to be driven from outside it, whether it has a window, and where to connect."""

    #: Whether the browser is turned on for the sandbox. It stays so through a stop and a start.
    enabled: bool
    enabled_at: str | None
    #: Whether Chrome is running in the sandbox's machine now and its DevTools answer.
    running: bool
    #: Whether Chrome has no window. False when it is on the desktop. None while it is off.
    headless: bool | None
    #: Where its DevTools are reached from outside, over a WebSocket. Keep it as a password is kept. None while it is not running.
    cdp_url: str | None
    #: Where the same DevTools are reached from inside the sandbox. None while it is off.
    local_cdp_url: str | None


@dataclass
class DesktopRegion:
    """A part of a desktop's screen, in pixels of the screen."""

    x: int
    y: int
    width: int
    height: int


@dataclass
class DesktopScreenshot:
    """A picture of a sandbox's screen in base64, which is what a model is handed.

    A point of the picture is at region.x + x * region.width / width on the screen.
    """

    #: image/png or image/jpeg.
    media_type: str
    width: int
    height: int
    #: The part of the screen the picture shows.
    region: DesktopRegion
    base64: str


@dataclass
class DesktopLink:
    """A link to a sandbox's desktop: to watch it only, or to use it as well, until it expires."""

    id: str
    #: view to watch the screen only, control to use it as well.
    access: str
    #: Where it leads, in a browser. Keep it as a password is kept until it expires.
    url: str | None
    created_at: str
    expires_at: str
    #: Whether a stream that is open through it is cut when it expires.
    cut_on_expiry: bool


@dataclass
class FileWritten:
    path: str
    bytes: int


@dataclass
class Restored:
    snapshot: Snapshot
    #: Ids of the snapshots taken since, which a roll back discards.
    discarded: list[str]


@dataclass
class SizeInfo:
    name: str
    vcpu: int
    memory_mb: int
    disk_gb: int


@dataclass
class Catalog:
    """The default region, how many sandboxes there are, and the sizes on offer."""

    region: str
    used: int
    sizes: list[SizeInfo]
    vm_isolation: bool
    #: The regions sandboxes can be made in now, the default first. Empty from an API older than regions.
    regions: list[str] = field(default_factory=list)


@dataclass
class Region:
    """A region sandboxes can be made in: what it is called, where its servers are, and the sizes it has room for now."""

    name: str
    label: str
    about: str
    #: True for the region a create that names none is made in.
    default: bool
    sizes_with_room: list[str]


@dataclass
class SignIn:
    password: bool
    email_code: bool
    github: bool
    google: bool
    #: A passkey, for an account that has added one: both steps of two-factor sign-in at once. False from an API older than passkeys.
    passkey: bool = False


@dataclass
class PolicyEmail:
    """Sandbox mail: whether it is offered here. It is off for each sandbox until that sandbox's owner turns it on."""

    #: Whether a sandbox can be sent mail.
    receiving: bool
    #: Whether a sandbox can send mail. It can write to one address only: its own account's.
    sending: bool
    #: The domain every sandbox's mail domain is under. None where there is no mail.
    domain: str | None


@dataclass
class Policy:
    """What applies to every account here."""

    signup: str
    sandboxes_per_account: int | None
    default_ttl_seconds: int | None
    max_ttl_seconds: int | None
    public_urls: bool
    route_domain: str | None
    routes_per_sandbox: int | None
    forks: bool
    archiving: bool
    archive_after_seconds: int | None
    email: PolicyEmail
    password_sign_up: bool
    sign_in: SignIn
    #: Whether a sandbox can be reached over SSH here.
    ssh: bool
    #: Whether a sandbox can be given a desktop here.
    desktop: bool
    #: Whether a sandbox's browser can be driven from outside it here, over the Chrome DevTools Protocol.
    browser: bool
    #: Whether a sandbox can be held to an outbound rule here.
    network_policy: bool
    #: Whether a variable can be kept out of its sandbox here: set with hosts, its commands see a stand-in in its place.
    hidden_variables: bool
    #: Whether a sandbox's CPU, memory, disk and network are read and kept here.
    metrics: bool
    #: Whether an account's sandbox metrics and lifecycle events can be sent to a collector of its own here.
    telemetry: bool
    #: Whether GitHub Actions jobs can run on sandboxes here: an organization connects GitHub in the console, and a job asks for runs-on: agental or agental-<size>. False from an API older than that.
    runs_on: bool = False
    #: Whether a sandbox can be stopped with its memory kept here: stop(keep_memory=True). False from an API older than that.
    keep_memory: bool = False
    #: Whether a sandbox's terminal can be opened here: a login shell in it, over a WebSocket (POST /v1/sandboxes/{id}/terminal). False from an API older than that.
    terminal: bool = False
    #: The npm package the agental command is published as, for npx <it>@latest. None until it is published.
    cli_package: str | None = None
    #: Where the console is, such as https://console.agental.io: its Billing page is console_url/billing.
    console_url: str | None = None
    #: Whether a sandbox's web traffic can leave through a residential address here, at $3 a GB. False from an API older than that.
    residential_egress: bool = False


@dataclass
class MailUsage:
    """Mail in a window. It is counted by UTC day, and deleting a message, or a sandbox, takes nothing away from it."""

    #: Messages taken for a sandbox.
    received: int
    #: Messages a sandbox sent.
    sent: int
    #: What they come to at 500 a message, in millionths of a US dollar: $0.50 a thousand.
    cost_micros: int


@dataclass
class StoppedUsage:
    """Stopped time in a window, with the disk kept, and what it comes to at the stopped price: 5% of the running one."""

    seconds: int
    #: Not a charge here: Billing says what is.
    list_cost_micros: int


@dataclass
class SnapshotsUsage:
    """Snapshots past the account's free ones in a window."""

    #: Snapshots kept at the end of the window.
    kept: int
    #: How many of the oldest cost nothing.
    free: int
    #: Seconds of snapshots kept past the free ones: two of them for an hour is 7,200.
    charged_seconds: int
    #: At $1.70 a month each. Not a charge here: Billing says what is.
    list_cost_micros: int


@dataclass
class SandboxUsage:
    sandbox_id: str
    duration_ms: int
    since: str | None
    until: str
    seconds: int
    list_cost_micros: int
    mail: MailUsage
    currency: str
    cost: None
    billing: str
    #: Stopped time, apart from the running time above. None from an API older than it.
    stopped: StoppedUsage | None = None


@dataclass
class UsageBySandbox:
    sandbox_id: str
    name: str | None
    size: str
    running: bool
    seconds: int
    list_cost_micros: int
    #: Seconds it was stopped in the window, with its disk kept, and what they come to at the stopped price.
    stopped_seconds: int = 0
    stopped_list_cost_micros: int = 0


@dataclass
class UsageByKey:
    api_key_id: str | None
    name: str | None
    sandboxes: int
    seconds: int
    list_cost_micros: int


@dataclass
class UsageByMember:
    """The running time of the sandboxes one person made: in a team, by member."""

    #: Who made them. None for sandboxes made by whoever runs the service.
    user_id: str | None
    sandboxes: int
    seconds: int
    list_cost_micros: int


@dataclass
class DaySnapshotsUsage:
    """Snapshots past the free ones on one day."""

    charged_seconds: int
    list_cost_micros: int


@dataclass
class UsageByDay:
    """One UTC day: its running time, and beside it the day's stopped time, snapshots and mail. Added up, the days come to the totals."""

    day: str
    seconds: int
    list_cost_micros: int
    #: None from an API older than it, as are the two below.
    stopped: StoppedUsage | None = None
    snapshots: DaySnapshotsUsage | None = None
    mail: MailUsage | None = None


@dataclass
class UsageByHour:
    """One UTC hour in which a sandbox ran: its running time, and how many sandboxes ran in it."""

    #: The first moment of the hour.
    hour: str
    sandboxes: int
    seconds: int
    list_cost_micros: int


@dataclass
class MailUsageBySandbox:
    sandbox_id: str
    name: str | None
    received: int
    sent: int
    cost_micros: int


@dataclass
class AccountMailUsage(MailUsage):
    """The account's mail in a window: the total, and a line for each sandbox that had any."""

    sandboxes: list[MailUsageBySandbox]


@dataclass
class Usage:
    """Running time and what it comes to at the list price, by sandbox, by key, by day and by hour, with stopped time, snapshots and mail of the same window beside it."""

    since: str
    until: str
    seconds: int
    list_cost_micros: int
    mail: AccountMailUsage
    sandboxes: list[UsageBySandbox]
    keys: list[UsageByKey]
    days: list[UsageByDay]
    currency: str
    cost: None
    billing: str
    #: Stopped time, apart from the running time above. None from an API older than it.
    stopped: StoppedUsage | None = None
    #: Snapshots past the free ones. None from an API older than it.
    snapshots: SnapshotsUsage | None = None
    #: Each hour in which a sandbox ran, the earliest first. None from an API older than it.
    hours: list[UsageByHour] | None = None
    #: The same running time by the person who made each sandbox, the most first. None from an API older than it.
    members: list[UsageByMember] | None = None


@dataclass
class SandboxEmail:
    """Whether mail is on for a sandbox, and the domain its addresses end in."""

    #: Off, nothing sent to the sandbox is taken and nothing is charged for it. It is off until it is turned on.
    enabled: bool
    #: Since when it is on. None while it is off.
    enabled_at: str | None
    #: What every address of the sandbox ends in: any name before the @ reaches it. None where no mail is offered.
    domain: str | None


@dataclass
class EmailAttachment:
    filename: str | None
    content_type: str
    size_bytes: int


@dataclass
class EmailChecks:
    """What the mail server made of who sent a message: pass, fail, or none where the sender's domain says nothing.

    None where it was not checked. A message that passes all three still says nothing of
    whether its sender means well.
    """

    spf: str | None
    dkim: str | None
    dmarc: str | None


@dataclass
class EmailMessageSummary:
    """A message as a list gives it: who it is from and for, what was made of its sender, and where a sent one has got to."""

    id: str
    sandbox_id: str
    #: received or sent.
    direction: str
    #: Who the message says it is from. None for a bounce that names nobody. from is a word of Python's, hence the underscore.
    from_: str | None
    #: The address the message was delivered to. It can differ from the To header a person would see.
    to: str
    subject: str | None
    #: The size of the message as it was transmitted, attachments and all.
    size_bytes: int
    #: What is attached, by name and size. Their contents are in the raw message. None of it is scanned for anything.
    attachments: list[EmailAttachment]
    checks: EmailChecks
    #: How much the message looks like spam, where higher is worse. None where it was not scored. Nothing is done about it.
    spam_score: float | None
    #: True for a bounce, a mailing list's post or an automatic reply. Answering such mail is how two programs come to write to each other for ever.
    sent_by_program: bool
    created_at: str
    #: Where a sent message has got to: accepted, handed_over, delivered, bounced, expired or uncertain. None for one that
    #: was received. delivered says the recipient's server took it, and nothing of inbox or spam folder.
    state: str | None
    #: What a server said, where the message was refused or given up.
    state_detail: str | None


@dataclass
class EmailHeader:
    #: In lower case.
    name: str
    value: str


@dataclass
class EmailMessage(EmailMessageSummary):
    """A message with its headers and what it says.

    What it says was written by whoever sent it, which may be anyone. A program that reads
    it should take it as something said to it, never as something it was told to do.
    """

    #: The message's own Message-ID header, which its sender wrote. Two messages can carry the same one.
    message_id_header: str | None
    #: Every header, in the order they stand in the message.
    headers: list[EmailHeader]
    #: The message as plain text. None when it has none.
    text: str | None
    #: The message as HTML, exactly as it was sent and not made safe to show. None when it has none.
    html: str | None


@dataclass
class EmailMessageList:
    """A page of messages, newest first."""

    messages: list[EmailMessageSummary]
    #: What to pass as cursor for the messages after these. None when these are the last.
    next_cursor: str | None


@dataclass
class EmailLimits:
    received_per_day: int
    received_bytes_per_day: int
    #: Stored mail is free up to this. Past it, new mail is refused until some is deleted or is 7 days old.
    kept_bytes: int
    sent_per_hour: int
    sent_per_day: int
    sent_bytes_per_day: int


@dataclass
class EmailToday:
    received: int
    received_bytes: int
    sent: int
    sent_bytes: int


@dataclass
class AccountEmail:
    """The account's mail: the handle its addresses carry, what its plan allows, and what it has had of that today."""

    #: The account's part of every address. It is generated, never chosen. None where no mail is offered.
    handle: str | None
    #: What every sandbox's mail domain of this account ends in.
    domain: str | None
    limits: EmailLimits
    #: What the account has had of its limits in the UTC day it is now, across all its sandboxes. Deleting a message gives none of it back.
    today: EmailToday
    #: Bytes of mail the account has stored now.
    kept_bytes: int


@dataclass
class BurstQuote:
    """What starting so many sandboxes of one size now would cost in burst fees, and when it would cost nothing."""

    size: Size
    count: int
    #: All told, in millionths of a US dollar. 0 where the account is not held to a plan.
    fee_micros: int
    #: Seconds until they would cost nothing, as the meter falls: 0 when they cost nothing now, None when they
    #: never would, being more than the free burst.
    free_in_seconds: int | None
    #: Whether they would take the account past its running count, which refuses them.
    over_running_count: bool


@dataclass
class Burst:
    """Where the account stands against its plan: its running count, its burst meter against the free burst, and its burst fees against its budget.

    Counts are in small sandboxes: a tiny counts half, a medium 2, a large 4. Amounts are
    millionths of a US dollar.
    """

    #: Whether the account is held to its plan: a start past the running count is refused, and a fast one costs a
    #: fee. False: what it starts is only measured.
    enforced: bool
    #: The plan it is on, "free" included, or None where plans are not enforced on it.
    plan: str | None
    #: Whose limits it has: its plan's, a plan's we gave it ("hand"), or, where plans are not enforced on it, the free plan's ("none").
    limits_from: Literal["plan", "hand", "none"]
    #: The plan whose limits it has.
    limits_plan: str
    #: Small sandboxes it runs at once at most. A create, fork or start past it is refused with running_limit_reached.
    running_units: int
    #: Small sandboxes it starts at once for nothing. Starting faster costs a burst fee.
    free_burst_units: int
    #: The memory of its sandboxes running, being made or being started.
    running_mb: int
    #: running_units in megabytes: 4096 each.
    cap_mb: int
    #: free_burst_units in megabytes.
    free_mb: int
    #: What its burst meter reads: the memory of each start it made, halved for every half_life_minutes since.
    meter_mb: int
    #: What it may start now for nothing: free_mb less meter_mb, and no more than the room under cap_mb.
    free_now_mb: int
    #: By how much the meter is over the free burst. While it is, every start costs a fee.
    over_mb: int
    half_life_minutes: int
    #: See BurstFee.
    fee_micros_per_gib: int
    #: Whether it may start a sandbox at all now. A plan out of credit may not: billing() says why.
    may_start: bool
    #: Whether it is on the free plan and has paid for nothing yet: until it has, it runs and starts at most
    #: Prices.free.unpaid_units small sandboxes at once.
    unpaid: bool
    #: What it has spent on burst fees this month.
    spent_micros: int
    #: The most it spends on burst fees in a month. A start that would go past it is refused with burst_budget_reached.
    budget_micros: int
    #: Whether the account set its budget. False: it is default_budget_micros.
    budget_set: bool
    default_budget_micros: int
    #: What one sandbox of each size its plan makes would cost to start now.
    next_start: list[BurstQuote]
    #: With burst(size=...), what starting that many of that size now would cost. None without.
    quote: BurstQuote | None


@dataclass
class SendEmailAttachment:
    filename: str
    #: The file's bytes in base64.
    content_base64: str
    content_type: str | None = None


@dataclass
class SendEmail:
    """A message for a sandbox to send, as the API takes it. It goes to the address of the sandbox's own account and to no other."""

    subject: str
    #: The message, as plain text.
    text: str
    #: It can only be the address of the sandbox's own account, which is who the message is for when this is left out.
    to: str | None = None
    #: What stands before the @ of the address the message is from. The rest is the sandbox's mail domain. sandbox unless it is said.
    from_: str | None = None
    #: The name shown beside that address.
    from_name: str | None = None
    #: The id of a message this sandbox received, which this one answers. It gives no leave to write to whoever sent that message.
    reply_to: str | None = None
    attachments: list[SendEmailAttachment] | None = None


@dataclass
class MetricsReading:
    """What a sandbox read at one moment. A figure its server did not have then is None."""

    at: str
    #: The share of its CPUs' time that was not idle, 0 to 1.
    cpu_utilization: float | None
    #: Memory in use, as the sandbox counts it: what it has less what is available.
    memory_used_bytes: float | None
    memory_total_bytes: float | None
    #: What of its root disk cannot be written to: its size less what is available.
    disk_used_bytes: float | None
    disk_total_bytes: float | None
    network_received_bytes_per_second: float | None
    network_sent_bytes_per_second: float | None
    disk_read_bytes_per_second: float | None
    disk_written_bytes_per_second: float | None


@dataclass
class SandboxMetrics:
    """What one sandbox read between two moments."""

    sandbox_id: str
    since: str
    until: str
    step_seconds: int
    #: How long a reading is kept, in days.
    kept_days: int
    #: Oldest first. There is no point for a moment the sandbox was stopped at.
    points: list[MetricsReading]
    #: False when a server did not answer, so that readings it holds may be missing.
    complete: bool


@dataclass
class SandboxAtMoment:
    sandbox_id: str
    name: str
    size: str
    #: None when the sandbox was stopped then, or was started less than a minute before.
    reading: MetricsReading | None


@dataclass
class MetricsAtMoment:
    """What each of your sandboxes read at one moment."""

    at: str
    sandboxes: list[SandboxAtMoment]
    #: False when a server did not answer, so that a sandbox on it has no reading though it was running.
    complete: bool


@dataclass
class RunningAt:
    at: str
    running: int


@dataclass
class RunningOverTime:
    """How many of your sandboxes were running, step by step."""

    since: str
    until: str
    step_seconds: int
    points: list[RunningAt]


@dataclass
class TelemetryError:
    """The last thing that went wrong in sending metrics or events to a collector."""

    message: str
    #: What the collector answered, or None where it did not answer or nothing was sent.
    status: int | None
    at: str


@dataclass
class Telemetry:
    """Where your sandboxes' metrics and events are sent, the names of its headers, and how the last export went."""

    #: The collector's OTLP/HTTP base address. Metrics go to /v1/metrics under it and events to /v1/logs.
    endpoint: str
    #: The names of the headers sent with each export. Their values are never shown.
    headers: list[str]
    #: When the collector last took the metrics. None until it has.
    last_export_at: str | None
    #: None until something has gone wrong.
    last_error: TelemetryError | None
    created_at: str
    updated_at: str


@dataclass
class RetentionCleared:
    """What one clean-up deleted of an account's sandboxes' history."""

    at: str
    commands: int
    conversations: int
    events: int


@dataclass
class RetainedData:
    """Something agental keeps for a period of its own, which the account does not choose."""

    what: str
    kept: str


@dataclass
class DataRetention:
    """How long your sandboxes' history is kept, and what agental keeps besides."""

    #: Days a command's output, a conversation and an event are kept: 1, 7, 30 or 90. None keeps them until the sandbox is deleted.
    keep_days: int | None
    #: Whether history is deleted on a schedule at all: keep_days is not None.
    enabled: bool
    #: The periods keep_days may take, in days.
    choices: list[int]
    #: What an account that has not chosen keeps.
    default_keep_days: int
    #: The last clean-up that deleted any of the account's history. None until one has.
    last_cleared: RetentionCleared | None
    #: What agental keeps for periods of its own.
    retained: list[RetainedData]


@dataclass
class TelemetryTest:
    """What your collector answered one empty export."""

    #: True when the collector took the export.
    ok: bool
    #: What the collector answered, or None where it did not answer or nothing was sent.
    status: int | None
    #: What went wrong, or what the collector said it did not take.
    message: str | None


@dataclass
class RunsOnSandbox:
    """One sandbox of a repository's runs-on pool."""

    sandbox_id: str
    name: str
    size: str
    #: Whether a job holds it now.
    busy: bool


@dataclass
class RunsOnRepository:
    """A repository's runs-on pool: how many sandboxes of each size it keeps, the template they are made on, and the ones it has."""

    #: owner/name, in lower case.
    repository: str
    #: The most sandboxes of each size its pool keeps. None: the deployment's.
    pool_max: int | None
    #: The template its pool sandboxes are made on. None: the empty runs-on template. A fork's job always gets the empty one.
    template_id: str | None
    sandboxes: list[RunsOnSandbox]
    #: The size a job that asks for runs-on: agental gets: its template's, or medium with none. agental-<size> gets that size. None from an API older than that.
    size: str | None = None


@dataclass
class RunsOnRepositories:
    """Each repository's runs-on pool, and the size a pool keeps where none was set."""

    #: Whether GitHub Actions jobs run on sandboxes here.
    available: bool
    default_pool_max: int | None
    repositories: list[RunsOnRepository]


@dataclass
class WebhookDelivery:
    """One try at posting an event to a webhook, and what came of it."""

    id: str
    event: str
    #: succeeded when the server answered with a 2xx, failed otherwise.
    status: str
    #: What the server answered, or None where nothing answered.
    http_status: int | None
    error: str | None
    created_at: str
    #: The event this was a try of: the id in what was sent. None for a try made before events were tried again.
    event_id: str | None
    #: Which try of that event this was, counted from 1.
    attempt: int | None


@dataclass
class Webhook:
    """An address your account's events are posted to, with its tries of the last 30 days."""

    id: str
    url: str
    #: The kinds of event it is sent, or ["*"] for every one and any added later.
    events: list[str]
    description: str
    created_at: str
    updated_at: str
    deliveries: list[WebhookDelivery]
    #: Given when it is made or given a new one, and never again.
    signing_secret: str | None = None


@dataclass
class ApiKey:
    """An API key: what it may do, which sandboxes and templates it reaches, until when, and from where. Never its secret."""

    id: str
    name: str
    #: The first characters of the secret, to tell keys apart by. None for a key from before they were kept.
    prefix: str | None
    created_at: str
    #: True for a key of whoever runs the service, which sees every account.
    sees_all: bool
    #: The preset it was made with, or None for one made with its actions named.
    preset: ApiKeyPreset | None
    #: What it may do, as it was made: a preset is written out into its actions then, so a preset that grows later does not widen it.
    actions: list[ApiKeyAction]
    #: The sandboxes it is limited to, beside those on template_ids. Both empty is every sandbox of the account.
    sandbox_ids: list[str]
    #: The templates it is limited to: it reaches them and every sandbox on them. Empty is every template.
    template_ids: list[str]
    #: When it stops working. None is never.
    expires_at: str | None
    #: The addresses and ranges it may be used from. Empty is anywhere.
    allow_from: list[str]
    #: When it was last used, to the minute. None if never.
    last_used_at: str | None
    #: The address it was last used from.
    last_used_from: str | None
    #: The organization it acts in, for good: the one it was made in. None for a key of whoever runs the service.
    organization_id: str | None
    #: The account that made it. In a team, its owners and admins see every key of it, and a member only their own.
    created_by: str | None


@dataclass
class CreatedApiKey(ApiKey):
    """A key just made, with its secret."""

    #: The key itself. It is given this once, and only its hash is kept.
    secret: str


@dataclass
class CreateApiKey:
    """A key to make, as the API takes it: a name, a preset or the actions named, never both, and what limits it."""

    name: str
    #: read-only, full-sandbox, ci or admin.
    preset: ApiKeyPreset | None = None
    actions: list[ApiKeyAction] | None = None
    #: It reaches these sandboxes, those on template_ids, and the ones it makes. Every other sandbox answers it 404.
    sandbox_ids: list[str] | None = None
    #: It reaches these templates and the sandboxes on them. With several and not the default, a sandbox it makes has to name one.
    template_ids: list[str] | None = None
    #: An ISO 8601 date and time with its offset. Left out, never.
    expires_at: str | None = None
    #: IPv4 and IPv6 addresses and ranges, 100 at most. Left out, anywhere.
    allow_from: list[str] | None = None
    #: A code from the authenticator app, or a backup code, for a person signed in to the site with two-factor sign-in on. A key needs none.
    code: str | None = None


@dataclass
class ApiKeyUsage:
    """How long the sandboxes one key made ran, and what that comes to at the list price."""

    api_key_id: str
    created_at: str
    age_ms: int
    #: Start of the window. None is everything the key's sandboxes ever ran.
    since: str | None
    until: str
    #: Sandboxes the key made that ran in the window.
    sandboxes: int
    seconds: int
    #: In millionths of a US dollar. Not a charge.
    list_cost_micros: int
    currency: str
    cost: None
    billing: str


@dataclass
class SizePrice:
    """A size, and what an hour of it costs at list price."""

    size: Size
    vcpu: int
    memory_mb: int
    disk_gb: int
    #: In millionths of a US dollar: 27000 is $0.027.
    list_price_micros_per_hour: int
    currency: str
    label: str
    #: What an hour of it stopped costs, with its disk kept: 5% of the list price. None from an API older than it.
    stopped_micros_per_hour: int | None = None
    #: What an hour of it stopped with its memory kept costs, its disk kept too: 10% of the list price. None from an API older than it.
    stopped_with_memory_micros_per_hour: int | None = None


@dataclass
class Plan:
    """A plan: what it costs a month, the credits it includes, which sizes it makes, how many it runs at once and how fast it starts them.

    The first, "free", costs nothing and includes nothing: what it runs is paid from credits, its
    one credit for a card saved and top-ups. See Prices.free.

    Counts are in small sandboxes: a tiny counts half, a medium 2, a large 4. A create, fork or
    start past running_units raises an ApiError 429 running_limit_reached. Starting faster than
    free_burst_units costs a burst fee: see Prices.burst_fee.
    """

    id: str
    dollars_per_month: int
    #: Credits the month's price pays for, in millionths of a US dollar. They do not carry over.
    included_micros: int
    #: The sizes a new sandbox, fork or template start may be on the plan, smallest first. A sandbox
    #: made before a change of plan still starts at its own size. None from an API older than it.
    sizes: list[Size] | None = None
    #: Small sandboxes the plan runs at once. None from an API older than it.
    running_units: int | None = None
    #: Small sandboxes the plan starts at once for nothing. None from an API older than it.
    free_burst_units: int | None = None


@dataclass
class BurstFee:
    """What a start costs when an account starts sandboxes faster than its plan's free burst.

    Fees come out of the plan's credits and then the top-ups.
    """

    #: How long an account's burst meter takes to fall by half. Each start adds its memory to it.
    half_life_minutes: int
    #: What each GiB of a start past the free burst costs, in millionths of a US dollar.
    fee_micros_per_gib: int
    #: What an account spends on burst fees in a month at most unless it sets another budget: set_burst_budget().
    default_budget_micros: int
    #: The most one start costs in burst fees, in hours of its size's list price. None from an API older than it.
    fee_cap_hours: int | None = None


@dataclass
class TopUp:
    """Credits bought once, on any plan, the free one included. From $200 it adds a bonus. It does not expire."""

    id: str
    #: What it costs, in whole US dollars.
    dollars: int
    #: The credits it adds: what it costs and the bonus.
    micros: int
    #: The part of micros that is the bonus. None from an API older than it.
    bonus_micros: int | None = None
    #: The bonus as a share of what it costs, to one place: 7.4 for $537 bought for $500. None from an API older than it.
    bonus_percent: float | None = None


@dataclass
class TopUpRange:
    """The smallest and the largest top-up, in whole US dollars."""

    min_dollars: int
    max_dollars: int


@dataclass
class TopUpBonus:
    """None below from_dollars, from_percent there, rising evenly on a log scale to to_percent at to_dollars, and to_percent from there on."""

    from_dollars: int
    from_percent: float
    to_dollars: int
    to_percent: float


@dataclass
class FreePlan:
    """The free plan: nothing a month, and what it runs is paid from credits.

    It needs a card saved, charged nothing: checkout(card=True). Once it is, the account is given
    credit_micros, once an account and once a card, which does not expire.
    """

    #: The free plan's id among the tiers: "free".
    plan_id: str
    #: The credit given once for a card saved, in millionths of a US dollar.
    credit_micros: int
    #: Small sandboxes it runs and starts at once, at most, until the account has paid for something, a top-up
    #: of any size; then the plan's running_units. A tiny counts half.
    unpaid_units: int


@dataclass
class StoragePrices:
    """What a stopped sandbox's disk and snapshots past the free ones cost, by the second."""

    #: A stopped sandbox's share of its running price. Each size's is in SizePrice.stopped_micros_per_hour.
    stopped_percent: float
    #: How many of an account's oldest snapshots cost nothing.
    free_snapshots: int
    #: What each snapshot past them costs an hour: $1.70 over a month.
    snapshot_micros_per_hour: int
    #: A share of its running price for a sandbox stopped with its memory kept. None from an API older than it.
    stopped_with_memory_percent: float | None = None


@dataclass
class Prices:
    """What is sold: the hourly price of each size (plans), the plans (tiers), the top-ups and their bonus, and the free plan's terms."""

    currency: str
    interval: str
    label: str
    note: str
    #: Each size and what an hour of it costs. Not the monthly plans, despite the name, which the API gives it: those are tiers.
    plans: list[SizePrice]
    #: The plans, the free one first: what each costs, its sizes, and how many it runs and starts at once.
    tiers: list[Plan]
    #: The top-ups offered as a choice. Any other whole number of dollars in top_up_range may be bought too.
    top_ups: list[TopUp]
    #: The free plan's credit and what it runs at once before the account has paid.
    free: FreePlan
    #: What stopped sandboxes and snapshots cost. None from an API older than it.
    storage: StoragePrices | None = None
    #: None from an API older than the bonus.
    top_up_range: TopUpRange | None = None
    #: None from an API older than it.
    top_up_bonus: TopUpBonus | None = None
    #: What starting faster than a plan's free burst costs. None from an API older than it.
    burst_fee: BurstFee | None = None


@dataclass
class BillingPeriod:
    start: str
    end: str


@dataclass
class BillingFree:
    """Where an account on the free plan stands."""

    #: Whether it has a card saved, which it must before it runs anything, and so has had its credit.
    card_saved: bool
    #: Whether it has paid for anything. Until it has, it runs and starts at most unpaid_units small sandboxes at once.
    paid: bool
    unpaid_units: int


@dataclass
class BillingLapse:
    """An account that may not run a sandbox and still holds stopped sandboxes or snapshots."""

    lapsed_at: str
    #: When its sandboxes and snapshots are deleted, unless a card is saved, credits bought, or what is owed paid, first.
    deletes_at: str
    #: no_plan: it is on the free plan with no card saved. out_of_credit: it is on the free plan and its credits
    #: are used. unpaid: it has a plan bought by the month and owes for what it used past the plan and its top-ups.
    reason: str = "no_plan"


@dataclass
class Billing:
    """Where the account stands. Amounts are millionths of a US dollar."""

    provider: str
    #: True while the payment provider is in its test mode: nothing is really charged.
    test_mode: bool
    #: Whether plans are enforced: a card saved, or a plan, is needed to run a sandbox.
    enforced: bool
    #: The subscription to a plan bought by the month, as the payment provider names it. none or canceled: the
    #: account is on the free plan.
    status: str
    #: The plan it is on: one bought by the month while it is live, else the free plan. None only where plans are not enforced.
    plan: Plan | None
    #: The month it is in. On the free plan, from where its use was last settled to the next month's start, in UTC.
    period: BillingPeriod | None
    #: Set while the account is on the free plan, else None.
    free: BillingFree | None
    included_micros: int
    #: What has been used this month at list price: running time, mail, stopped sandboxes and snapshots past the free ones.
    used_micros: int
    #: The part of used_micros that is mail.
    mail_micros: int
    #: What is left of the credits bought on top of the plan, and of the free plan's credit. It is used after the
    #: month's credits, and it does not expire.
    top_up_micros: int
    cancel_at_period_end: bool
    can_run: bool
    #: billing_required or out_of_credit when the account may not run a sandbox, else None.
    reason: str | None
    #: Set while the account may not run a sandbox and still holds sandboxes or snapshots, else None.
    lapse: BillingLapse | None = None
    #: The part of used_micros that is stopped sandboxes.
    stopped_micros: int = 0
    #: The part of used_micros that is snapshots past the free ones.
    snapshot_micros: int = 0
    #: What an earlier month ran past its plan and top-ups, taken from this month's credits first.
    carried_micros: int = 0
    #: True while the account must save a card, or buy a plan, before it uses agental: plans are enforced and it is
    #: on the free plan with no card saved.
    needs_plan: bool = False
    #: The part of used_micros that is burst fees: what starts faster than the plan's free burst cost. burst() says where the account stands.
    burst_micros: int = 0
    #: The auto-reload: the top-up the saved card is charged for when the credits run out, with its credits added before any sandbox is stopped. None while it is off.
    auto_reload: AutoReloadSetting | None = None
    #: Whether an auto-reload can be set: Stripe sells here, and the account has saved a card there.
    can_auto_reload: bool = False


@dataclass
class Checkout:
    """Where to send a person to pay. None with changed True when a plan was moved at once."""

    url: str | None
    changed: bool


@dataclass
class AutoReloadSetting:
    """The auto-reload: the top-up the saved card is charged for when the credits run out."""

    #: The top-up charged, in whole US dollars.
    dollars: int


@dataclass
class AutoReload:
    """The auto-reload as it is now: set, or None while it is off."""

    auto_reload: AutoReloadSetting | None
