# agental

Python SDK for [agental.io](https://agental.io): a Linux virtual machine of your own over HTTP. One call makes it, another runs a command in it, another stops it. Its disk is kept while it is stopped.

```bash
pip install agental-sdk
```

Then `from agental import Agental`. Python 3.10 or later. It depends on `httpx` and nothing else.

## Start

Make an API key in the console and put it in `AGENTAL_API_KEY`.

```python
from agental import Agental

agental = Agental()

sandbox = agental.sandboxes.create(size="small")  # tiny, small, medium or large

result = sandbox.exec("uname -a")
print(result.exit_code, result.stdout)

sandbox.files.write("app/main.py", "print('hello')\n")
print(sandbox.exec(["python3", "/opt/agental/app/main.py"]).stdout)

sandbox.delete()
```

`create` answers when the machine is running. A sandbox you already have is `agental.sandboxes.get(id)`, and `agental.sandboxes.list()` gives all of them.

## asyncio

`AsyncAgental` is the same client, awaited.

```python
import asyncio
from agental import AsyncAgental

async def main() -> None:
    async with AsyncAgental() as agental:
        sandbox = await agental.sandboxes.create()
        print((await sandbox.exec("uname -a")).stdout)
        await sandbox.delete()

asyncio.run(main())
```

## Commands

```python
sandbox.exec(["ls", "-la", "/opt/agental"])  # the arguments exactly, no shell
sandbox.exec("cd /opt/agental && npm test")  # a line of shell, run by sh -c
```

- A command that fails is an answer, not an error: read `exit_code`.
- Every command starts fresh, as root in `/root`. A `cd` or an `export` does not carry over to the next one.
- A command gets 60 seconds unless you pass `timeout`, in seconds, up to 90. One that runs out is killed and exits `124`. For longer, use `commands.run` below, up to 600.
- Each of `stdout` and `stderr` is cut at 1 MB, and `truncated` is then true.

To see output as it arrives:

```python
for event in sandbox.exec_stream("npm test"):
    if event.type == "exit":
        print("exited", event.exit_code)
    else:
        print(event.data, end="")
```

For a command that takes more than a minute, do not hold a request open for it:

```python
build = sandbox.commands.run("cd /opt/agental && make", timeout=600)
print(build.status, build.exit_code, build.stdout)

# or start it and look later
started = sandbox.commands.start("make")
now = sandbox.commands.get(started.id)
```

For work that must outlast ten minutes, start it in the background inside the machine: `sandbox.exec("nohup npm run dev > dev.log 2>&1 < /dev/null &")`.

## On a schedule

```python
tick = sandbox.schedules.create("python scrape.py", every="1h", name="scrape")
sandbox.schedules.create("./digest.sh", cron="0 7 * * 1-5", time_zone="Europe/Sofia")

sandbox.schedules.run(tick.id)  # one run now
runs = sandbox.schedules.runs(tick.id)  # the latest first: outcome, exit_code, command_id
sandbox.schedules.update(tick.id, enabled=False)  # paused
```

- When a run finds the sandbox stopped, it starts it, runs, and stops it again when the command ends, keeping its disk: you pay for the minutes it runs. `keep_memory=True` keeps its memory too. `if_stopped="skip"` runs only while it runs.
- A sandbox someone started again, sent a command or moved the stop of meanwhile is left running.
- Every 5 minutes at the most: for anything more often, leave the sandbox running and loop inside it. `at` runs it once, at a date and time, as for a check you mean to come back to.
- With `at`, `delete_after=True` deletes the sandbox once the run's command exits 0, whether the run started it or not; the run says so in `deleted_sandbox`. A run that fails, times out or does not run leaves it, so that it can be looked at. It takes a key that may delete the sandbox.
- After 10 runs one after another fail, a schedule pauses itself and says why in `paused_reason`; resuming it clears that.
- While a run still goes, the next is skipped. `overlap="replace"` stops it instead, `"parallel"` runs both.
- A run gets 10 minutes unless `timeout` says, in seconds, up to six hours. One that could not be made on time is `missed`, and never made up later.

## Files

Paths are relative to the work folder, `/opt/agental`. A file can be up to 10 MB.

```python
sandbox.files.write("notes.txt", "hi")
sandbox.files.read("notes.txt")  # "hi"
sandbox.files.write("logo.png", data)  # bytes
sandbox.files.read_bytes("logo.png")
```

## Stop, start, fork, snapshot

```python
sandbox.stop()   # the disk is kept, what was running is not, and it costs 5% of the running price
sandbox.stop(keep_memory=True)  # or keep its memory too, at 10%: start() puts back every process as it was
sandbox.start()  # waits until it is running, also when it has to be brought back from storage

copy = sandbox.fork()  # a new sandbox with the same disk
live = sandbox.fork(memory=True)  # or one that goes on from where it was, every process running on (Firecracker boxes only)

snapshot = sandbox.snapshot("before-upgrade")
sandbox.restore(snapshot.id)  # roll the disk back
fresh = agental.sandboxes.create(snapshot_id=snapshot.id)
```

## Regions

A sandbox is made in one region and stays there. `eu` has servers in the European Economic Area or the United Kingdom; `us` and `apac` are the others, where they have servers.

```python
from agental import ApiError

for region in agental.regions():  # the regions you can make sandboxes in now, the default first
    print(region.name, region.label, region.default, region.sizes_with_room)

sandbox = agental.sandboxes.create(region="eu")
sandbox = agental.sandboxes.create(region=["eu", "us"])  # the first of them that has room

try:
    agental.sandboxes.create(size="large", region="us")
except ApiError as error:
    if error.code != "capacity_exceeded":
        raise
    print(error.regions_with_room)  # where a sandbox of that size fits now, maybe []
```

Left out, a sandbox is made in the default region, or in the region of the snapshot it is made from. A full region raises an `ApiError` with the status 409 and the code `capacity_exceeded`, and the sandbox is not made anywhere else unless you named more than one region. `regions_with_room` says where it would fit now: create it again there.

## When it stops

A sandbox is stopped once it has run for its time limit since its last start, and its disk is kept. The limit is `ttl_seconds`, in seconds: give it when you make a sandbox, on a template's start, or later.

```python
from datetime import datetime, timezone

sandbox = agental.sandboxes.create(ttl_seconds=7200)  # two hours after each start
sandbox.update(ttl_seconds=14_400)  # four hours, from now on

# The run under way: when it stops, how long that is from now, and what decided it.
stop = sandbox.auto_stop()
print(stop.auto_stop_at, stop.seconds_left, stop.set_by)  # set_by "ttl": its last start and ttl_seconds

# Move that stop, later or sooner, for this run alone.
sandbox.set_auto_stop(in_seconds=3600)  # an hour from now
sandbox.set_auto_stop(at=datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc))

# Or take its time limit away: it runs until it is stopped.
sandbox.clear_auto_stop()
```

- A sandbox runs until it is stopped, unless it has a time limit: from its create, its template, `update(ttl_seconds=...)`, or a moved stop.

- Left out at a create, `ttl_seconds` is what the template's start says, and where that says none, the deployment's default. `None` is the deployment's default, whatever the template says. `agental.policy()` has `default_ttl_seconds` and `max_ttl_seconds`: a limit over the ceiling raises an `ApiError` with the code `ttl_too_long`.
- Give `set_auto_stop` either `at`, a `datetime` with its time zone or an ISO 8601 string with its offset, or `in_seconds`, and not both: anything else raises a `ValueError` before it is sent. A moved stop is 60 seconds from now at the least and `max_ttl_seconds` from now at the most, or it raises `auto_stop_too_soon` or `auto_stop_too_far` and the stop stays as it was. Each move counts from now, so a sandbox runs on for as long as something keeps moving its stop. A sandbox that is not running raises `invalid_state`.
- A move holds until the sandbox stops: its next start goes by `ttl_seconds` again. `update(ttl_seconds=...)` while it runs puts the stop back to its last start and the new limit.
- `set_by` is `ttl`, `account` for a move by one of your keys or the console, or `sandbox` for a move by the sandbox itself. Every move is the event `sandbox.auto_stop_set`.

## Show what you built

```python
route = sandbox.expose(8080)
print(route.url)  # anyone can open it while the sandbox is running
```

The app must listen on `0.0.0.0`. Pass `public=False` to keep the port behind your key.

## SSH

```python
sandbox = agental.sandboxes.create(ssh_keys=[open("/home/me/.ssh/id_ed25519.pub").read()])
ssh = sandbox.ssh.on()
print(f"ssh -p {ssh.port} {ssh.user}@{ssh.host}")  # user is root
print(ssh.host_key)  # pin it in known_hosts

sandbox.ssh.add_key("ssh-ed25519 AAAA... me@laptop")  # the sandbox must be running
for key in sandbox.ssh.keys():
    print(key.id, key.fingerprint, key.comment)
sandbox.ssh.remove_key(key.id)

sandbox.ssh.off()  # closes the port and cuts whoever is connected
```

SSH stays on through a stop and a start. `ssh_keys` at a create lets the keys in, but turns nothing on: `ssh.on()` does, or its template. A template can do both for every sandbox made from it: `template.ssh_on()` and `template.add_ssh_key(line)`. The agental command's `agental ssh` does all of it in one go. `agental.policy().ssh` says whether sandboxes can be reached over SSH where you are.

## Terminal

A login shell in the sandbox, as root in `/root`, over a WebSocket: no key and no port, as the console's Terminal tab has it.

```python
with sandbox.terminal(cols=120, rows=32) as terminal:
    terminal.write("cd /opt/agental && ls\n")
    terminal.resize(132, 40)  # as your own terminal's window changes
    terminal.write("exit 3\n")
    for chunk in terminal:  # bytes, as the shell wrote them
        print(chunk.decode(errors="replace"), end="")
print(terminal.exit.exit_code)  # 3
```

`AsyncSandbox.terminal()` is the same with `await` and `async for`. It needs a key with the `exec` action, and the sandbox running. The API closes it when the shell ends (`exit.close_code` 1000, with `exit_code`), when the key or the sandbox no longer lets it be open (4003), after 30 minutes with nothing typed (4008), and when its sandbox stops answering (4011); `exit.error_message` says why when the API did. Closing it hangs the shell up, as an SSH session's end does: start what must outlive it with `nohup` or `setsid`. The command's `agental shell <sandbox-id>` opens one in your own terminal.

## Desktop

A sandbox can have a screen of its own, which programs open windows on and which you watch and use in a browser.

```python
desktop = sandbox.desktop.on()  # 1280 by 800 unless you pass width and height
print(desktop.url)  # keep it as a password is kept
print(desktop.display)  # what DISPLAY is for a command that opens a window on it

png = sandbox.desktop.screenshot()  # bytes; format="jpeg", region=(x, y, w, h) and scale make it smaller
shot = sandbox.desktop.screenshot_base64(scale=0.5)  # for a model: base64, its size and the part it shows
sandbox.desktop.click(640, 400)
sandbox.desktop.type("hello")
sandbox.desktop.key("ctrl+s")

link = sandbox.desktop.create_link("view", expires_in=600)  # watch only, for ten minutes
print(link.url)

sandbox.desktop.off()  # closes what was open on it
```

`move`, `drag` and `scroll` move the pointer, and `clipboard()` and `set_clipboard(text)` read and fill the clipboard. A click, a key or typing is never sent twice by itself. `agental.policy().desktop` says whether a sandbox can have a desktop where you are.

## Browser

Chrome in the sandbox, driven from your own computer over the Chrome DevTools Protocol:

```python
from playwright.sync_api import sync_playwright

browser = sandbox.browser.on()  # no window, or on the desktop when one is on
with sync_playwright() as playwright:
    chrome = playwright.chromium.connect_over_cdp(browser.cdp_url)
    page = chrome.contexts[0].new_page()
    page.goto("https://example.com")

sandbox.browser.off()  # cdp_url leads nowhere from then on
```

Keep `cdp_url` as a password is kept: whoever has it has the browser, and what it is signed in to. `on(headless=True)` or `on(headless=False)` says whether it has a window; asked otherwise than it is, Chrome is started again, which closes what was open in it. `local_cdp_url` reaches it from inside the sandbox. What Chrome remembers stays on the sandbox's disk. `agental.policy().browser` says whether a sandbox's browser can be driven where you are.

## Templates and secrets

A template is what a sandbox is made as and held to: its start (a size, a snapshot or a new machine, and a time limit), the variables and files every sandbox on it gets, and what every sandbox on it may reach (see below). Every account has one named `default`, which a sandbox is on when you name none.

```python
template = agental.templates.create("coding-agent")
template.secrets.set("GITHUB_TOKEN", os.environ["GITHUB_TOKEN"])
template.secrets.write_file(".npmrc", "//registry.npmjs.org/:_authToken=...\n")
template.set_start("medium")

sandbox = agental.sandboxes.create(template=template)
sandbox.exec("gh auth status")  # GITHUB_TOKEN is in its environment

# A sandbox's own, on top of its template's, from its next command.
sandbox.secrets.set("GITHUB_TOKEN", "a token for this one only")
```

A sandbox takes a change to its template when it next starts. To give it to the ones that are running now, apply it:

```python
template.secrets.set("GITHUB_TOKEN", "a new token")
print(sandbox.template().up_to_date)  # False
template.apply()  # every running sandbox on it, or apply([sandbox.id])
```

`set_start("medium", ttl_seconds=7200)` has the template's sandboxes run for two hours after each start. `snapshot_id` left out or `None` is a new machine. `ttl_seconds` left out stays as it was, and `None` is the deployment's default. Sandboxes made before keep their size, their disk and their time limit.

Values are read one at a time with `secrets.get(name)` and `secrets.read_file(path)`; `variables()` and `files()` give names and paths only. `sandbox.set_template(other)` puts a sandbox on another template, which it has from its next command. A template that sandboxes are on cannot be deleted.

## Keep a secret out of the sandbox

An agent can use a key without being able to read it. Set a variable with the hosts it is for, and it is kept out of the sandbox: its commands see a stand-in in its place, and when an HTTPS request leaves the sandbox for one of those hosts, the proxy on its server puts the value in place of the stand-in. Sent anywhere else, the stand-in is worth nothing.

```python
from agental import HiddenVariable

kept = template.secrets.set("GITHUB_TOKEN", os.environ["GITHUB_TOKEN"], hosts=["api.github.com", "github.com"])
print(kept.stand_in)  # agental_hidden_..., what $GITHUB_TOKEN is in the sandbox
template.apply()  # the running sandboxes on it take it now

# The request carries the stand-in out of the sandbox, and api.github.com is sent the token.
sandbox.exec('curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user')

# Fewer hosts, without sending the value again. A new host needs set() with the value.
template.secrets.set_hosts("GITHUB_TOKEN", ["api.github.com"])

variable = template.secrets.variable("GITHUB_TOKEN")
if isinstance(variable, HiddenVariable):
    print(variable.hosts, variable.stand_in)
else:
    print(variable.value)
```

The value is never given back, to you or anyone: `variable(name)` gives a kept-out variable as a `HiddenVariable`, with its hosts and stand-in, and a plain one as a `PlainVariable`, with its value. `get(name)` raises an `AgentalError` for one kept out, and `hidden()` names the variables kept out. The stand-in stays the same when the value is replaced, so a file that holds it keeps working. Set again without hosts, a variable is plain, and its commands read its value: give its hosts each time you replace one. A host is a name such as `api.github.com`, or `*.example.com` for every name under `example.com`, with no scheme, port or path, and a variable has 1 to 10 of them. A name on the Public Suffix List, such as `github.io`, is refused, and so is a wildcard over one, such as `*.com`: each raises an `ApiError` with the code `invalid_host`. Hosts do not open the template's outbound rule. `agental.policy().hidden_variables` says whether variables can be kept out where you are: where they cannot, setting one with hosts raises an `ApiError` with the code `hidden_variables_unavailable`.

## Decide what it may reach

A sandbox reaches the internet unless its template says otherwise. The rule lives on the template, and every sandbox on it is held to it, enforced outside the sandbox, so nothing running in it can change it, root included. A sandbox can add addresses of its own on top:

```python
# Nothing gets out of a sandbox on this template...
template = agental.templates.create("locked-down")
template.set_network("blocked")

# ...but this one may reach its own database, from its first boot.
sandbox = agental.sandboxes.create(template=template, allow=["203.0.113.7"])

# Its own addresses, from now on. The list replaces the one it had, and [] takes them away.
sandbox.set_network(allow=["203.0.113.7", "198.51.100.0/24"])

network = sandbox.network()
print(network.outbound, network.allow)  # what it is held to: allowlist [...]
print(network.template, network.own)  # the template's rule as it has it, and its own addresses
```

The rule is `"open"`, `"blocked"`, or `"allowlist"` with the addresses in `allow`, and `set_network` on a template sends the whole rule each time. A sandbox's own addresses add to an allowlist, let it out to them alone on a blocked template, and change nothing while its template is open. Together they come to 100 addresses at most. `allow` takes IPv4 addresses and ranges, not names.

A sandbox made from a template is held to its rule from its first boot. A change to the rule reaches a sandbox already on it when it next starts, or now with `template.apply()`: its `failed` names any sandbox whose server did not take the new rule, which stays held to the one it had. A sandbox's own addresses hold from when `set_network` answers. Nothing is restarted, and your commands, files and ports still reach it. `agental.policy().network_policy` says whether sandboxes can be held to a rule where you are: where they cannot, asking for `"blocked"`, `"allowlist"` or addresses of a sandbox's own raises an `ApiError` with the code `network_policy_unavailable`.

## See what it uses

A sandbox's CPU, memory, disk and network are read once a minute, from outside it, and kept 15 days. Nothing is installed in it:

```python
from datetime import datetime, timedelta, timezone

# The last hour of one sandbox, oldest first.
metrics = sandbox.metrics()
if metrics.points:
    last = metrics.points[-1]
    print(last.cpu_utilization, last.memory_used_bytes)  # 0.42 is 42% of its CPUs

# Another stretch, with a point every five minutes.
sandbox.metrics(since=datetime.now(timezone.utc) - timedelta(days=1), step_seconds=300)

# Every sandbox you have, at one moment. Without at, now.
for each in agental.metrics(at="2026-10-14T09:35:00Z").sandboxes:
    print(each.name, each.reading.memory_used_bytes if each.reading else None)

# How many of them were running, step by step, over the last 24 hours.
running = agental.running_over_time()
print(running.points[-1].running)
```

A sandbox has no point for a moment it was stopped at, and its `reading` is then `None`. A figure its server did not have is `None` too. `complete` is false when a server did not answer, and readings may then be missing. A `datetime` must say its time zone. `agental.policy().metrics` says whether sandboxes are read where you are: where they are not, both `metrics` calls raise an `ApiError` with the code `metrics_unavailable`. `running_over_time` answers either way.

## What it costs

```python
# Where the account stands, in millionths of a US dollar.
billing = agental.billing()
print(billing.used_micros, billing.top_up_micros)

# What is sold: the plans in tiers, the free one first, the hourly price of each size in plans, top-ups and their bonus, and the free plan's credit.
prices = agental.prices()
for plan in prices.tiers:
    print(plan.id, plan.dollars_per_month, plan.sizes)

# On the free plan, save a card first: nothing is charged, and the account is given $10 of credit once.
card_page = agental.checkout(card=True).url

# Start paying $500 for $537 of credits. A person pays at url: nothing is charged before.
url = agental.checkout(top_up=500).url

# When the credits run out, charge the saved card $50 for a top-up, before anything is stopped.
agental.set_auto_reload(50)
```

An account starts on the free plan: nothing a month, and what it runs is paid from credits at the same list prices. It needs a card saved first, charged nothing, and is then given $10 of credit, once an account and once a card; `billing().free` says whether a card is saved (`card_saved`) and whether the account has paid for anything (`paid`). Until a card is saved, a create raises an `ApiError` 402 `billing_required`; once the credits are used, 402 `out_of_credit`, and a top-up ends it. To go back to the free plan from a paid one, cancel it in the billing portal.

`used_micros` is the month's usage at list price, spent from the plan's credits first and then from the top-ups, which do not expire. A top-up is any whole number of dollars from $15 to $100,000. From $200 it adds a bonus: 2% at $200, growing evenly on a log scale to 25% at $10,000 and over, so $1,000 adds $1,115. `prices.top_ups` lists what each offered amount adds, and `prices.top_up_bonus` the curve. `checkout(plan="plan100")` starts a plan the same way, or moves an account that has one at once, with `url` None and `changed` True. A checkout whose answer is lost is not sent again.

`set_auto_reload(dollars)` has the card saved at the last purchase charged for a top-up of that much, with tax, when the month's credits and the top-ups run out, and the credits added at once, before any sandbox is stopped: at most three times a day, and not again for a day after a card that would not pay. `set_auto_reload(None)` turns it off, and `billing().auto_reload` says how it is set. It needs a card saved first: `billing().can_auto_reload` says whether it can be set, and the call answers `409 no_card` until then.

The free plan makes tiny and small sandboxes, the $100 plan medium ones too, and the $500 and $2000 plans every size: each plan's `sizes` in `prices().tiers` says which. `prices().plans`, despite its name, is the sizes and what an hour of each costs. A create, fork or template start of a size the plan does not make raises an `ApiError` with the status 402 and the code `size_not_on_plan`, whose message names the plan that makes it. A sandbox made before a change of plan still starts at its own size.

## How many you run, and how fast you start them

Each plan runs so many small sandboxes at once, its running count, and starts so many at once for nothing, its free burst: a tiny counts half, a medium 2 and a large 4. The free plan runs 20 and bursts 20, the $100 plan 100 and 100, the $500 plan 600 and 200, the $2000 plan 2000 and 400. An account on the free plan runs and starts at most 2 small sandboxes at once until it has paid for something, a top-up of any size (`headroom().now.unpaid`). `prices().tiers` gives each plan's `running_units` and `free_burst_units`, `prices().free.unpaid_units` the free plan's count before it has paid, and `prices().burst_fee` how fees are worked out. Each start adds its memory to a burst meter that halves every 15 minutes. Starting faster than the free burst is never refused for speed: it costs a burst fee, which grows with how far over the meter goes.

`headroom()` says how close the account runs to these limits: `now`, where it stands, and over a range (today, UTC, unless `from_` and `to` say otherwise, 90 days at most) what it ran against its running count, each burst (a stretch when the meter was over the free burst, so every start in it paid a fee), what did not start and why, and how fast what started ran.

```python
headroom = agental.headroom()
now = headroom.now
print(now.running_mb / 4096, now.running_units)  # small sandboxes running, of 100
print(now.free_now_mb / 4096)  # small sandboxes you may start now for nothing
print(now.spent_micros, now.budget_micros)  # burst fees this month, and the most you spend on them
print(now.next_start)  # what one sandbox of each size would cost to start now
print(headroom.at_running_count_ms / 60_000)  # minutes today at the running count
if headroom.bursts:
    print(headroom.bursts[0].started_at, headroom.bursts[0].ended_at)  # the newest burst; ended_at is None while it goes on
print(headroom.refused.total, headroom.refused.by_reason)  # what did not start today: running_count, budget, fee_cap, room or other

# The last week, read every hour.
week = agental.headroom(from_=datetime.now(timezone.utc) - timedelta(days=7), step="hour")
print([point.peak_running_mb / 4096 for point in week.series])

# Spend at most $10 a month on burst fees; 0 refuses every start that would cost one, None puts back the default ($50).
agental.set_headroom_budget(10_000_000)
```

A create, fork or start takes `max_burst_fee_micros`, the most it may pay, and the sandbox's `burst_fee_micros` says what it paid:

```python
sandbox = agental.sandboxes.create(max_burst_fee_micros=50_000)  # at most 5 cents
print(sandbox.burst_fee_micros)  # 0 within the free burst
sandbox.start(max_burst_fee_micros=0, max_burst_wait=300)  # free, waiting up to 5 minutes for it to be
```

Where `now.enforced` is True, a start is refused with an `ApiError` of status 429 and one of these codes, all in `BURST_REFUSALS` (`from agental import BURST_REFUSALS`). `fee_micros` says what it would have cost and `retry_after_seconds` when it would cost nothing, or None when waiting would not help; `refused_last_hour` how many creates and starts were refused with the same code in the last hour, this one with them, and `headroom_url` the console's page that shows them all:

- `running_limit_reached`: the account already runs its plan's running count. Stop something first.
- `burst_fee_too_high`: the start would cost more than `max_burst_fee_micros`.
- `burst_budget_reached`: the fee would take this month's burst fees past the budget.

With `max_burst_wait`, in seconds, a start refused with one of the last two waits `retry_after_seconds`, if that is no longer than allowed, and is tried once more. It is off by default, and never waits for `running_limit_reached`. Burst fees come out of the plan's credits, then the top-ups, and `billing().burst_micros` is this month's.

## Send it to your own collector

Your sandboxes' metrics and lifecycle events can be sent to an OTLP/HTTP collector of your own:

```python
# Set it, or replace the one you had. Nothing is sent yet.
agental.set_telemetry("https://otlp.example.com", headers={"x-api-key": "..."})

# Send it one empty export, and see what it answered.
print(agental.test_telemetry())  # TelemetryTest(ok=True, status=200, message=None)

# Where it is, the names of its headers, and how the last export went. None when none is set.
telemetry = agental.get_telemetry()
if telemetry:
    print(telemetry.headers, telemetry.last_export_at, telemetry.last_error)

# Stop sending to it.
agental.delete_telemetry()
```

`endpoint` is the collector's base address, https on port 443 or 4318: metrics go to `/v1/metrics` under it, every minute, and events to `/v1/logs`, as they happen. The values of `headers` are kept and never shown again. An event has the same id in the collector as in your webhooks. `delete_telemetry` and `test_telemetry` raise an `ApiError` with the status 404 when none is set. `agental.policy().telemetry` says whether anything is sent to a collector where you are: where it is not, these calls raise an `ApiError` with the code `telemetry_unavailable`. Metrics are sent only where `metrics` is true as well.

## What agental keeps about you

```python
import json

exported = agental.export_account()
print(exported["account"]["email"], len(exported["sessions"]))
with open("agental-account.json", "w") as file:
    json.dump(exported, file, indent=2)
```

It is everything agental keeps about your account and the person behind it: the account, its sessions with the address and browser each began from, its keys by name, billing, payments, usage, and what it has made. Secrets are named, never given. It works with a key. Closing an account does not: only a person signed in to the console can close one, from Settings.

## How long its history is kept

The output of your sandboxes' commands, their agent conversations and their events are kept 30 days unless you choose otherwise.

```python
retention = agental.get_data_retention()
print(retention.keep_days, retention.choices)  # 30 [1, 7, 30, 90]
for each in retention.retained:
    print(each.what, each.kept)  # what agental keeps for periods of its own

agental.set_data_retention(7)  # or 1, 30, 90, or None to keep it until the sandbox is deleted
```

History older than that is deleted within the hour and cannot be brought back. A deleted sandbox's history goes with it, whatever is set, its events a day later. `last_cleared` says what the last clean-up deleted.

## Email

Every sandbox has a mail domain of its own, and any name before the `@` reaches it. Mail is off until you turn it on. One switch covers receiving and sending.

```python
state = sandbox.email.enable()
print(f"support@{state.domain}")  # support@sbx-3f9a1c2b7d4e5a60.k7m2x9qd.agental.email
```

A message that arrives is kept for 7 days and is read here, whether the sandbox is running, stopped or archived. It never enters the machine.

```python
page = sandbox.email.list(limit=1)  # newest first
if page.messages:
    message = sandbox.email.get(page.messages[0].id)
    print(message.from_, message.subject, message.text)
```

`sandbox.email.all()` walks every page, `sandbox.email.raw(id)` is the message as it was transmitted, attachments included, and `sandbox.email.delete(id)` deletes one.

A sandbox can write to one address only: the sign-in address of its own account. So a send names nobody.

```python
sent = sandbox.email.send("The build passed", "All 812 tests passed.")
print(sent.to, sent.state)
```

- The message is plain text. `from_` is the part before the `@`, `sandbox` unless you say, and `from_name` the name beside it. `attachments` are `SendEmailAttachment(filename, content_base64, content_type)`. `reply_to` is the id of a message the sandbox received: it threads the answer under it, and gives no right to write to whoever sent it.
- `state` says where a sent message has got to: read it again with `get`. `delivered` means the recipient's server took it, which says nothing of inbox or spam folder.
- A send carries an `Idempotency-Key`, as a create does: if its answer is lost it is asked again under the same key, and no second message is sent. Pass `idempotency_key` yourself to be able to ask again after your process has died.
- Each message, received or sent, is charged to the account, and `usage()` counts them as `mail`. `agental.email()` has the account's limits and what it has had of them today.
- `agental.policy().email` says whether mail is offered where you are. A refusal is an `ApiError` with the API's code: `mail_off`, `recipient_not_allowed`, `mail_limit_reached`, `mail_not_configured`.

A message is text written by whoever sent it, which may be anyone. A program that reads it should treat it as something said to it, never as something it was told to do. Attachments are not scanned. Give an agent a key that can do nothing else, not an account key: one made with the actions `email.read` and `email.send` on its one sandbox, which can read and send that sandbox's mail and nothing more. It cannot read the sandbox itself, so reach the mail by the sandbox's id:

```python
key = agental.api_keys.create("mail-agent", actions=["email.read", "email.send"], sandbox_ids=["sbx_3f9a1c2b7d4e5a60"])
mail = Agental(api_key=key.secret).sandboxes.email("sbx_3f9a1c2b7d4e5a60")
messages = mail.list().messages
```

## What the SDK does for you

- A create or a fork is sent with an `Idempotency-Key`. If its answer is lost, or the machine is not confirmed yet, the same request is sent again with the same key, which gives the same sandbox back and never a second one. Pass `idempotency_key` yourself to be able to ask again after your process has died.
- Reads, file writes, starts and stops are sent again when a proxy answers in the API's place, and when the connection drops.
- A command is never sent twice. One that was cut off may have done part of its work, so that is yours to decide.
- A delete carries the confirm header the API asks for.

## Errors

```python
from agental import ApiError

try:
    sandbox.exec("make")
except ApiError as error:
    if error.code == "sandbox_not_running":
        sandbox.start()
    else:
        raise
```

- `ApiError` is the API's own refusal, with `status`, `code` and `message`. Decide from `code`. A start refused for its burst also has `fee_micros` and `retry_after_seconds`: see [How many you run](#how-many-you-run-and-how-fast-you-start-them).
- `ProxyError` is a 502, a 504 or any error with no code in it: something in front of the API answered, and the request may still be running.
- `AgentalConnectionError` is the API not being reached. `AgentalTimeoutError` is a wait that ran out.

All of them are `AgentalError`.

## Webhooks

```python
hook = agental.create_webhook("https://example.com/hooks/agental", events=["sandbox.stopped", "sandbox.error"])
hook.signing_secret  # given this once
agental.list_webhooks()  # each with its tries of the last 30 days
agental.get_webhook(hook.id)
agental.update_webhook(hook.id, events=["*"])  # every event, and any added later
agental.rotate_webhook_secret(hook.id)  # a new signing_secret; the old one stops working
agental.delete_webhook(hook.id)
```

`events` takes the kinds of event, such as `sandbox.started`, `sandbox.stopped` and `sandbox.error`. `sandbox.auto_stop_set` is a running sandbox's stop moved: its `data` has `autoStopAt`, `previousAutoStopAt`, and `by`, `account` or `sandbox`.

Check each post your server receives:

```python
from agental import verify_webhook

genuine = verify_webhook(raw_body, request.headers.get("x-agental-signature"), signing_secret)
```

Pass the body exactly as it arrived, before any JSON parsing.

## API keys

A key does what it was made to do and nothing else: a preset, or the actions it may do named one by one. It can be limited to some sandboxes and templates, to a date, and to the addresses it is used from.

```python
from datetime import datetime, timedelta, timezone

key = agental.api_keys.create(
    "ci-github",
    # Use sandboxes, make, fork and resume them, and delete the ones it made.
    preset="ci",
    # These templates and the sandboxes on them.
    template_ids=["tpl_coding_agent"],
    # Refused from anywhere else.
    allow_from=["203.0.113.0/24", "2001:db8::/48"],
    # Optional: when it stops working.
    expires_at=datetime.now(timezone.utc) + timedelta(days=30),
)
key.secret  # given this once

agental.api_keys.list()  # never with their secrets
agental.api_keys.get(key.id)  # with when and from where it was last used
agental.api_keys.set_allow_from(key.id, [])  # anywhere: the one thing that changes after it is made
agental.api_keys.usage(key.id)  # what the sandboxes it made ran, at list price
agental.api_keys.delete(key.id)
```

- The presets are `read-only`, `full-sandbox`, `ci` and `admin`. Give `preset` or `actions`, one of them: anything else raises a `ValueError` before it is sent. `actions` takes names such as `sandbox.read`, `exec` and `email.send` (the type `ApiKeyAction` lists them all), or `"*"` for everything.
- `sandbox.auto-stop` reads and moves when a running sandbox is to be stopped. It is in the `full-sandbox` and `ci` presets, but not in a key made with one before it was added: a preset is written out into a key's actions when the key is made.
- A key always reaches the sandboxes it makes. One limited to several templates, none of them your default, has to name a template when it makes a sandbox, or it is refused with `template_required`.
- A key used to make another makes only a weaker one: no action, sandbox or template it lacks, no later expiry, and addresses inside its own. A stronger one is refused with `key_exceeds_caller`.
- A key is refused with `key_action_forbidden` for what it was not made to do, `key_address_forbidden` from an address not on its list, and `key_expired` once it has expired.

`allow_from` is for keys used outside agental, such as from CI or your own servers. A sandbox reaches the API from its server's shared IPv4 address, which changes when the sandbox moves, so limit a key used inside a sandbox with `sandbox_ids` instead. On a network with IPv6 the API sees your IPv6 address, so a list of IPv4 addresses alone refuses you there.

## Options

```python
Agental(
    api_key="...",                  # default: AGENTAL_API_KEY, then the agental command's sign-in
    base_url="https://agental.io",  # default: AGENTAL_API_URL, then this
    max_retries=3,                  # how many times a request that is safe to send again is sent again
    retry_delay=1.0,                # seconds between those tries, at the least
    poll_interval=2.0,              # seconds between looks at what is waited for: a start, a command
    http_client=my_httpx_client,    # an httpx.Client, or httpx.AsyncClient for AsyncAgental; you close it
)
```

With no key given and none in `AGENTAL_API_KEY`, the client uses the key the agental command saved when you signed in on this computer with `npx agental@latest`, in `~/.config/agental/credentials.json` (or under `XDG_CONFIG_HOME`), for the API it signed in to. Every request says it comes from `agental-python/` and the SDK's version, which is `agental.__version__`.

The full API is OpenAPI 3.1 at `https://agental.io/openapi.json`.
