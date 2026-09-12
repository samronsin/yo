#!/usr/bin/env python3
"""Codex 5h-window observer: did this ping anchor a window?

Every `yo <codex command>` run goes through probe(): a token-free rateLimits
read before the ping (a second one 15s later only when the first is
ambiguous), the production ping (utils/codex_runner.py), then after a 10s
settle two reads 60s apart. A real anchor locks resetsAt at ping+5h; without
one resetsAt is a hypothetical that drifts with query time. Never judge
anchoring from the Codex web UI (it hides windows at 0% usage).

Run directly for a one-off trial outside the recorded history (see main):
change ONE variable versus a known result, and remember an ANCHORED verdict
closes the gap for ~5h.
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
PRE_WAIT_SECS = 15    # second pre-read, only when one read cannot tell idle from active
SETTLE_SECS = 10      # after the ping, before the first post-read, so a late-registering anchor reads locked
POST_WAIT_SECS = 60   # between the two post-ping reads that decide the verdict (drift 60s vs 5s tolerance)
OBSERVATION_ERRORS = (OSError, RuntimeError, ValueError, KeyError)


def utc(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def read_rate_limits(timeout: float = 30.0, command: str = "codex") -> dict:
    """The 5h window's read, {"read_at", "resets_at", "used_percent"} (spends no tokens)."""
    return five_hour_read(read_limits_payload(timeout, command))


def read_limits_payload(timeout: float = 30.0, command: str = "codex") -> dict:
    """The account's whole rateLimits object via `codex app-server` (spends no tokens)."""
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
                    result = msg.get("result")
                    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
                        raise RuntimeError(f"unexpected rateLimits response: {line[:200]!r}")
                    return result["rateLimits"]
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def five_hour_read(rate_limits) -> dict:
    """Reduce a rateLimits payload to the 5h window's read, stamped with the read time."""
    window = five_hour_window(rate_limits)
    reset = window.get("resetsAt")
    if (isinstance(reset, bool) or not isinstance(reset, (int, float))
            or not math.isfinite(reset)):
        raise RuntimeError("missing or invalid quota reset timestamp")
    return {"read_at": time.time(), "resets_at": reset, "used_percent": window.get("usedPercent")}


def five_hour_window(rate_limits):
    """The 5h limit out of the account's limits, wherever it sits.

    Every verdict here assumes a 300-minute window (WINDOW_SECS). It is
    normally `primary` with the weekly limit as `secondary`, but a plan can
    report the weekly limit alone (seen 2026-08-08 to 08-24), and judging a
    weekly reset with 5h arithmetic would give a confident wrong answer.
    """
    for slot in ("primary", "secondary"):
        limit = rate_limits.get(slot)
        if isinstance(limit, dict) and limit.get("windowDurationMins") == WINDOW_SECS // 60:
            return limit
    raise RuntimeError("the account reports no five-hour quota window")


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


def quick_state(read):
    """Window state from a single read when it is unambiguous, else None.

    Usage above 0% only exists inside a live window, and a reset in the past
    means nothing is open. Only the 0%-with-reset-near-now+5h case needs a
    second read to separate a fresh window from the drifting hypothetical.
    """
    used = read.get("used_percent")
    if isinstance(used, (int, float)) and used > 0:
        return "active"
    if read["resets_at"] <= read["read_at"]:
        return "idle"
    return None


def window_reads(command, reads, pre_wait=PRE_WAIT_SECS):
    """Settle the window state with as few reads as possible, appending to `reads`.

    Starts from what `reads` already holds (a first read, or nothing) and takes
    a second read `pre_wait` later only when one read cannot tell a fresh
    window from the drifting hypothetical reset an idle account reports (see
    quick_state). Returns "active", "idle" or "unknown"; the reads stay in the
    list so callers can keep them as evidence even if a later read raises.
    """
    if not reads:
        reads.append(read_rate_limits(command=command))
    state = quick_state(reads[0])
    if state is None:
        time.sleep(pre_wait)
        reads.append(read_rate_limits(command=command))
        state = window_state(reads)
    return state


