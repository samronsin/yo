#!/usr/bin/env python3
"""One-shot probe for 5h-window anchoring: does this variant anchor?

Fires a single ping for the given (model, effort, thread_source) variant via
`./yo codex` — so the codex invocation under test is exactly the production
one — then two spaced rateLimits reads decide the verdict: a real anchor
locks resetsAt at ping+5h, while without one resetsAt is a hypothetical that
drifts with query time. Never judge anchoring from the Codex web UI (it
hides windows at 0% usage).

Bisection etiquette (see PR #14 for a worked example): test in an unanchored
gap, change ONE variable versus a known result, and remember an ANCHORED
verdict closes the gap for ~5h. Refuses to run while a window is open.
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent  # repo root
WINDOW_SECS = 5 * 3600
DRIFT_TOLERANCE_SECS = 5   # locked resetsAt jitters by ~2s server-side
OPEN_WINDOW_MARGIN_SECS = 90  # hypothetical window reads ~now+5h; less means real


def utc(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def read_rate_limits() -> dict:
    """Read account rate limits via `codex app-server` (spends no tokens)."""
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for req in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "gap-anchor-test", "title": "gap-anchor-test", "version": "0.0.1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
        ):
            proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                primary = msg["result"]["rateLimits"]["primary"]
                return {"read_at": time.time(), "resets_at": primary["resetsAt"],
                        "used_percent": primary.get("usedPercent")}
        raise RuntimeError("no rateLimits response within 30s")
    finally:
        proc.kill()


def run_ping(model: str | None, effort: str | None,
             thread_source: str | None, log_path: Path) -> int:
    # Go through ./yo so the invocation under test is the production one; the
    # ping's own output lands in logs/yo-codex-*.log like any cron run. Only
    # pass overrides that were explicitly requested, so a no-arg probe tests
    # exactly yo's defaults rather than re-stating (and eventually shadowing)
    # them here.
    cmd = [str(ROOT_DIR / "yo"), "codex"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if thread_source:
        cmd += ["--thread-source", thread_source]
    with open(log_path, "a") as log:
        log.write(f"+ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, cwd=ROOT_DIR)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=None,
                        help="override; omitted = yo's default model")
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high"],
                        help="override; omitted = yo's default effort")
    parser.add_argument("--thread-source", default=None,
                        help='e.g. "scheduled"; omitted = codex default ("user")')
    parser.add_argument("--wait", type=int, default=120,
                        help="seconds between the two verdict reads")
    parser.add_argument("--force", action="store_true",
                        help="skip the open-window pre-check")
    args = parser.parse_args()

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"gap-anchor-test-{stamp}.log"
    result_path = log_dir / f"gap-anchor-test-{stamp}.json"

    def say(msg: str) -> None:
        line = f"[{utc(time.time())}] {msg}"
        print(line, flush=True)
        with open(log_path, "a") as log:
            log.write(line + "\n")

    variant = (f"model={args.model or 'yo default'} "
               f"effort={args.effort or 'yo default'} "
               f"thread_source={args.thread_source or 'user (default)'}")
    say(f"variant under test: {variant}")

    pre = read_rate_limits()
    hypothetical_gap = pre["read_at"] + WINDOW_SECS - pre["resets_at"]
    say(f"pre-check: resetsAt={utc(pre['resets_at'])} used%={pre['used_percent']} "
        f"(now+5h - resetsAt = {hypothetical_gap:.0f}s)")
    window_open = pre["resets_at"] > pre["read_at"] and hypothetical_gap > OPEN_WINDOW_MARGIN_SECS
    if window_open and not args.force:
        say(f"ABORT: a window is already open (expires {utc(pre['resets_at'])}); "
            "a ping now would join it and prove nothing. Re-run after expiry.")
        return 1

    ping_start = time.time()
    say("sending ping...")
    rc = run_ping(args.model, args.effort, args.thread_source, log_path)
    say(f"ping rc={rc} (output in {log_path.name})")
    if rc != 0:
        say("ABORT: ping failed; no verdict.")
        return 1

    reads = [read_rate_limits()]
    say(f"read #1: resetsAt={utc(reads[0]['resets_at'])} "
        "(anchor and hypothetical both read ~ping+5h here; waiting...)")
    for i in (2, 3):
        time.sleep(args.wait)
        reads.append(read_rate_limits())
        say(f"read #{i}: resetsAt={utc(reads[-1]['resets_at'])} used%={reads[-1]['used_percent']}")

    drift = reads[-1]["resets_at"] - reads[0]["resets_at"]
    elapsed = reads[-1]["read_at"] - reads[0]["read_at"]
    anchored = abs(drift) <= DRIFT_TOLERANCE_SECS
    anchor_delta = reads[0]["resets_at"] - WINDOW_SECS - ping_start

    say(f"drift over {elapsed:.0f}s: {drift:+d}s "
        f"(anchor would be ping{anchor_delta:+.0f}s)")
    if anchored:
        say(f"VERDICT: ANCHORED — resetsAt locked at {utc(reads[-1]['resets_at'])} "
            f"({variant}).")
        say("note: this success closed the gap for ~5h.")
    else:
        say(f"VERDICT: NOT ANCHORED — resetsAt drifts with query time ({variant}); "
            "change one variable and re-test (the gap is still open).")

    result_path.write_text(json.dumps({
        "variant": {"model": args.model, "effort": args.effort,
                    "thread_source": args.thread_source},
        "ping_start": ping_start,
        "pre": pre,
        "reads": reads,
        "drift_secs": drift,
        "anchored": anchored,
    }, indent=2) + "\n")
    say(f"result written to {result_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
