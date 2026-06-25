#!/usr/bin/env python3
"""Generate and install the yo crontab from working hours.

Given a timezone and working hours, schedules --num-windows runs (default 3)
spaced --window-hours apart (default 5; each run covers the stretch until the
next one) and centers that block on the working day so the slack is split
evenly before and after. The run times are converted from the requested
timezone into the system-local time cron schedules against (cron has no
portable per-crontab timezone -- see to_system_times), a cron entry is
installed at each, then the resulting crontab is printed.
"""
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT_DIR = Path(__file__).parent.resolve()
JOB_CMD = str(ROOT_DIR / "yo")
# cron runs with a bare PATH; this is the baseline we give it. The directories
# where the selected agents actually resolve get prepended at install time (see
# cron_path_for), so the jobs find their CLI wherever it lives.
BASE_CRON_PATH = f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin"

# Each successive run is nudged STAGGER_MINUTES later (cumulative) so
# consecutive runs don't fire back-to-back.
STAGGER_MINUTES = 2

# Schedule defaults (also shown as the metavar in --help usage).
DEFAULT_WINDOW_HOURS = 5
DEFAULT_NUM_WINDOWS = 3

# Sentinel markers delimiting a managed block. They're namespaced per agent so
# re-installing one agent replaces only its own block, letting codex and claude
# schedules coexist (install once per agent).
def markers(agent):
    return f"# >>> yo-{agent} >>>", f"# <<< yo-{agent} <<<"


def positive_int(value):
    """argparse type: accept a strictly positive integer."""
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return ivalue


def parse_args():
    parser = argparse.ArgumentParser(description="Install yo cron jobs")
    parser.add_argument("--tz", required=True, help="Timezone, e.g. Europe/Paris")
    parser.add_argument("--hours", required=True, help="Working hours as START-END (24h), e.g. 9-18")
    parser.add_argument("--window-hours", type=positive_int, default=DEFAULT_WINDOW_HOURS,
                        metavar=str(DEFAULT_WINDOW_HOURS), help="Hours each run covers")
    parser.add_argument("--num-windows", type=positive_int, default=DEFAULT_NUM_WINDOWS,
                        metavar=str(DEFAULT_NUM_WINDOWS), help="Number of runs per day")
    parser.add_argument("--agent", nargs="+", required=True, choices=("codex", "claude"),
                        help="Agent CLI(s) to run, one block each")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    return parser.parse_args()


def hour_minute(t):
    """Convert a fractional hour to an (hour, minute) pair on a 24h clock.

    Args:
        t: Time as a fractional hour (e.g. 9.5 means 09:30). May fall outside
            0-24 for warm-up before midnight or overnight windows past 24.

    Returns:
        An (hour, minute) tuple rounded to the nearest minute and wrapped
        modulo 24h, e.g. 9.5 -> (9, 30) and 25 -> (1, 0).
    """
    return divmod(round(t * 60) % (24 * 60), 60)


def compute_run_times(start, end, num_windows, window_hours):
    """Ping schedule (fractional hours) that re-anchors a 5h usage window.

    codex and claude both gate usage with a ~5h window anchored to your first
    message, so they share this schedule. It spaces the runs window_hours apart
    so each run covers the stretch until the next, then centers the whole block
    on the working day, splitting the slack evenly before `start` and after
    `end`. The first run therefore fires before `start`; the effective coverage
    is each stretch clipped to [start, end] -- keeping a freshly-anchored window
    live across the workday.

    Args:
        start, end: Working hours as fractional hours, with end > start
            (callers add 24 to a wrapped overnight end).
        num_windows: Number of runs per day.
        window_hours: Hours each run covers.

    Returns:
        The run times as a list of fractional hours (see hour_minute).
    """
    slack = num_windows * window_hours - (end - start)
    block_start = start - slack / 2
    return [
        block_start + i * window_hours + i * STAGGER_MINUTES / 60
        for i in range(num_windows)
    ]


def to_system_times(run_times, tz):
    """Convert run times from `tz` into the system-local times cron schedules against.

    cron has no portable per-crontab timezone: Debian's cron ignores CRON_TZ
    entirely (`man 5 crontab` LIMITATIONS), running every job in the daemon's own
    timezone regardless. So rather than ask cron to interpret the schedule in `tz`,
    we bake the conversion into the HH:MM fields here -- emitting times in the
    daemon's timezone (which `datetime.astimezone()` reads the same way cron does).
    This is correct on every cron implementation, since the daemon always uses
    system-local time.

    The offset is fixed at install time, so a static crontab drifts by the DST delta
    across a transition; re-run install.py after the clocks change to re-anchor.

    Args:
        run_times: Run times as fractional hours in `tz` (see hour_minute).
        tz: IANA timezone name the run times are expressed in.

    Returns:
        The run times as fractional hours in the system-local timezone.
    """
    source = ZoneInfo(tz)
    ref = datetime.now(source)  # reference date fixes the DST offset to "now"
    out = []
    for t in run_times:
        hour, minute = hour_minute(t)
        local = datetime(ref.year, ref.month, ref.day, hour, minute, tzinfo=source).astimezone()
        out.append(local.hour + local.minute / 60)
    return out


