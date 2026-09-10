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


# Headless `/usage`: Claude Code answers the slash command in print mode
# without a model turn (the JSON envelope reports num_turns 0 and zero tokens,
# verified on 2.1.265), so this read cannot anchor a window. The CLI resolves
# and refreshes its own credentials, which is why yo goes through it rather
# than the claude.ai usage endpoint (that would need per-profile keychain
# access and a token yo must never refresh).
USAGE_ARGS = ["--print", "--output-format", "json", "/usage"]
USAGE_TIMEOUT_SECS = 60
# The result is prose; these are the limit lines as of 2.1.265, e.g.
#   Current session: 18% used · resets Sep 10 at 4:30pm (Europe/Paris)
#   Current week (all models): 26% used · resets Sep 14 at 5am (Europe/Paris)
#   Current week (Fable): 49% used · resets Sep 14 at 5am (Europe/Paris)
# Reset times come at minute precision in the machine's timezone; keep them as text.
SESSION_RE = re.compile(r"^Current session: ([\d.]+)% used\W+resets (.+?)\s*$", re.MULTILINE)
WEEK_RE = re.compile(r"^Current week \((.+?)\): ([\d.]+)% used\W+resets (.+?)\s*$", re.MULTILINE)


def _number(text):
    value = float(text)
    return int(value) if value.is_integer() else value


# "Sep 10 at 4:30pm (Europe/Paris)", "Sep 14 at 5am (Europe/Paris)": month-day,
# 12h clock with optional minutes, IANA zone in parentheses. No year.
RESET_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}) at (\d{1,2})(?::(\d{2}))?(am|pm) \((\S+)\)$")


def parse_reset(text, now=None):
    """The epoch time of a /usage reset string, or None when it is not in the known shape.

    The string carries no year, so the one that puts the reset closest to `now`
    is used (a late-December read of a January reset lands in the next year).
    """
    m = RESET_RE.match(text.strip())
    if not m:
        return None
    try:
        zone = ZoneInfo(m[6])
        month = datetime.datetime.strptime(m[1], "%b").month
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m[3]) % 12 + (12 if m[5] == "pm" else 0)
    now = datetime.datetime.now(zone) if now is None else datetime.datetime.fromtimestamp(now, zone)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime.datetime(year, month, int(m[2]), hour, int(m[4] or 0), tzinfo=zone))
        except ValueError:  # Feb 29 in a non-leap year
            continue
    return min(candidates, key=lambda dt: abs(dt - now)).timestamp() if candidates else None


def quota(command):
    """The account's quota windows from `<command> --print /usage`, for `yo --status`.

    Returns {"five_hour": {"used_percent", "resets", "resets_at"} or None, "weekly":
    [{"scope", "used_percent", "resets", "resets_at"}...], "spent_turns", "cost_usd"},
    where "resets" is the text as printed and "resets_at" its epoch time when the
    text parses (see parse_reset). Raises RuntimeError when the CLI fails or its
    output is not the /usage view.
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
        return {"used_percent": _number(used), "resets": resets, "resets_at": parse_reset(resets), **extra}

    return {
        "five_hour": window(session[1], session[2]) if session else None,
        "weekly": [window(used, resets, scope=scope) for scope, used, resets in weeks],
        "spent_turns": envelope.get("num_turns", 0),
        "cost_usd": envelope.get("total_cost_usd"),
    }
