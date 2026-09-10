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
    from .codex_anchor_probe import OBSERVATION_ERRORS, WINDOW_SECS
else:
    import claude_runner
    import codex_runner
    from codex_anchor_probe import OBSERVATION_ERRORS, WINDOW_SECS

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
    """Render report() for a human: one label/text row per line, labels padded to the widest."""
    command, quota, now = out["command"], out["quota"], out["read_at"]
    rows = []
    if quota is None:
        rows.append(("quota", f"unavailable: {out.get('error', 'unknown error')}"))
    elif out["backend"] == "codex":
        rows += codex_rows(quota, now)
    else:
        rows += claude_rows(quota, now)
    run = out["last_run"]
    if run is None:
        rows.append(("last run", "none (no run logs)"))
    else:
        parts = [local(run["started_at"]) if run["started_at"] else "unknown time"]
        if run.get("outcome"):
            parts.append(f"{run['outcome']} ({run.get('source') or 'ping'})")
        parts.append("still running or crashed" if run["returncode"] is None else f"rc={run['returncode']}")
        parts.append(run["log"])
        rows.append(("last run", ", ".join(parts)))
    width = max(11, *(len(label) for label, _ in rows))
    lines = [f"{command} ({out['backend']}), read {local(now)}"]
    lines += [f"  {label:<{width}} {text}" for label, text in rows]
    return "\n".join(lines) + "\n"


def five_hour_text(state, used, resets_at, now, resets_text=None):
    """The 5h window's row, the same shape for both backends.

    `state` is "active", "idle" or "unknown"; `resets_at` an epoch time or None
    (then `resets_text` is shown as printed by the CLI).
    """
    parts = {"active": ["open"], "idle": ["idle, no window open"],
             "unknown": ["open or idle? cannot tell"]}[state]
    parts.append(f"{percent(used)} used")
    if isinstance(resets_at, (int, float)):
        if state == "active":
            parts.append(f"anchored {local(resets_at - WINDOW_SECS, '%H:%M')}")
            parts.append(f"resets {local(resets_at)} ({remaining(resets_at, now)} left)")
        elif state == "unknown":
            parts.append(f"reports reset {local(resets_at)}")
    elif resets_text and state != "idle":
        parts.append(f"resets {resets_text}")
    return ", ".join(parts)


def window_text(used, resets_at, resets_text=None):
    """A weekly (or other) window's row."""
    if isinstance(resets_at, (int, float)):
        return f"{percent(used)} used, resets {local(resets_at)}"
    return f"{percent(used)} used, resets {resets_text or '?'}"


def window_label(minutes, scope=None):
    if minutes == 7 * 24 * 60:
        label = "weekly"
    elif not minutes:
        label = "other window"
    else:
        days, rest = divmod(minutes, 1440)
        label = f"{days}d window" if days and not rest else f"{minutes}min window"
    return f"{label} ({scope})" if scope else label


def codex_rows(quota, now):
    five = quota["five_hour"]
    rows = [("5h window", "not reported by the account" if five is None
             else five_hour_text(five["state"], five["used_percent"], five["resets_at"], now))]
    rows += [(window_label(window.get("window_minutes")), window_text(window["used_percent"], window.get("resets_at")))
             for window in quota["weekly"]]
    return rows


def claude_rows(quota, now):
    """Claude's view has no idle/active flag: usage above 0% proves a live window, 0% leaves it open."""
    five = quota["five_hour"]
    if five is None:
        rows = [("5h window", "not reported by /usage")]
    else:
        state = "active" if (five["used_percent"] or 0) > 0 else "unknown"
        rows = [("5h window", five_hour_text(state, five["used_percent"], five["resets_at"], now, five["resets"]))]
    rows += [(window_label(7 * 24 * 60, None if window["scope"] == "all models" else window["scope"]),
              window_text(window["used_percent"], window["resets_at"], window["resets"]))
             for window in quota["weekly"]]
    if quota.get("spent_turns"):
        rows.append(("warning", f"the /usage call spent {quota['spent_turns']} model turn(s); "
                                "this Claude Code version may not support headless /usage"))
    return rows


def main(args):
    """Entry point for `yo <command> --status`: print the report, exit 1 if the quota could not be read."""
    out = report(args.command, args.backend)
    print(format_report(out), end="")
    return 1 if "error" in out else 0
