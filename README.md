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

- **`yo`** — the runner. Invokes the selected agent CLI in a
  read-only/non-interactive mode, and writes the output to a
  per-command log. The command is a required first argument: a backend name
  (`codex` or `claude`) or any [custom command](#custom-commands) paired with
  `--backend`. An optional `--model` flag overrides the per-backend default
  (GPT-5.6-terra for Codex, Haiku for Claude).
  Both send one `yo` prompt by default. `--experiment` opts a Codex command into
  recorded trials (see below).
- **`install.py`** — generates and installs the crontab. Given a timezone and
  working hours, it builds a schedule that re-anchors each agent's 5h usage
  window across your day (see [Window model](#window-model)) and pipes the
  result into your crontab. The schedule is computed in your `--tz` and then
  **converted to the system time cron actually schedules against** (see
  [Timezones](#timezones)).
- **`test_install.py`** — unit tests for the schedule helpers.

## Usage

Run once, ad hoc:

```sh
./yo codex                    # Codex, one default ping
./yo claude                   # Claude, default model (Haiku)
./yo claude --model opus      # Claude with an explicit model
```

### Experiments

`--experiment` tests one combination per call. Experiments are Codex-only:
Codex exposes the quota reset the probe verifies against, Claude does not, so
`yo` and `install.py` refuse `--experiment` on the Claude backend rather than
record unverifiable trials. The grid in `utils/experiments.py:PARAMETERS` spans
two models (`gpt-5.6-terra`, `gpt-5.6-luna`), three efforts (low/medium/high),
two thread sources (default/scheduled), and two prompts (`yo`/a short calculation).
These are candidates, not a promise that every model is available to your account.

Explore the least-tested combinations in randomized order. Each experiment is
a single ping. Results describe that
ping only; they do not prove independence from earlier traffic or a usage threshold.
There is no automatic promotion of a winning configuration.
Edit the grid to add models or prompts; CLI overrides pin an axis:

```sh
./yo codex --experiment --model gpt-5.6-terra # explore effort, source, prompt
./yo codex-pro --backend codex --experiment-status
./yo claude --experiment                    # error: codex backend only
./yo codex                                  # original single ping
./yo codex --prompt 'Calculate 17 * 23.'      # manual prompt, no learning
```

With `--experiment`, `--prompt` pins the prompt axis, just as `--model` pins
the model. Without it, `--prompt` simply changes the one-shot prompt on either
backend. `--no-experiment` remains a compatibility alias for ordinary mode and
cannot be combined with experiment options.

The probe checks the window with two readings 15 seconds apart, sends at most
one ping, and reads the quota immediately after completion and again 180 seconds later. An active or
ambiguous window is skipped.
Already-open windows are recorded and reported as `no_data` with reason
`window_already_open`: no prompt is sent and no success is credited.
Observation errors never score a combination; execution errors count as
unsuccessful trials but remain distinguishable from
anchoring failures. Other Codex activity during the request can confound
attribution; an anchor outside the request interval is inconclusive.

If the first ping leaves the window clearly closed, its result is saved before
one recovery attempt: `gpt-5.6-sol`, high effort, with a fixed scheduling problem.
Recovery rechecks the window before sending anything and uses the same verification
timing. Its evidence goes only to `recovery.log`, never trial history or reports.
There are at most two pings total; ambiguous or failed observations stop recovery.
Exit status reflects the final operational result, without changing the first-ping
experiment verdict. Ordinary pings do not use recovery.

A per-command/backend lock prevents overlapping experiments. `history.json` and a
probe log live in `logs/experiments/<command-hash>/`. Trial history continues
across grid, CLI-version, and executable changes; each trial records its settings.
Coverage counts are specific to the current settings. Reads use the same wrapper as the ping.
Changing accounts behind a wrapper is not detected: use a separate command
name per account. Status reads history without sending a prompt or checking
quota. Status shows configuration coverage. This records individual observations,
not a causal proof or a validated recipe for replay.
Evidence from interrupted trials remains in logs
but cannot score. Old per-grid history files are preserved, not automatically
combined with the current history. Existing individual trial records are retained;
obsolete sequence summaries are removed on the next write.

To share observations in a GitHub issue, generate a Markdown report:

```sh
./yo codex-pro --backend codex --experiment-report > codex-experiments.md
```

This is an offline export, not an upload. A short table shows one row per trial;
detailed evidence is in a collapsible JSON section. It includes exact
prompts/configs, CLI versions, outcomes, request durations, and reset drift
across verification reads. Full timestamps and raw readings stay in local
history; the report omits them.
Local paths, command names, raw errors/logs, and absolute timestamps are omitted.
Literal prompts remain included, so review custom prompts before publishing.
A report ID helps identify duplicate submissions. Token counts are not currently
recorded, and external traffic is unknown; the report preserves those limitations.

Existing cron jobs continue sending ordinary pings. Opt a schedule into experiments:

```sh
./install.py --tz Europe/Paris --hours 9-18 --command codex --experiment
```

Experimental schedules space runs 5h 5m apart (ordinary schedules use 5h 2m).
The five-minute margin allows time for verification and recovery before anchoring,
but a slow request can exceed it; the next run skips any still-open window.

Reinstall without `--experiment` to return it to ordinary pings. There is no
immediate retry or automatic schedule rewriting.
Exit codes are `0` for anchored/already active/busy, `3` for not anchored, and
`2` for execution errors or inconclusive observations.

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
decides *when* the window opens. A successful Codex request is not sufficient
evidence that it anchored; the experiment runner checks the observed reset.
Both agents currently get the same schedule.

To keep a freshly-anchored window live across the workday, `yo` *re-anchors* it:
`--num-windows` pings spaced `--window-hours` apart, centered on your working
hours (the first ping fires a bit before you start). For `--hours 9-18` that's
**06:00, 11:02, 16:04** — a new 5h block anchored roughly every 5 hours.

> Both providers also enforce a separate **weekly limit** alongside the 5h
> window. `yo` uses small prompts, but they still consume usage. Scheduling
> around the 5h window does not increase or bypass the weekly allowance.
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
in `yo-<command>.last.txt`.

## Anchor test utilities

The server-side rules for which pings anchor a 5h window shift silently (see
issues #9 and PR #14 for the history); when pings stop anchoring, re-bisect
rather than trusting old conclusions. Two utilities support that:

- `utils/codex_anchor_probe.py [--model M] [--effort E] [--thread-source S]` — fires
  ONE ping via `./yo codex` in an unanchored gap, then takes two account
  rate-limit reads (via `codex app-server`, token-free): one right after the
  ping and another `--wait` seconds later — about 3 minutes total
  with the 180s default. A real anchor locks `resetsAt` at ping+5h; without
  one it drifts with query time. Change one variable per run; an ANCHORED
  verdict closes the gap for ~5h. Refuses to run while a window is open.
  `--command WRAPPER` selects the same wrapper for reads and the ping;
  `--prompt TEXT` tests a custom prompt. This utility bypasses automatic
  experiments so its one-shot trial remains independent.
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
- Python 3.10+ on `PATH` for experiments and the anchor utilities.
- `cron` (the installer pipes into `crontab`).
