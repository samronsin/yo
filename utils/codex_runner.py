#!/usr/bin/env python3
"""yo's Codex backend: send one ping, verify it anchored, record it in the run log.

`yo <codex command>` execs into here. The production invocation lives in
codex_command(); the ping is bracketed by utils/codex_anchor_probe.py's
token-free quota reads, and the run log (logs/yo-<command>-<timestamp>.log)
ends with a verdict line and one `record: {...}` JSON line: what ran (model,
effort, thread source, prompt, CLI version, executable), the quota reads,
token usage including cached input, and the outcome. The series is a grep
over the run logs; see load_records.
"""
import argparse
import datetime
import fcntl
import glob
import itertools
import json
import os
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
PROMPT = "yo"
DEFAULTS = {"model": "gpt-5.6-terra", "effort": "medium", "thread_source": ""}
RECORD_PREFIX = "record: "  # marks the machine-readable line in a run log
# Exit status by outcome; execution errors propagate the ping's own status.
EXIT_CODES = {"anchored": 0, "window_open": 0, "inconclusive": 0, "observation_error": 0,
              "not_anchored": 3}


def codex_command(command, config, last_message_file):
    """The production ping invocation; every recorded ping and probe sends exactly this.

    Anchoring the 5h usage window is model-dependent (single-variable A/B pings
    in unanchored gaps, 2026-08-28/29, see PR #14): gpt-5.6-terra anchors with
    this plain invocation, gpt-5.4 only anchored with --thread-source scheduled
    (a codex >= 0.150 flag), and gpt-5.6-luna never anchors. Effort is
    irrelevant (mini@low anchored too), so keep medium. Default to
    gpt-5.6-terra, gpt-5.4's catalog upgrade target (gpt-5.4 retires
    2026-08-31T19:00Z), proven end-to-end by the 2026-08-29 04:30Z cron ping.
    Every recorded ping now carries its own verdict, so check the records
    before trusting the above. Still no --ephemeral (b4ed61a: ephemeral runs
    only join, never anchor) and no --ignore-user-config (29b9fe7: drops
    ~/.codex/config.toml settings the ping needs). Non-ephemeral also persists
    runs to ~/.codex/sessions, where session_usage reads token counts back.
    An unset thread source means codex's own default, "user".
    """
    args = [command, "exec"]
    if config["thread_source"]:
        args += ["--thread-source", config["thread_source"]]
    args += [
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--color", "never",
        "--output-last-message", str(last_message_file),
        "-m", config["model"],
        "-c", f'model_reasoning_effort="{config["effort"]}"',
        "-c", 'model_reasoning_summary="none"',
        "-c", 'model_verbosity="low"',
        "-c", "include_environment_context=false",
        "-c", "include_apps_instructions=false",
        "-c", "include_collaboration_mode_instructions=false",
        "-c", "project_doc_max_bytes=0",
        "-c", "tool_output_token_limit=1024",
        PROMPT,
    ]
    return args


def stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def send_ping(command, config, log_file, last_message_file):
    """Run the ping, append its output to the run log, return details for the record."""
    with open(log_file, "a") as log:
        log.write(f"agent={command} backend=codex model={config['model']} "
                  f"reasoning_effort={config['effort']} "
                  f"thread_source={config['thread_source'] or 'user(default)'}\n")
        log.flush()
        proc = subprocess.run(codex_command(command, config, last_message_file), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT_DIR)
        log.write(proc.stdout)
        log.write(f"[{stamp()}] yo end rc={proc.returncode}\n")
    details = {"returncode": proc.returncode, **parse_output(proc.stdout)}
    if "session_id" in details:
        details["usage"] = session_usage(details["session_id"])
    return details


def parse_output(text):
    """What only Codex's own output can tell us: its version, session and token total."""
    details = {}
    version = re.search(r"OpenAI Codex v(\S+)", text)
    if version:
        details["cli_version"] = version.group(1)
    session = re.search(r"^session id: (\S+)", text, re.MULTILINE)
    if session:
        details["session_id"] = session.group(1)
    tokens = re.search(r"^tokens used\n([\d,]+)", text, re.MULTILINE)
    if tokens:
        details["tokens_used"] = int(tokens.group(1).replace(",", ""))
    return details


