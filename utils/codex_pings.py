#!/usr/bin/env python3
"""Record every Codex ping with its verified anchoring outcome.

`yo <codex command>` execs into here. Each run's log (logs/yo-<command>-
<timestamp>.log) ends with a verdict line and one `record: {...}` JSON line:
what ran (model, effort, thread source, prompt, CLI version, executable), the
quota reads around it, token usage including cached input, and the verdict
from utils/codex_anchor_probe.py. The series is a grep over the run logs; see
load_records.
"""
import argparse
import datetime
import fcntl
import itertools
import json
import re
import shutil
import subprocess
import sys

if __package__:
    from .codex_anchor_probe import ROOT_DIR, probe
else:
    from codex_anchor_probe import ROOT_DIR, probe

SCHEMA = 1
BACKEND = "codex"  # the only backend with a quota observer
OVERRIDES = ("model", "effort", "thread_source")
RECORD_PREFIX = "record: "  # marks the machine-readable line in a run log
# Exit status by outcome; execution errors propagate the ping's own status.
EXIT_CODES = {"anchored": 0, "window_open": 0, "inconclusive": 0, "observation_error": 0,
              "not_anchored": 3}


def run_logs(command, log_dir=None):
    """This command's run logs, oldest first (a same-second collision suffix sorts after its base)."""
    def order(path):
        found = re.fullmatch(rf"yo-{re.escape(command)}-(\d{{8}}T\d{{6}}Z)(?:-(\d+))?\.log", path.name)
        return (found.group(1), int(found.group(2) or 1)) if found else (path.name, 0)
    return sorted((log_dir or ROOT_DIR / "logs").glob(f"yo-{command}-*.log"), key=order)


def load_records(command, log_dir=None):
    """Every recorded ping for `command`, oldest first (one per run log)."""
    records = []
    for log in run_logs(command, log_dir):
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith(RECORD_PREFIX):
                try:
                    records.append(json.loads(line[len(RECORD_PREFIX):]))
                except json.JSONDecodeError:
                    continue  # a torn line from a crash mid-write never hides the rest
    return records


def cli_version(command):
    try:
        text = subprocess.run([command, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\d+\.\d+\.\d+\S*", text)
    return found.group(0) if found else (text or None)


def say(log_file, message):
    """Verdicts go to the ping's own log; stdout only when someone is watching (cron stays quiet)."""
    try:
        with open(log_file, "a") as log:
            log.write(message + "\n")
    except OSError:
        pass
    if sys.stdout.isatty():
        print(message, flush=True)


def run(args):
    if getattr(args, "backend", BACKEND) != BACKEND:
        raise ValueError(f"recorded pings are only supported with the {BACKEND} backend, not {args.backend}")
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"yo-{args.command}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{args.command}: another ping is still being verified; skipped", file=sys.stderr)
            return 0
        variant = {key: getattr(args, key, "") or "" for key in OVERRIDES}
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = log_dir / f"yo-{args.command}-{stamp}.log"
        for n in itertools.count(2):  # never share a log: it is parsed back and holds one record
            if not log_file.exists():
                break
            log_file = log_dir / f"yo-{args.command}-{stamp}-{n}.log"
        record = probe(variant, args.command, log_file)
        record.update(schema=SCHEMA, command=args.command, executable=shutil.which(args.command),
                      source="manual" if any(variant.values()) else "default")
        record.setdefault("cli_version", cli_version(args.command))
        say(log_file, f"[{datetime.datetime.now().isoformat(timespec='seconds')}] "
                      f"{record['source']} ping: {record['outcome']}")
        with open(log_file, "a") as log:
            log.write(RECORD_PREFIX + json.dumps(record, sort_keys=True) + "\n")
        if record["outcome"] == "execution_error":
            return record.get("returncode") or 2
        return EXIT_CODES.get(record["outcome"], 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command")
    parser.add_argument("--backend", default=BACKEND, help=f"only {BACKEND} is supported")
    for name in ("model", "effort", "thread-source"):
        parser.add_argument(f"--{name}", default="", help="override yo's default")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
