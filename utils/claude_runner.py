"""yo's Claude backend: send a plain ping (run) or read the account's quota windows (quota)."""
import datetime
import json
import re
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from . import codex_runner as shared
else:
    import codex_runner as shared


def run(args):
    """args are yo's parsed arguments (command, model); Claude has no quota observer, so no --probe."""
    model = args.model or "haiku"
    log_file, last_message_file = shared.open_run_log(args.command)
    with log_file.open("a") as log:
        log.write(f"agent={args.command} backend=claude model={model}\n")
        log.flush()
        try:
            # Claude's stdout is the last message; stderr goes directly to the run log.
            with last_message_file.open("w") as last:
                proc = subprocess.run([
                    args.command, "--print", "--model", model,
                    "--output-format", "text", "--permission-mode", "default", "yo",
                ], stdout=last, stderr=log, cwd=shared.ROOT_DIR)
            rc = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
            log.write(last_message_file.read_text(errors="replace"))
        except OSError as exc:
            # Whatever failed (the executable, the last-message file), the run
            # log still gets the error and its terminator, like the shell did.
            log.write(f"{args.command}: {exc}\n")
            if exc.filename == str(last_message_file):
                rc = 1
            else:
                rc = 127 if isinstance(exc, FileNotFoundError) else 126
        log.write(f"[{shared.stamp()}] yo end rc={rc}\n")
    return rc


# Headless /usage spends no model turn on Claude Code 2.1.265.
# Use the CLI so it handles credentials; check num_turns for incompatible versions.
USAGE_ARGS = ["--print", "--output-format", "json", "/usage"]
USAGE_TIMEOUT_SECS = 60
WEEK_MINUTES = 7 * 24 * 60  # the "Current week" lines, in codex_runner.quota's window_minutes terms
# The result is prose, e.g.:
#   Current session: 18% used · resets Sep 10 at 4:30pm (Europe/Paris)
#   Current week (all models): 26% used · resets Sep 14 at 5am (Europe/Paris)
SESSION_RE = re.compile(r"^Current session: ([\d.]+)% used\W+resets (.+?)\s*$", re.MULTILINE)
WEEK_RE = re.compile(r"^Current week \((.+?)\): ([\d.]+)% used\W+resets (.+?)\s*$", re.MULTILINE)

# Optional year, "at" or comma separator, 12h clock and IANA timezone.
RESET_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2})(?:,? (\d{4}))?(?: at|,) (\d{1,2})(?::(\d{2}))? ?([AaPp][Mm]) \((\S+)\)$")


def parse_reset(text, now=None):
    """Parse a /usage reset, choosing the nearest year if omitted; return None if unknown."""
    m = RESET_RE.match(text.strip())
    if not m:
        return None
    month_name, day, year, hour, minute, meridiem, zone_name = m.groups()
    try:
        zone = ZoneInfo(zone_name)
        month = datetime.datetime.strptime(month_name, "%b").month
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    now = datetime.datetime.now(zone) if now is None else datetime.datetime.fromtimestamp(now, zone)
    years = [int(year)] if year else [now.year - 1, now.year, now.year + 1]
    candidates = []
    for candidate in years:
        try:
            candidates.append(datetime.datetime(candidate, month, int(day), hour, int(minute or 0), tzinfo=zone))
        except ValueError:  # Feb 29 in a non-leap year
            continue
    return min(candidates, key=lambda dt: abs(dt - now)).timestamp() if candidates else None


def quota(command):
    """Read /usage windows in codex_runner.quota's shape; raise on CLI errors.

    "resets" keeps the text as printed for when parse_reset() cannot read it.
    The view has no open/idle flag: usage above 0% proves a live window, 0%
    cannot tell (state "unknown").
    """
    proc = subprocess.run([command, *USAGE_ARGS], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=USAGE_TIMEOUT_SECS, cwd=shared.ROOT_DIR)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError(f"{command} /usage exited {proc.returncode}: {detail[-1][:200] if detail else 'no output'}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"unexpected /usage output: {proc.stdout.strip()[:120]!r}") from None
    text = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(envelope, dict) or envelope.get("is_error") or not isinstance(text, str):
        raise RuntimeError(f"unexpected /usage result: {proc.stdout.strip()[:200]!r}")
    session = SESSION_RE.search(text)
    weeks = WEEK_RE.findall(text)
    if not session and not weeks:
        first = text.strip().splitlines()[0] if text.strip() else ""
        raise RuntimeError(f"unrecognized /usage output: {first[:120]!r}")
    def window(used, resets, **extra):
        return {"used_percent": float(used), "resets": resets, "resets_at": parse_reset(resets), **extra}

    return {
        "five_hour": window(session[1], session[2], state="active" if float(session[1]) > 0 else "unknown")
        if session else None,
        "weekly": [window(used, resets, window_minutes=WEEK_MINUTES,
                          scope=None if scope == "all models" else scope)
                   for scope, used, resets in weeks],
        "spent_turns": envelope.get("num_turns", 0),
        "cost_usd": envelope.get("total_cost_usd"),
    }