def session_usage(session_id):
    """Token usage (incl. cached input) from the session's rollout file, if findable.

    Each ping is a fresh session, so the last token_count event's cumulative
    total covers every request the ping made (a tool call means more than one);
    the per-request figure would only describe the final one.

    A wrapper may point CODEX_HOME elsewhere without telling us, so look under
    our CODEX_HOME when set and otherwise under every ~/.codex*.
    """
    roots = [os.environ["CODEX_HOME"]] if os.environ.get("CODEX_HOME") else glob.glob(os.path.expanduser("~/.codex*"))
    for root in roots:
        for path in glob.glob(os.path.join(root, "sessions", "**", f"rollout-*-{session_id}.jsonl"), recursive=True):
            usage = None
            try:
                with open(path, errors="replace") as rollout:
                    for line in rollout:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload")
                        if event.get("type") != "event_msg" or not isinstance(payload, dict):
                            continue
                        info = payload.get("info")
                        if payload.get("type") != "token_count" or not isinstance(info, dict):
                            continue
                        for key in ("total_token_usage", "last_token_usage"):  # cumulative first
                            if isinstance(info.get(key), dict):
                                usage = info[key]
                                break
            except OSError:
                continue
            if usage:
                return {k: usage.get(k) for k in ("input_tokens", "cached_input_tokens",
                                                  "output_tokens", "reasoning_output_tokens", "total_tokens")}
    return None


def cli_version(command):
    """Fallback for runs that sent nothing (no banner to parse)."""
    try:
        text = subprocess.run([command, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\d+\.\d+\.\d+\S*", text)
    return found.group(0) if found else (text or None)


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


def append(log_file, *lines):
    with open(log_file, "a") as log:
        log.write("".join(line + "\n" for line in lines))


def run(args):
    if getattr(args, "backend", BACKEND) != BACKEND:
        raise ValueError(f"{__name__} only runs the {BACKEND} backend, not {args.backend}")
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"yo-{args.command}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{args.command}: another ping is still being verified; skipped", file=sys.stderr)
            return 0
        overrides = {key: getattr(args, key, "") or "" for key in DEFAULTS}
        config = {key: overrides[key] or DEFAULTS[key] for key in DEFAULTS}
        utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = log_dir / f"yo-{args.command}-{utc}.log"
        for n in itertools.count(2):  # never share a log: it holds exactly one record
            if not log_file.exists():
                break
            log_file = log_dir / f"yo-{args.command}-{utc}-{n}.log"
        append(log_file, f"[{stamp()}] yo start", f"root={ROOT_DIR}")
        last_message_file = log_dir / f"yo-{args.command}.last.txt"
        record = probe(lambda: send_ping(args.command, config, log_file, last_message_file), args.command)
        record.update(schema=SCHEMA, command=args.command, executable=shutil.which(args.command),
                      source="manual" if any(overrides.values()) else "default",
                      effective={**config, "prompt": PROMPT})
        record.setdefault("cli_version", cli_version(args.command))
        if record["outcome"] == "window_open":
            append(log_file, f"[{stamp()}] yo end rc=0 (window already open, nothing sent)")
        verdict = f"[{stamp()}] {record['source']} ping: {record['outcome']}"
        append(log_file, verdict, RECORD_PREFIX + json.dumps(record, sort_keys=True))
        if sys.stdout.isatty():  # someone is watching; cron stays quiet
            print(verdict, flush=True)
        if record["outcome"] == "execution_error":
            return record.get("returncode") or 2
        return EXIT_CODES.get(record["outcome"], 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command")
    parser.add_argument("--backend", default=BACKEND, help=f"only {BACKEND} is supported")
    for name in ("model", "effort", "thread-source"):
        parser.add_argument(f"--{name}", default="", help="override the default")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
