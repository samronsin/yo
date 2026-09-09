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
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT_DIR = Path(__file__).parent.resolve()
JOB_CMD = str(ROOT_DIR / "yo")
# cron runs with a bare PATH; this is the baseline we give it. The directories
# where the selected commands actually resolve get prepended at install time (see
# cron_path_for), so the jobs find their CLI wherever it lives.
BASE_CRON_PATH = f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin"

# Each successive run is nudged STAGGER_MINUTES later (cumulative) so
# consecutive runs don't fire back-to-back.
STAGGER_MINUTES = 2

# Schedule defaults (also shown as the metavar in --help usage).
DEFAULT_WINDOW_HOURS = 5
DEFAULT_NUM_WINDOWS = 3

# Sentinel markers delimiting a managed block. They're namespaced per command so
# re-installing one command replaces only its own block, letting schedules
# coexist (install once per command). MARKER_RE (below) is the reverse mapping.
def markers(command):
    return f"# >>> yo-{command} >>>", f"# <<< yo-{command} <<<"


def positive_int(value):
    """argparse type: accept a strictly positive integer."""
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return ivalue


# Backends whose runner flags `yo` knows.
BACKENDS = ("codex", "claude")

# A command name goes unquoted into the crontab line (run by cron's /bin/sh),
# the block markers, and the log filename, so restrict it to a conservative
# shell- and path-safe set -- an executable resolvable on PATH never needs more.
# Must start alphanumeric so it can't be read as a flag or a dotfile.
COMMAND_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def command_name(value):
    """argparse `type=` converter for --command: validate the command's charset.

    An argparse `type=` callable receives the raw string and its return value
    becomes the parsed argument, so this validates and returns `value`
    unchanged -- no conversion, but the return is what argparse stores.
    The command is what `yo` invokes and the crontab marker / log key; it's
    restricted so it stays safe unquoted in the crontab line it's written to.
    The runner backend is a separate --backend (see resolve_backend), mirroring
    `yo <command> --backend ...`.
    """
    if value == "":
        raise argparse.ArgumentTypeError("empty command name")
    if not COMMAND_NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"command name '{value}' must start with a letter or digit and use "
            f"only letters, digits, '.', '_', '-' (it's written unquoted into "
            f"the crontab)")
    return value


