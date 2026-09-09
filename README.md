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

./yo claude                 # smoke-test: one ping now, reply in logs/

# install a daily schedule anchored to your working hours
./install.py --tz Europe/Paris --hours 9-18 --command claude
```

That's it — `cron` now pings on schedule. Re-run `install.py` any time to change
the hours or commands, or `./install.py --remove claude` to stop. See
[Usage](#usage) for more.

## Components

- **`yo`** — the runner. Invokes the selected agent CLI once with the prompt
  `yo`, in a read-only/non-interactive mode, and writes the output to a
  per-command log. The command is a required first argument: a backend name
  (`codex` or `claude`) or any [custom command](#custom-commands) paired with
  `--backend`. An optional `--model` flag overrides the per-backend default
  (GPT-5.6-terra for Codex, Haiku for Claude). Every Codex ping is also
  verified against the account's quota reset and recorded (see
  [Recorded Codex pings](#recorded-codex-pings)).
- **`install.py`** — generates and installs the crontab. Given a timezone and
  working hours, it builds a schedule that re-anchors each agent's 5h usage
  window across your day (see [Window model](#window-model)) and pipes the
  result into your crontab. The schedule is computed in your `--tz` and then
  **converted to the system time cron actually schedules against** (see
  [Timezones](#timezones)).
- **`utils/codex_pings.py`** — the Codex recorder `yo` execs into: verifies
  each ping and appends one JSON line per ping.
- **`test_install.py`**, **`test_codex_pings.py`** — unit tests.

## Usage

Run once, ad hoc:

```sh
./yo codex                    # Codex, default model (GPT-5.6-terra)
./yo claude                   # Claude, default model (Haiku)
./yo claude --model opus      # Claude with an explicit model
```

### Recorded Codex pings

Codex exposes its 5h quota reset through the CLI's token-free `app-server`,
which makes anchoring observable: a real anchor locks `resetsAt` at ping+5h,
while an unanchored account reports a hypothetical reset that drifts with the
clock. So every `yo` run on the Codex backend goes through
`utils/codex_pings.py`:

1. **Pre-read.** One quota read, plus a second 15s later only if the first is
   ambiguous (0% used with a reset near now+5h). A window that is already
   open means nothing is sent and the run is recorded as `window_open`.
2. **Ping.** The production invocation, unchanged, via `yo ... --no-record`.
3. **Post-reads.** Two reads 180s apart decide the verdict: `anchored`,
   `not_anchored`, or `inconclusive` (a window is live but was not opened by
   this ping). A ping that failed is `execution_error`; one whose quota could
   not be read afterwards is `observation_error`. A failed pre-read never
   blocks the ping.
4. **Record.** One JSON line appended to `logs/yo-<command>.pings.jsonl`
   with the effective model, effort, thread source and prompt, the CLI
   version and executable, the quota reads, the session id and token usage
   (including cached input tokens, read from the CLI's own session rollout),
   the outcome, and whether the config was yo's `default` or a `manual`
   override. The per-run `yo-<command>-<timestamp>.log` is written as before
   and ends with the verdict.

Exit status is `0` whenever a window is open or the run could not be judged,
`3` when the window is verifiably still closed, and the ping's own status
when the ping itself failed. Each run takes about 3.5 minutes longer than the
ping alone; a per-command lock skips a run that overlaps one still being
verified.

```sh
./yo codex                               # default ping, verified and recorded
./yo codex --no-record                   # raw ping, nothing recorded (smoke test)
./yo codex --prompt 'Calculate 17 * 23.'  # manual override, recorded as such
tail -1 logs/yo-codex.pings.jsonl | python3 -m json.tool
```

Install a schedule (review the snippet, confirm, and it's added to your crontab).
One command per run — for several, run it once each:

```sh
./install.py --tz Europe/Paris --hours 9-18 --command codex
./install.py --tz Europe/Paris --hours 9-18 --command claude
```

Each command gets its own marked block in the crontab, so installing one leaves
the others (and your own crontab lines) untouched. Re-running a command replaces
only its block.

To see what's installed, with each command's run times and log location:

```sh
./install.py --status
```

To stop a command, remove its block (review it, confirm, and it's gone). Only
that block goes; other commands' blocks and your own crontab lines stay:

```sh
./install.py --remove codex
```

A block installed under a name you've since renamed keeps firing until it's
removed; `--status` lists it, so `--remove` it by its old name.

See `./install.py --help` for `--window-hours`, `--num-windows`, and `--yes`.

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

A custom name with no `--backend` is rejected, since the runner is unknown.

`install.py` keys the log and crontab marker on the command name
(`yo-codex-pro`), and bakes the resolved `--backend` into the generated cron
line. Every command must resolve on `PATH` — `install.py` prepends the directory
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
another timezone), **re-run `install.py`** to re-anchor the schedule.

## Logs

Written under `logs/` as `yo-<command>-<timestamp>.log`, with the final message
in `yo-<command>.last.txt`. Codex commands also get `yo-<command>.pings.jsonl`,
one line per recorded ping (see [Recorded Codex pings](#recorded-codex-pings)).

## Anchor test utilities

The server-side rules for which pings anchor a 5h window shift silently (see
issues #9 and PR #14 for the history); when pings stop anchoring, re-bisect
rather than trusting old conclusions. Every recorded ping already carries its
verdict (`logs/yo-<command>.pings.jsonl`), so start there. Two utilities remain for
one-off work:

- `utils/codex_anchor_probe.py [--command WRAPPER] [--model M] [--effort E]
  [--thread-source S] [--prompt TEXT] [--wait SECS] [--force]` — the observer
  the recorder uses, run as a one-shot outside the recorded history: one ping
  via `./yo ... --no-record`, then two rate-limit reads `--wait` seconds apart
  (180s default). Its verdict and evidence go to `logs/gap-anchor-test-*`.
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
- Python 3.10+ on `PATH` for the Codex recorder and the anchor utilities.
- `cron` (the installer pipes into `crontab`).
