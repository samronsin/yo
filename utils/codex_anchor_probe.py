#!/usr/bin/env python3
"""One-shot probe for 5h-window anchoring: does this variant anchor?

Fires a single ping for the given (model, effort, thread_source) variant via
`./yo codex` — so the codex invocation under test is exactly the production
one — then three spaced rateLimits reads decide the verdict: a real anchor
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
import math
import os
import select
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


def read_rate_limits(timeout: float = 30.0, command: str = "codex") -> dict:
    """Read account rate limits via `codex app-server` (spends no tokens)."""
    proc = subprocess.Popen(
        [command, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        for req in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "codex-anchor-probe", "title": "codex-anchor-probe", "version": "0.0.1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
        ):
            proc.stdin.write((json.dumps(req) + "\n").encode())
        proc.stdin.flush()
        # select + os.read, never a bare readline(): a stalled app-server that
        # writes no newline would block readline() forever and the deadline
        # below would never be checked again.
        deadline = time.monotonic() + timeout
        fd = proc.stdout.fileno()
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise RuntimeError(f"no rateLimits response within {timeout:.0f}s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("codex app-server closed stdout without answering")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == 2:
                    if "error" in msg:
                        raise RuntimeError(f"rateLimits read failed: {msg['error']}")
                    primary = (msg.get("result") or {}).get("rateLimits", {}).get("primary")
                    if not primary or primary.get("windowDurationMins") != 300:
                        raise RuntimeError("no supported primary five-hour quota window")
                    reset = primary.get("resetsAt")
                    if (isinstance(reset, bool) or not isinstance(reset, (int, float))
                            or not math.isfinite(reset)):
                        raise RuntimeError("missing or invalid quota reset timestamp")
                    return {"read_at": time.time(), "resets_at": primary["resetsAt"],
                            "used_percent": primary.get("usedPercent")}
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def run_ping(model: str | None, effort: str | None,
             thread_source: str | None, log_path: Path,
             command: str = "codex", prompt: str | None = None,
             backend: str = "codex") -> int:
    # Go through ./yo so the invocation under test is the production one; the
    # ping's own output lands in logs/yo-codex-*.log like any cron run. Only
    # pass overrides that were explicitly requested, so a no-arg probe tests
    # exactly yo's defaults rather than re-stating (and eventually shadowing)
    # them here.
    cmd = [str(ROOT_DIR / "yo"), command, "--backend", backend, "--no-experiment"]
    if prompt is not None:
        cmd += ["--prompt", prompt]
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


def window_state(reads):
    """Distinguish a stable active reset from a hypothetical drifting reset."""
    first, last = reads[0], reads[-1]
    elapsed = last["read_at"] - first["read_at"]
    drift = last["resets_at"] - first["resets_at"]
    if elapsed < 2 * DRIFT_TOLERANCE_SECS:
        return "unknown"
    if all(r["resets_at"] <= r["read_at"] for r in reads):
        return "idle"
    if (max(r["resets_at"] for r in reads) - min(r["resets_at"] for r in reads)
            <= DRIFT_TOLERANCE_SECS and last["resets_at"] > last["read_at"]):
        return "active"
    if (abs(drift - elapsed) <= DRIFT_TOLERANCE_SECS
            and all(abs(r["resets_at"] - r["read_at"] - WINDOW_SECS)
                    <= OPEN_WINDOW_MARGIN_SECS for r in reads)):
        return "idle"
    return "unknown"


def probe(variant, command, log_path, wait=120, force=False):
    """Shared one-shot experiment: one production ping, with evidence and no retry."""
    result = {"variant": variant, "command": command, "started_at": time.time(),
              "outcome": "inconclusive", "pre": [], "reads": []}
    try:
        result["pre"].append(read_rate_limits(command=command))
        time.sleep(15)
        result["pre"].append(read_rate_limits(command=command))
        state = window_state(result["pre"])
        if state != "idle" and not force:
            if state == "active":
                result.update(outcome="no_data", reason="window_already_open")
            else:
                result["outcome"] = "inconclusive"
        else:
            result["ping_start"] = time.time()
            result["returncode"] = run_ping(**variant, command=command, log_path=log_path)
            result["ping_end"] = time.time()
            for i in range(3):
                if i:
                    time.sleep(wait)
                result["reads"].append(read_rate_limits(command=command))
            state = window_state(result["reads"])
            anchor = result["reads"][-1]["resets_at"] - WINDOW_SECS
            if result["returncode"] != 0:
                result["outcome"] = "execution_error"
            elif not force and state == "idle":
                result["outcome"] = "not_anchored"
            elif (not force and state == "active"
                  and result["ping_start"] - DRIFT_TOLERANCE_SECS
                  <= anchor <= result["ping_end"] + DRIFT_TOLERANCE_SECS):
                result["outcome"] = "anchored"
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        result["outcome"] = ("execution_error" if result.get("returncode", 0) != 0
                             or ("ping_start" in result and "ping_end" not in result)
                             else "observation_error")
        result["error"] = str(exc)
    result["finished_at"] = time.time()
    with open(log_path, "a") as log:
        log.write(json.dumps(result) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--command", default="codex", help="Codex executable or wrapper")
    parser.add_argument("--prompt", default="yo")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    parser.add_argument("--thread-source", default=None)
    parser.add_argument("--wait", type=int, default=120, help="seconds between verdict reads")
    parser.add_argument("--force", action="store_true", help="send despite pre-check; verdict is inconclusive")
    args = parser.parse_args()
    if args.wait < 10:
        parser.error("--wait must be at least 10 seconds to distinguish drift")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    variant = {name: getattr(args, name) for name in ("model", "effort", "thread_source", "prompt")}
    result = probe(variant, args.command, log_dir / f"gap-anchor-test-{stamp}.log",
                   args.wait, args.force)
    result_path = log_dir / f"gap-anchor-test-{stamp}.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"VERDICT: {result['outcome']}; evidence={result_path}")
    return {"anchored": 0, "no_data": 1, "not_anchored": 3}.get(result["outcome"], 2)


if __name__ == "__main__":
    sys.exit(main())