def probe(send, command, pre_wait=PRE_WAIT_SECS, settle=SETTLE_SECS,
          post_wait=POST_WAIT_SECS, force=False):
    """One verified ping: pre-read, ping, two post-reads, verdict. No retry.

    `send` performs the ping and returns a dict with at least "returncode",
    plus whatever the ping's output revealed (see codex_runner.send_ping);
    `command` is the Codex executable or wrapper the quota reads go through.

    Outcomes: window_open (a window was already live, nothing sent), anchored,
    not_anchored, inconclusive (window live but not attributable to this ping),
    execution_error (the ping itself failed), observation_error (the ping ran
    but the quota could not be read afterwards). A failed pre-read never
    blocks the ping: anchoring is the point, verification is the bonus.
    """
    result = {"started_at": time.time(), "pre": [], "reads": [], "outcome": "inconclusive"}
    try:
        state = window_reads(command, result["pre"], pre_wait)
    except OBSERVATION_ERRORS as exc:
        state = "unknown"
        result["read_error"] = str(exc)
    result["pre_state"] = state
    if state == "active" and not force:
        result.update(outcome="window_open", finished_at=time.time())
        return result

    result["ping_start"] = time.time()
    try:
        result.update(send())
    except OSError as exc:  # the executable itself could not be run
        result["returncode"], result["error"] = None, str(exc)
    result["ping_end"] = time.time()

    try:
        time.sleep(settle)
        for i in range(2):
            if i:
                time.sleep(post_wait)
            result["reads"].append(read_rate_limits(command=command))
    except OBSERVATION_ERRORS as exc:
        result["read_error"] = str(exc)
    if len(result["reads"]) == 2:
        result["post_state"] = window_state(result["reads"])
    if result["returncode"] != 0:
        result["outcome"] = "execution_error"
    elif len(result["reads"]) < 2:
        result["outcome"] = "observation_error"
    elif result["post_state"] == "idle":
        result["outcome"] = "not_anchored"
    elif result["post_state"] == "active":
        anchor = result["reads"][-1]["resets_at"] - WINDOW_SECS
        if result["ping_start"] - DRIFT_TOLERANCE_SECS <= anchor <= result["ping_end"] + DRIFT_TOLERANCE_SECS:
            result["outcome"] = "anchored"
    result["finished_at"] = time.time()
    return result


def main() -> int:
    if __package__:
        from . import codex_runner, settings
    else:
        import codex_runner
        import settings
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--command", default="codex", help="Codex executable or wrapper")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    parser.add_argument("--thread-source", default=None)
    parser.add_argument("--wait", type=int, default=POST_WAIT_SECS, help="seconds between verdict reads")
    parser.add_argument("--force", action="store_true", help="send even if a window is open (verdict inconclusive)")
    args = parser.parse_args()
    if args.wait < 2 * DRIFT_TOLERANCE_SECS:
        parser.error(f"--wait must be at least {2 * DRIFT_TOLERANCE_SECS} seconds to distinguish drift")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = settings.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    config = {name: getattr(args, name) or codex_runner.DEFAULTS[name] for name in codex_runner.DEFAULTS}
    log_file = log_dir / f"gap-anchor-test-{stamp}.log"
    result = probe(lambda: codex_runner.send_ping(args.command, config, log_file,
                                                  log_dir / f"gap-anchor-test-{stamp}.last.txt"),
                   args.command, post_wait=args.wait, force=args.force)
    result["effective"] = {**config, "prompt": codex_runner.PROMPT}
    result_path = log_dir / f"gap-anchor-test-{stamp}.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"VERDICT: {result['outcome']}; evidence={result_path}")
    return {"anchored": 0, "window_open": 1, "not_anchored": 3}.get(result["outcome"], 2)


if __name__ == "__main__":
    sys.exit(main())
