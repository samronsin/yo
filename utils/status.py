"""`yo <command> --status`: the account's quota windows and the last run, per backend.

Each backend answers quota() in its runner module, using the CLI's own
token-free read (Codex: `app-server`, Claude: headless `/usage`), so a status
read never spends a model turn and cannot anchor a window itself. This module
adds what both share (the last run log) and renders the report.
"""
import datetime
import json
import re
import subprocess
import time

if __package__:
    from . import claude_runner, codex_runner
    from .codex_anchor_probe import OBSERVATION_ERRORS
else:
    import claude_runner
    import codex_runner
    from codex_anchor_probe import OBSERVATION_ERRORS

STAMP_RE = re.compile(r"-(\d{8}T\d{6})Z\.log$")  # the UTC stamp open_run_log() puts in the name


def last_run(command, log_dir=None):
    """The most recent run log's outcome: when it started, its exit status, its probe verdict if any."""
    logs = codex_runner.run_logs(command, log_dir)
    if not logs:
        return None
    log = logs[-1]
    text = log.read_text(errors="replace")
    entry = {"log": str(log), "returncode": None, "started_at": None}
    stamp = STAMP_RE.search(log.name)
    if stamp:
        started = datetime.datetime.strptime(stamp[1], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
        entry["started_at"] = started.timestamp()
    ended = re.findall(r"yo end rc=(\d+)", text)
    if ended:
        entry["returncode"] = int(ended[-1])
    for line in reversed(text.splitlines()):
        if line.startswith(codex_runner.RECORD_PREFIX):
            try:
                record = json.loads(line[len(codex_runner.RECORD_PREFIX):])
            except json.JSONDecodeError:
                break
            entry.update(outcome=record.get("outcome"), source=record.get("source"))
            break
    return entry


def report(command, backend):
    """Everything `--status` shows, as data; a failed quota read lands in "error" instead of raising."""
    out = {"command": command, "backend": backend, "read_at": time.time(),
           "last_run": last_run(command), "quota": None}
    quota = codex_runner.quota if backend == "codex" else claude_runner.quota
    try:
        out["quota"] = quota(command)
    except FileNotFoundError:
        out["error"] = f"'{command}' is not an executable on PATH (shell functions and aliases do not count)"
    except (*OBSERVATION_ERRORS, subprocess.SubprocessError) as exc:
        out["error"] = str(exc)
    return out


def local(ts, fmt="%Y-%m-%d %H:%M %Z"):
    return datetime.datetime.fromtimestamp(ts).astimezone().strftime(fmt)


def remaining(until, now):
    minutes = max(0, int((until - now) // 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def percent(value):
    return "?%" if value is None else f"{value:g}%"


def format_report(out):
    """Render report() for a human."""
    command, quota, now = out["command"], out["quota"], out["read_at"]
    lines = [f"{command} ({out['backend']}), read {local(now)}"]
    if quota is None:
        lines.append(f"  quota       unavailable: {out.get('error', 'unknown error')}")
    elif out["backend"] == "codex":
        lines += format_codex(quota, now)
    else:
        lines += format_claude(quota)
    run = out["last_run"]
    if run is None:
        lines.append("  last run    none (no run logs)")
    else:
        parts = [local(run["started_at"]) if run["started_at"] else "unknown time"]
        if run.get("outcome"):
            parts.append(f"{run['outcome']} ({run.get('source') or 'ping'})")
        parts.append("still running or crashed" if run["returncode"] is None else f"rc={run['returncode']}")
        parts.append(run["log"])
        lines.append("  last run    " + ", ".join(parts))
    return "\n".join(lines) + "\n"


def format_codex(quota, now):
    five = quota["five_hour"]
    if five is None:
        lines = ["  5h window   not reported by the account"]
    else:
        parts = {"active": ["open"], "idle": ["idle, no window open"],
                 "unknown": ["open or idle? could not tell from two reads"]}[five["state"]]
        parts.append(f"{percent(five['used_percent'])} used")
        if five["state"] == "active":
            parts.append(f"anchored {local(five['anchored_at'], '%H:%M')}")
            parts.append(f"resets {local(five['resets_at'])} ({remaining(five['resets_at'], now)} left)")
        elif five["state"] == "unknown":
            parts.append(f"reports reset {local(five['resets_at'])}")
        lines = ["  5h window   " + ", ".join(parts)]
    for window in quota["weekly"]:
        minutes = window.get("window_minutes")
        days, rest = divmod(minutes or 0, 1440)
        label = "other window" if not minutes else f"{days}d window" if days and not rest else f"{minutes}min window"
        resets = local(window["resets_at"]) if isinstance(window.get("resets_at"), (int, float)) else "?"
        lines.append(f"  {label:<11} {percent(window['used_percent'])} used, resets {resets}")
    return lines


def format_claude(quota):
    five = quota["five_hour"]
    rows = [("5h window", f"{percent(five['used_percent'])} used, resets {five['resets']}"
                          if five else "not reported by /usage")]
    rows += [(f"weekly ({window['scope']})", f"{percent(window['used_percent'])} used, resets {window['resets']}")
             for window in quota["weekly"]]
    width = max(11, *(len(label) for label, _ in rows))  # scoped weekly labels outgrow the default column
    lines = [f"  {label:<{width}} {text}" for label, text in rows]
    if quota.get("spent_turns"):
        lines.append(f"  warning     the /usage call spent {quota['spent_turns']} model turn(s); "
                     "this Claude Code version may not support headless /usage")
    return lines


def main(args):
    """Entry point for `yo <command> --status`: print the report, exit 1 if the quota could not be read."""
    out = report(args.command, args.backend)
    print(format_report(out), end="")
    return 1 if "error" in out else 0
