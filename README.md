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
./install.py --tz Europe/Paris --hours 9-18 --agent claude
```

That's it — `cron` now pings on schedule. Re-run `install.py` any time to change
the hours or agents. See [Usage](#usage) for more.

## Components

- **`yo`** — the runner. Invokes the selected agent CLI once with the prompt
  `yo`, in a read-only/non-interactive mode, and writes the output to a
  per-agent log. The agent is a required first argument (`codex` or `claude`);
  an optional `--model` flag overrides the per-agent default (GPT-5.4-mini for
  Codex, Haiku for Claude).
- **`install.py`** — generates and installs the crontab. Given a timezone and
  working hours, it builds a schedule that re-anchors each agent's 5h usage
  window across your day (see [Window model](#window-model)) and pipes the
  result into your crontab.
- **`test_install.py`** — unit tests for the schedule helpers.

## Usage

Run once, ad hoc:

```sh
./yo codex                    # Codex, default model (GPT-5.4-mini)
./yo claude                   # Claude, default model (Haiku)
./yo claude --model opus      # Claude with an explicit model
```

Install a schedule (review the snippet, confirm, and it's added to your crontab):

```sh
./install.py --tz Europe/Paris --hours 9-18 --agent codex
./install.py --tz Europe/Paris --hours 9-18 --agent claude
./install.py --tz Europe/Paris --hours 9-18 --agent codex claude    # both
```

Each agent gets their own marked block in the crontab, so installing one agent
leaves the others (and your own crontab lines) untouched. Re-running an agent
replaces only their block.

See `./install.py --help` for `--window-hours`, `--num-windows`, and `--yes`.

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

## Logs

Written under `logs/` as `yo-<agent>-<timestamp>.log`, with the final message in
`yo-<agent>.last.txt`.

## Requirements

- A host that's running whenever the pings should fire — a server or always-on
  VM. `cron` only runs while the machine is up, so a laptop that sleeps overnight
  will miss its scheduled pings (and the anchoring they provide).
- The agent CLI you select (`codex` and/or `claude`) on `PATH`.
- `cron` (the installer pipes into `crontab`).