def cron_path_for(agents):
    """Build the cron PATH so the jobs find each agent's CLI where it lives.

    Resolves each agent in the installer's environment (erroring if one is
    missing) and prepends the directory it was found in to BASE_CRON_PATH, so
    cron's PATH points at the real location rather than a hardcoded guess.
    """
    dirs = []
    for agent in agents:
        location = shutil.which(agent)
        if location is None:
            sys.exit(f"error: '{agent}' not found on PATH; install it before scheduling")
        dirs.append(str(Path(location).parent))
    return ":".join(dict.fromkeys(dirs + BASE_CRON_PATH.split(":")))


def render_cron(system_times, tz, agent, cron_path):
    """Render the managed crontab block for the given run times.

    The times are emitted in the daemon's own timezone (see to_system_times); we
    deliberately don't write a CRON_TZ line, since Debian's cron ignores it and
    other crons already schedule in system-local time -- so the converted fields
    are correct everywhere.

    Args:
        system_times: Run times as fractional hours in system-local time
            (see to_system_times and hour_minute).
        tz: IANA timezone the schedule was requested in, recorded as a comment.
        agent: Agent CLI to run, passed to the yo command as its argument.
        cron_path: PATH value for the block (see cron_path_for).

    Returns:
        The crontab text, wrapped in the begin/end markers, ending in a newline.
    """
    begin, end = markers(agent)
    local_tz = datetime.now().astimezone().tzname()
    lines = [
        begin,
        f"# {tz} schedule converted to system time ({local_tz}); re-run install.py after a clock change",
        f"PATH={cron_path}",
        "",
    ]
    for t in system_times:
        hour, minute = hour_minute(t)
        lines.append(f"{minute} {hour} * * * {JOB_CMD} {agent}")
    lines.append(end)
    return "\n".join(lines) + "\n"


def main(args):
    if shutil.which("crontab") is None:
        sys.exit("error: 'crontab' not found on PATH; install cron (e.g. 'apt install cron') and ensure the service is running")

    try:
        ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"error: unknown timezone '{args.tz}'; use an IANA name like Europe/Paris")

    try:
        start, end = (int(x) for x in args.hours.split("-"))
    except ValueError:
        sys.exit("error: --hours must be START-END, e.g. 9-18")
    if end <= start:
        print(
            f"warning: end ({end}) <= start ({start}); assuming working hours wrap "
            f"past midnight (e.g. night shift), treating end as {end}:00 next day",
            file=sys.stderr,
        )
        end += 24  # working hours wrap past midnight (e.g. night shift 22-6)

    agents = list(dict.fromkeys(args.agent))  # de-dupe, preserve order
    cron_path = cron_path_for(agents)  # resolves agents; errors if one is missing

    # The windowed schedule should cover the working day; warn if it can't.
    coverage = args.num_windows * args.window_hours
    if coverage < end - start:
        print(
            f"warning: {args.num_windows} run(s) x {args.window_hours}h = {coverage}h "
            f"cannot cover the {end - start}h working day; some hours will lack a "
            f"fresh window",
            file=sys.stderr,
        )

    run_times = compute_run_times(start, end, args.num_windows, args.window_hours)
    system_times = to_system_times(run_times, args.tz)
    cron_content = "".join(render_cron(system_times, args.tz, a, cron_path) for a in agents)

    requested = ", ".join(f"{h:02d}:{m:02d}" for h, m in map(hour_minute, run_times))
    scheduled = ", ".join(f"{h:02d}:{m:02d}" for h, m in map(hour_minute, system_times))
    local_tz = datetime.now().astimezone().tzname()
    print(f"Scheduled pings ({', '.join(agents)}): {requested} {args.tz} "
          f"-> {scheduled} {local_tz} (cron schedules in system time)")

    print(f"\nGenerated cron snippet:\n\n{cron_content}")

    if not args.yes:
        try:
            reply = input("\nInstall this into your crontab? [y/N] ")
        except EOFError:
            reply = ""  # non-interactive stdin: treat as a decline
        if reply.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted; nothing changed.")

    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0 and "no crontab for" not in existing.stderr.lower():
        sys.exit(f"error: 'crontab -l' failed: {existing.stderr.strip() or existing.returncode}")

    # Drop only the blocks for the agents we're installing, so re-running
    # replaces them while leaving other agents' blocks (and the user's own
    # lines) untouched.
    begins = {markers(a)[0] for a in agents}
    ends = {markers(a)[1] for a in agents}
    kept, skipping = [], False
    for line in existing.stdout.splitlines():
        if line in begins:
            skipping = True
        elif line in ends:
            skipping = False
        elif not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():  # avoid blank-line pile-up across runs
        kept.pop()

    merged = "\n".join(kept) + ("\n" if kept else "") + cron_content
    subprocess.run(["crontab", "-"], input=merged, text=True, check=True)
    print("Crontab updated.")


if __name__ == "__main__":
    main(parse_args())
