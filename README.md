# yo

A tiny scheduler that pings an agentic coding CLI ("yo") a few times a day to
**optimize your daily usage-window**: a well-timed ping anchors each agent's
~5h usage window to your workday, so your quota lines up with when you actually
work. It logs the reply, and is backend-agnostic — it can drive **Codex** or
**Claude**.

## Quickstart

On an always-on host (server/VM — see [Requirements](#requirements)), with the
agent CLI you want already on `PATH`:

```sh
git clone https://github.com/samronsin/yo.git
cd yo

./yo claude                 # smoke-test: one ping now; creates ~/.yo/ and logs the reply there

# install a daily schedule anchored to your working hours
./install.py --tz Europe/Paris --hours 9-18 --command claude
```

That's it — `cron` now pings on schedule, and what you asked for is kept in
`~/.yo/settings.ini` (see [Settings](#settings)). Re-run `install.py` any time
to change the hours or commands, or `./install.py --remove claude` to stop.
See [Usage](#usage) for more.

## Components

- **`yo`** — the runner. Invokes the selected agent CLI once with the prompt
  `yo`, in a read-only/non-interactive mode, and writes the output to a
  per-command log. The command is a required first argument: a backend name
  (`codex` or `claude`) or any [custom command](#custom-commands) paired with
  `--backend`. An optional `--model` flag overrides the per-backend default
  (GPT-5.6-terra for Codex, Haiku for Claude). With `--probe`, a Codex ping
  is also verified against the account's quota reset and recorded (see
  [Recorded Codex pings](#recorded-codex-pings)); `install.py` schedules
  Codex pings that way.
- **`install.py`** — generates and installs the crontab. Given a timezone and
  working hours, it builds a schedule that re-anchors each agent's 5h usage
  window across your day (see [Window model](#window-model)) and pipes the
  result into your crontab. The schedule is computed in your `--tz` and then
  **converted to the system time cron actually schedules against** (see
  [Timezones](#timezones)).
- **`utils/codex_runner.py`** — the Codex backend called by `yo`: owns the
  Codex invocation and, with `--probe`, verifies and records the ping.
- **`utils/claude_runner.py`** — the Claude backend: sends a plain ping and
  writes its output and exit status to the run log.
- **`utils/status.py`** — `yo <command> --status`: the account's quota
  windows and the last run, read token-free through each CLI (see
  [Quota status](#quota-status)).
- **`utils/settings.py`** — `~/.yo/`: the settings file and the logs directory
  (see [Settings](#settings)).
- **`tests/`** — the test suite (`python3 -m unittest` from the repo root).

## Usage

Run once, ad hoc:

```sh
./yo codex                    # Codex, default model (GPT-5.6-terra)
./yo claude                   # Claude, default model (Haiku)
./yo claude --model opus      # Claude with an explicit model
./yo codex-pro                # a registered custom command: backend from settings
```

### Quota status

`--status` reads quota windows through the CLI and shows the last run.
It accepts the same command (and `--backend`, unless the command is registered
in [settings](#settings)) as a ping, but no ping flags. Times are shown in the
command's `tz` from settings, else the machine's:

```sh
./yo codex --status                         # Codex login, via `codex app-server`
./yo claude-pro --status                    # a registered Claude wrapper, via headless `/usage`
./yo claude-perso --backend claude --status # an unregistered one needs --backend
./yo --status                               # every registered command, one report each
```

```
codex (codex), read 2026-09-10 13:44 CEST
  5h window   open, 12% used, anchored 11:30, resets 2026-09-10 16:30 CEST (2h46m left)
  weekly      31% used, resets 2026-09-14 09:00 CEST
  last run    2026-09-10 06:00 CEST, anchored (default), rc=0, /home/me/.yo/logs/yo-codex-20260910T040004Z.log
```

- **Codex** uses the probe's token-free quota read. An ambiguous 5h window
  needs a second read after 15 seconds to distinguish `open` from `idle`.
- **Claude** uses headless `/usage` (zero model turns on Claude Code 2.1.265;
  a warning appears if a version spends turns). Positive usage means `open`;
  zero is ambiguous. Unrecognized reset dates are shown as printed by the CLI.
- **Anchored** is the reset minus five hours. Claude's minute-precision resets
  are too coarse for `--probe`, which remains Codex-only.
- **Last run** shows the newest log for the exact command: start time, exit
  status, and probe verdict when available.

Exit status is `0` on a successful quota read, or `1` with an error in the report.

### Recorded Codex pings

Codex exposes its 5h quota reset through the CLI's token-free `app-server`,
which makes anchoring observable: a real anchor locks `resetsAt` at ping+5h,
while an unanchored account reports a hypothetical reset that drifts with the
clock. `yo <codex command>` alone is the plain ping: it runs `codex exec`,
writes the run log, and exits with the ping's status. `--probe` wraps that
ping in the observer (`utils/codex_runner.py` with
`utils/codex_anchor_probe.py`):

1. **Pre-read.** One quota read, plus a second 15s later only if the first is
   ambiguous (0% used with a reset near now+5h). A window that is already
   open means nothing is sent and the run is recorded as `window_open`.
2. **Ping.** The production invocation (`codex exec ...`, built in
   `codex_runner.py`).
3. **Post-reads.** After a 10s settle, two reads 60s apart decide the verdict: `anchored`,
   `not_anchored`, or `inconclusive` (a window is live but was not opened by
   this ping). A ping that failed is `execution_error`; one whose quota could
   not be read afterwards is `observation_error`. A failed pre-read never
   blocks the ping.
4. **Record.** The per-run `yo-<command>-<timestamp>.log` is written as
   before and now ends with a verdict line and one `record: {...}` JSON line
   holding the effective model, effort, thread source and prompt, the CLI
   version and executable, the quota reads, the session id and token usage
   (including cached input tokens, read from the CLI's own session rollout),
   the outcome, and whether the config was yo's `default` or a `manual`
   override. Each run log is one ping's complete evidence; the series is a
   grep over them.

Exit status is `0` whenever a window is open or the run could not be judged,
`3` when the window is verifiably still closed, and the ping's own status
when the ping itself failed. Each run takes about 1.5 minutes longer than the
ping alone; a per-command lock skips a run that overlaps one still being
verified.

```sh
./yo codex                               # plain ping, about five seconds
./yo codex --probe                       # the same ping, verified and recorded
grep -h '^record: ' ~/.yo/logs/yo-codex-*.log | sed 's/^record: //' | python3 -m json.tool
```

`install.py` emits `--probe` on Codex lines by default, so the block you
confirm shows it; pass `--no-probe` to schedule plain pings instead. Claude's
only token-free quota read (`/usage`, see [Quota status](#quota-status)) has
minute precision, too coarse for the locked-vs-drifting test, so `--probe` is
refused there and never scheduled.

Install a schedule (review the snippet, confirm, and it's added to your crontab).
One command per run — for several, run it once each:

```sh
./install.py --tz Europe/Paris --hours 9-18 --command codex
./install.py --tz Europe/Paris --hours 9-18 --command claude
```

Each command gets its own crontab block and [settings](#settings) section.
Re-running replaces only that command's block and preserves omitted settings:
`./install.py --command codex --hours 8-17` changes just its hours.

To see what's installed, with each command's run times, whether its block
matches the settings file, and its log location:

```sh
./install.py --status
```

For what the account's quota looks like right now, see
[Quota status](#quota-status) (`./yo <command> --status`).

To regenerate a command's block from the file, after a clock
change or a hand edit of the settings:

```sh
./install.py --command codex
```

To stop a command, remove its block and settings section, keeping other commands
and your own crontab lines:

```sh
./install.py --remove codex
```

A block installed under a name you've since renamed keeps firing until it's
removed; `--status` lists it, so `--remove` it by its old name.

See `./install.py --help` for `--window-hours`, `--num-windows`, `--probe`/
`--no-probe`, the persisted `--model`/`--effort`/
`--thread-source` overrides, and `--yes`.

### Settings

`install.py` persists flags in `~/.yo/settings.ini`, one section per command;
omitted flags fall back to the section, then to the built-in defaults. Each
section stands on its own, nothing is shared between commands:

```ini
[codex-pro]
backend = codex
tz = Europe/Paris
hours = 9-18
window_hours = 5
num_windows = 3

[claude-perso]
backend = claude
tz = Europe/Paris
hours = 19-23
window_hours = 5
num_windows = 3
```

Keys: `backend`; `tz`, `hours`, `window_hours`, `num_windows`; `probe`
(`--probe`/`--no-probe`; the codex default is on); optional `model`, `effort`,
`thread_source` overrides, which `yo` applies to that command's pings (a record
then says `source: settings`).

`yo` reads these settings; its flags override them for one run. Registered
commands need no `--backend`, and `--status` uses their `tz`. Without
`--hours` (and none in the file), `install.py` registers the command and leaves
the crontab alone, which is how a laptop that must never get a crontab
registers its logins:

```sh
./install.py --command claude-perso --backend claude --tz Europe/Paris
./yo claude-perso --status                # no --backend needed
```

Add `--hours` later to schedule it. After editing schedule settings by hand,
run `./install.py --command NAME` to apply them. Rewrites discard comments.
Credentials and profiles belong in the wrapper script, not this file.

### Custom commands

Say you drive several logins behind their own wrappers — each a small executable
script on `PATH` (not a shell alias: cron and `install.py`'s `PATH` lookup only
see real files) that points its CLI at one account's auth/config. Bare
`codex`/`claude` only reach your default logins, so run each wrapper by name and
name the backend instead. The name is the executable `yo` invokes and a label
`yo` otherwise doesn't parse;
`--backend` (`codex` or `claude`) picks the runner:

```sh
./yo codex-pro     --backend codex     # professional Codex login
./yo codex-perso   --backend codex     # personal Codex login — same backend, other auth
./yo claude-perso  --backend claude    # personal Claude login
```

The first two share the Codex backend and differ only by name — which is exactly
why the backend is a separate flag and not read off the command: the name says
*which login*, the backend says *which runner*, and the two are independent.

`install.py` mirrors that shape — one command plus `--backend`, run once per
command (each lands in its own block, so they coexist). Bare `codex`/`claude`
still infer themselves:

```sh
./install.py ... --command codex-pro     --backend codex
./install.py ... --command codex-perso   --backend codex
./install.py ... --command claude-perso  --backend claude
```

A custom name with no `--backend` is rejected unless `install.py` has
registered it (see [Settings](#settings)), since the runner is unknown.

`install.py` keys the log, the crontab marker and the settings section on the
command name (`yo-codex-pro`), and bakes the resolved `--backend` into the
generated cron line as well, so scheduled pings keep running even if the
settings file is lost. Every command must resolve on `PATH` — `install.py` prepends the directory
it's found in to the block's cron `PATH`, same as for a backend name.

## Window model

Both Codex and Claude gate usage with a **~5-hour window anchored to your first
message**: your first prompt opens the window and it resets a fixed time later
(e.g. 9:00 → 14:00), rather than counting a sliding trailing total. That
anchoring is the whole reason a "yo" ping helps — a well-timed first message
decides *when* the window opens. Because both behave the same way, both agents
get the same schedule.

To keep a freshly-anchored window live across the workday, `yo` *re-anchors* it:
`--num-windows` pings spaced `--window-hours` apart, centered on your working
hours (the first ping fires a bit before you start). For `--hours 9-18` that's
**06:00, 11:02, 16:04** — a new 5h block anchored roughly every 5 hours.

> Both providers also enforce a separate **weekly limit** alongside the 5h
> window. `yo`'s pings are tiny (a "yo" and a one-line reply), so even several a
> day stay far below it — `yo` therefore ignores the weekly limit and schedules
> only around the 5h window.
>
> This describes each provider's *current* published behavior, which changes
> often. If Codex and Claude diverge, or either switches to a true *sliding*
> window (a trailing count with no fixed reset), the schedule would need to
> split per agent again.

## Why not just use Claude routines / Codex automation?

Both vendors now ship native scheduling (Claude routines, Codex automation), and
their schedulers draw from the **same usage quota** `yo` targets — so they can
anchor your window just as well. `yo` doesn't do anything they can't. What it
offers instead is:

- **One interface for every agent** — the same flags, schedule, and logs for
  Codex and Claude, rather than two vendor-specific systems.
- **Runs on infrastructure you already control** — your CLI install, your auth,
  plain `cron`, logs on your own disk; no dependency on a hosted scheduler.
- **Small and transparent** — a short script plus a crontab you can read.

If you live in a single vendor's ecosystem, their native routine is probably the
simpler choice. `yo` is for driving several agents uniformly from an always-on
box you already run.

## Timezones

You give `install.py` a `--tz` (e.g. `Europe/Paris`) and it reasons about the
schedule in that timezone. Since `cron` schedules jobs in the **system
timezone**, with no portable way to override that per-crontab, `install.py`
**converts** each run time from your `--tz` into the system timezone before
writing the cron lines, which is correct on every `cron`. The install output
shows both, e.g.:

```
Scheduled pings (claude): 06:00, 11:02, 16:04 Europe/Paris -> 04:00, 09:02, 14:04 UTC (cron schedules in system time)
```

Because a static crontab can't follow daylight-saving transitions, the offset is
fixed at install time. After the clocks change (or if you move the box to
another timezone), re-run **`./install.py --command NAME`** for each command
to re-anchor its schedule; `--status` shows which blocks have drifted.

## Logs

Written under `~/.yo/logs/` as `yo-<command>-<timestamp>.log`, with the final
message in `yo-<command>.last.txt`. The first run creates the directory; at a
terminal every ping prints `log: <path>` on stderr, while under cron it stays
silent. Logs from before this layout stay where they
were, under the checkout's `logs/`. Probed Codex runs end with a `record:` line holding
the ping's verified outcome (see [Recorded Codex pings](#recorded-codex-pings)).

## Anchor test utilities

The server-side rules for which pings anchor a 5h window shift silently (see
issues #9 and PR #14 for the history); when pings stop anchoring, re-bisect
rather than trusting old conclusions. Every recorded ping already carries its
verdict when probed (the `record:` line in its run log), so start there. Two utilities remain for
one-off work:

- `utils/codex_anchor_probe.py [--command WRAPPER] [--model M] [--effort E]
  [--thread-source S] [--wait SECS] [--force]` — the observer
  the runner uses, run as a one-shot outside the recorded history: one ping
  through the same invocation, then two rate-limit reads `--wait` seconds apart
  (60s default). Its verdict and evidence go to `~/.yo/logs/gap-anchor-test-*`.
  Change one variable per run; an ANCHORED verdict closes the gap for ~5h.
  Skips the ping while a window is open unless `--force`.
- `utils/codex_anchor_watch.py [--cron-time HH:MM] [--now]` — verifies a cron ping
  anchored: reads the firing times from the installed crontab's yo-codex block
  (`--cron-time` overrides), waits for the next firing (or judges the last
  one), summarizes the cron's own log to catch pings that died client-side,
  then applies the same locked-vs-drifting test and attributes the anchor.

Don't judge anchoring from the Codex web UI — it hides windows at 0% usage.

## Requirements

- A host that's running whenever the pings should fire — a server or always-on
  VM. `cron` only runs while the machine is up, so a laptop that sleeps overnight
  will miss its scheduled pings (and the anchoring they provide).
- The agent CLI you select (`codex` and/or `claude`) on `PATH`.
- Python 3.10+ (`python3`) on `PATH` for `yo`, the installer, and the utilities.
- `cron` (the installer pipes into `crontab`).