def resolve_backend(command, backend):
    """Decide the runner backend for `command`.

    A bare backend name ("codex"/"claude") is its own backend; if --backend is
    also given it must agree. Any other command requires --backend, since we
    can't tell which runner flags to apply otherwise.

    Args:
        command: The command to run (see command_name).
        backend: The --backend value (a backend name), or None.

    Returns:
        The resolved backend name; exits with an error if it can't be decided.
    """
    if command in BACKENDS:
        if backend is not None and backend != command:
            sys.exit(f"error: command '{command}' runs the {command} backend, but --backend {backend} was given; drop --backend or pass --backend {command}")
        return command
    if backend is None:
        sys.exit(f"error: '{command}' is a custom command; pass --backend codex|claude")
    return backend


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Install yo cron jobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true",
                      help="Show the installed yo cron jobs and exit")
    mode.add_argument("--remove", type=command_name, metavar="NAME",
                      help="Remove NAME's cron block (see --status) and exit; "
                           "other blocks and your own crontab lines are kept")
    parser.add_argument("--tz", help="Timezone, e.g. Europe/Paris (required to install)")
    parser.add_argument("--hours", help="Working hours as START-END (24h), e.g. 9-18 (required to install)")
    parser.add_argument("--window-hours", type=positive_int, default=DEFAULT_WINDOW_HOURS,
                        metavar=str(DEFAULT_WINDOW_HOURS), help="Hours each run covers")
    parser.add_argument("--num-windows", type=positive_int, default=DEFAULT_NUM_WINDOWS,
                        metavar=str(DEFAULT_NUM_WINDOWS), help="Number of runs per day")
    parser.add_argument("--command", type=command_name, metavar="NAME",
                        help="Command to run (required to install): a backend "
                             "(codex/claude), or a custom executable "
                             "script/binary on PATH whose runner is given by "
                             "--backend (e.g. codex-pro). Shell aliases are not "
                             "supported by cron. Run once per command; each gets "
                             "its own crontab block.")
    parser.add_argument("--backend", choices=BACKENDS,
                        help="Runner backend for a custom --command; a backend "
                             "name (codex/claude) is its own backend")
    parser.add_argument("--no-probe", action="store_true",
                        help="Schedule plain Codex pings instead of verified, recorded ones (see yo --probe)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args(argv)
    # --status and --remove act on the installed crontab without computing a
    # schedule, so the schedule flags are required (and only meaningful) to
    # install. Three flag-selected modes is the ceiling before subcommands.
    required = (("--tz", args.tz), ("--hours", args.hours), ("--command", args.command))
    mode_flag = "--status" if args.status else "--remove" if args.remove else None
    if mode_flag is None:
        missing = [flag for flag, value in required if value is None]
        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")
    else:
        if args.no_probe:
            parser.error(f"{mode_flag} cannot be combined with --no-probe")
        given = [flag for flag, value in (*required, ("--backend", args.backend))
                 if value is not None]
        if given:
            parser.error(f"{mode_flag} cannot be combined with {', '.join(given)}")
    return args


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


def cron_path_for(commands):
    """Build the cron PATH so the jobs find each command where it lives.

    Resolves each command in the installer's environment with shutil.which
    (erroring if one is missing) and prepends the directory it was found in to
    BASE_CRON_PATH, so cron's PATH points at the real location rather than a
    hardcoded guess. Shell aliases are intentionally rejected: cron will not load
    an interactive shell's alias definitions.
    """
    dirs = []
    for command in commands:
        location = shutil.which(command)
        if location is None:
            sys.exit(
                f"error: '{command}' not found as an executable on PATH; "
                "scheduled commands must be wrapper scripts or binaries, not shell aliases"
            )
        dirs.append(str(Path(location).parent))
    return ":".join(dict.fromkeys(dirs + BASE_CRON_PATH.split(":")))


def render_cron(system_times, tz, command, backend, cron_path, probe=None):
    """Render the managed crontab block for the given run times.

    The times are emitted in the daemon's own timezone (see to_system_times); we
    deliberately don't write a CRON_TZ line, since Debian's cron ignores it and
    other crons already schedule in system-local time -- so the converted fields
    are correct everywhere.

    Args:
        system_times: Run times as fractional hours in system-local time
            (see to_system_times and hour_minute).
        tz: IANA timezone the schedule was requested in, recorded as a comment.
        command: Command `yo` invokes, also the marker/log key (see command_name).
        backend: Runner backend for `command`; emitted as `--backend` unless
            `command` is itself a backend (then `yo` infers it and the line
            stays bare).
        cron_path: PATH value for the block (see cron_path_for).
        probe: Emit `--probe` so each run is verified and recorded. Defaults to
            True for the codex backend (the only one with a quota observer)
            and is never emitted for others.

    Returns:
        The crontab text, wrapped in the begin/end markers, ending in a newline.
    """
    if probe is None:
        probe = backend == "codex"
    begin, end = markers(command)
    local_tz = datetime.now().astimezone().tzname()
    lines = [
        begin,
        f"# {tz} schedule converted to system time ({local_tz}); re-run install.py after a clock change",
        f"PATH={cron_path}",
        "",
    ]
    backend_arg = "" if command == backend else f" --backend {backend}"
    probe_arg = " --probe" if probe and backend == "codex" else ""
    for t in system_times:
        hour, minute = hour_minute(t)
        lines.append(f"{minute} {hour} * * * {JOB_CMD} {command}{backend_arg}{probe_arg}")
    lines.append(end)
    return "\n".join(lines) + "\n"


def read_crontab():
    """The user's current crontab text ("" if they have none)."""
    if shutil.which("crontab") is None:
        sys.exit("error: 'crontab' not found on PATH; install cron (e.g. 'apt install cron') and ensure the service is running")
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0 and "no crontab for" not in existing.stderr.lower():
        sys.exit(f"error: 'crontab -l' failed: {existing.stderr.strip() or existing.returncode}")
    return existing.stdout


def write_crontab(lines):
    """Replace the user's crontab with `lines` (an empty list installs an empty crontab)."""
    text = "\n".join(lines) + ("\n" if lines else "")
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def confirm(question):
    """Ask a y/N question on stdin; a non-interactive stdin counts as a decline."""
    try:
        reply = input(question)
    except EOFError:
        reply = ""
    return reply.strip().lower() in ("y", "yes")


# Matches either marker line written by markers(); group 1 tells begin (>>>)
# from end (<<<), group 2 is the command.
MARKER_RE = re.compile(rf"# (>>>|<<<) yo-({COMMAND_NAME_RE.pattern}) \1")


class MalformedCrontab(Exception):
    """A yo-managed block has broken markers, so its extent can't be trusted."""


def split_managed_blocks(crontab):
    """Split crontab text into (command, lines) segments.

    `command` is None for runs of the user's own lines and the block's command
    for a yo-managed block (markers included). A block that's unterminated or
    contains another marker raises MalformedCrontab rather than guessing where
    it ends, since a wrong guess could drop lines the user wrote.
    """
    segments, command, lines = [], None, []
    for line in crontab.splitlines():
        m = MARKER_RE.fullmatch(line)
        if command is None and m and m[1] == ">>>":
            if lines:
                segments.append((None, lines))
            command, lines = m[2], [line]
        elif command is not None and m:
            if (m[1], m[2]) != ("<<<", command):
                raise MalformedCrontab(f"yo-{command} block contains an unexpected marker: {line}")
            segments.append((command, lines + [line]))
            command, lines = None, []
        else:
            lines.append(line)
    if command is not None:
        raise MalformedCrontab(f"yo-{command} block is missing its end marker")
    if lines:
        segments.append((None, lines))
    return segments


def remove_managed_block(crontab, command):
    """The crontab's lines without `command`'s managed block (other blocks stay)."""
    kept = [line for owner, lines in split_managed_blocks(crontab) if owner != command
            for line in lines]
    while kept and not kept[-1].strip():  # avoid blank-line pile-up across runs
        kept.pop()
    return kept


# A cron schedule field: digits, names, `*`, ranges, lists, steps. Excludes the
# other line shapes cron allows (comments, NAME=value, @reboot-style shortcuts).
CRON_FIELD_RE = re.compile(r"[\w*,/-]+")


def parse_cron_entry(line):
    """Split a cron job line into (schedule, command); None for any other line."""
    parts = line.split(None, 5)
    if len(parts) != 6 or not all(CRON_FIELD_RE.fullmatch(f) for f in parts[:5]):
        return None
    return " ".join(parts[:5]), parts[5].strip()


def describe_schedule(schedule):
    """A daily `M H * * *` schedule (what render_cron writes) as HH:MM; others as written."""
    m = re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* \*", schedule)
    return f"{int(m[2]):02d}:{int(m[1]):02d}" if m else schedule


def installed_jobs(crontab):
    """[(command, [(schedule, command_line), ...]), ...] for each yo-managed block."""
    return [
        (command, [entry for entry in map(parse_cron_entry, lines) if entry])
        for command, lines in split_managed_blocks(crontab)
        if command is not None
    ]


def format_status(jobs):
    """Render installed_jobs() for a human."""
    if not jobs:
        return "No yo cron jobs installed.\n"
    out = ["Installed yo cron jobs:"]
    for command, entries in jobs:
        schedule = ", ".join(describe_schedule(s) for s, _ in entries)
        command_lines = list(dict.fromkeys(line for _, line in entries))
        # yo logs next to itself (see LOG_DIR in yo), so locate the logs from the
        # installed yo's path rather than this checkout's.
        yo_path = Path(command_lines[0].split()[0]) if command_lines else ROOT_DIR / "yo"
        log_dir = yo_path.parent / "logs"
        out += ["", command,
                f"  schedule: {schedule} system time" if schedule else "  schedule: (no cron entries)",
                *(f"  command:  {line}" for line in command_lines),
                f"  logs:     {log_dir / f'yo-{command}-<timestamp>.log'}",
                f"  last:     {log_dir / f'yo-{command}.last.txt'}"]
    return "\n".join(out) + "\n"


def main(args):
    try:
        if args.status:
            print(format_status(installed_jobs(read_crontab())), end="")
        elif args.remove:
            remove_schedule(args)
        else:
            install_schedule(args)
    except MalformedCrontab as exc:
        sys.exit(f"error: {exc}; fix it by hand (crontab -e) and retry")


def install_schedule(args):
    crontab = read_crontab()  # fails fast when crontab isn't available

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

    command = args.command
    backend = resolve_backend(command, args.backend)
    cron_path = cron_path_for([command])  # resolves the command; errors if it's missing
    # Preflight: a malformed crontab should fail before the user approves anything.
    remove_managed_block(crontab, command)

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
    cron_content = render_cron(system_times, args.tz, command, backend, cron_path,
                               probe=backend == "codex" and not args.no_probe)

    requested = ", ".join(f"{h:02d}:{m:02d}" for h, m in map(hour_minute, run_times))
    scheduled = ", ".join(f"{h:02d}:{m:02d}" for h, m in map(hour_minute, system_times))
    local_tz = datetime.now().astimezone().tzname()
    print(f"Scheduled pings ({command}): {requested} {args.tz} "
          f"-> {scheduled} {local_tz} (cron schedules in system time)")

    print(f"\nGenerated cron snippet:\n\n{cron_content}")

    if not args.yes and not confirm("\nInstall this into your crontab? [y/N] "):
        sys.exit("Aborted; nothing changed.")

    # Drop only this command's block, so re-running replaces it while leaving
    # other commands' blocks (and the user's own lines) untouched. Re-read now:
    # the crontab may have changed while the user was reviewing the prompt.
    kept = remove_managed_block(read_crontab(), command)
    write_crontab(kept + cron_content.splitlines())
    print("Crontab updated.")


def remove_schedule(args):
    """Drop `args.remove`'s managed block from the crontab, leaving the rest as is."""
    command = args.remove
    # Splitting also refuses a malformed crontab before anything is shown or asked.
    segments = split_managed_blocks(read_crontab())
    block = [line for owner, lines in segments if owner == command for line in lines]
    if not block:
        installed = [owner for owner, _ in segments if owner is not None]
        hint = (f"installed: {', '.join(installed)} (see --status)" if installed
                else "no yo cron jobs installed")
        sys.exit(f"error: no yo-{command} block in the crontab; {hint}")

    print(f"Cron block to remove ({command}):\n\n" + "\n".join(block) + "\n")

    if not args.yes and not confirm("\nRemove this from your crontab? [y/N] "):
        sys.exit("Aborted; nothing changed.")

    # Re-read: the crontab may have changed while the user was reviewing the prompt.
    write_crontab(remove_managed_block(read_crontab(), command))
    print(f"Crontab updated; yo-{command} block removed.")


if __name__ == "__main__":
    main(parse_args())
