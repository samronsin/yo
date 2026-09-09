#!/usr/bin/env python3
"""Codex 5h-window observer: did this ping anchor a window?

Every `yo <codex command>` run goes through probe(): a token-free rateLimits
read before the ping (a second one 15s later only when the first is
ambiguous), the production ping via `./yo ... --no-record`, then after a 10s
settle two reads 60s apart. A real anchor locks resetsAt at ping+5h; without one resetsAt is a
hypothetical that drifts with query time. Never judge anchoring from the Codex
web UI (it hides windows at 0% usage).

Run directly for a one-off trial outside the recorded history (see main):
change ONE variable versus a known result, and remember an ANCHORED verdict
closes the gap for ~5h.
"""

import argparse
import datetime
import glob
import json
import math
import os
import re
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
DEFAULT_PROMPT = "yo"  # mirrors yo's PROMPT default; the recorder needs the effective value
OBSERVATION_ERRORS = (OSError, RuntimeError, ValueError, KeyError)


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
                    window = five_hour_window((msg.get("result") or {}).get("rateLimits") or {})
                    reset = window.get("resetsAt")
                    if (isinstance(reset, bool) or not isinstance(reset, (int, float))
                            or not math.isfinite(reset)):
                        raise RuntimeError("missing or invalid quota reset timestamp")
                    return {"read_at": time.time(), "resets_at": reset,
                            "used_percent": window.get("usedPercent")}
    finally:
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


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


def run_ping(variant, command, log_file):
    """Send one production ping through ./yo, writing its log to log_file.

    Goes through ./yo so the codex invocation under test is exactly the cron
    one; --no-record keeps yo from recursing back into the recorder. Only
    explicitly requested overrides are passed, so a default probe tests yo's
    own defaults instead of re-stating (and eventually shadowing) them here.
    Returns (returncode, stderr).
    """
    cmd = [str(ROOT_DIR / "yo"), command, "--backend", "codex", "--no-record"]
    for flag, key in (("--model", "model"), ("--effort", "effort"),
                      ("--thread-source", "thread_source"), ("--prompt", "prompt")):
        if variant.get(key):
            cmd += [flag, variant[key]]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          cwd=ROOT_DIR, env={**os.environ, "YO_LOG_FILE": str(log_file)})
    return proc.returncode, proc.stderr.strip()


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
    if (read.get("used_percent") or 0) > 0:
        return "active"
    if read["resets_at"] <= read["read_at"]:
        return "idle"
    return None


def ping_details(log_file, variant):
    """What the ping actually ran, parsed from yo's own log of it."""
    details = {"effective": {key: variant.get(key) or "" for key in ("model", "effort", "thread_source")}}
    details["effective"]["prompt"] = variant.get("prompt") or DEFAULT_PROMPT
    try:
        text = Path(log_file).read_text(errors="replace")
    except OSError:
        return details
    configs = re.findall(r"agent=\S+ backend=codex model=(\S+) reasoning_effort=(\S+) thread_source=(\S+)", text)
    if configs:
        model, effort, source = configs[-1]
        details["effective"].update(model=model, effort=effort,
                                    thread_source="" if source.startswith("user(") else source)
    version = re.search(r"OpenAI Codex v(\S+)", text)
    if version:
        details["cli_version"] = version.group(1)
    session = re.search(r"^session id: (\S+)", text, re.MULTILINE)
    if session:
        details["session_id"] = session.group(1)
        details["usage"] = session_usage(session.group(1))
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
    the recorder's CODEX_HOME when set and otherwise under every ~/.codex*.
    """
    roots = [os.environ["CODEX_HOME"]] if os.environ.get("CODEX_HOME") else glob.glob(os.path.expanduser("~/.codex*"))
    for root in roots:
        for path in glob.glob(os.path.join(root, "sessions", "**", f"rollout-*-{session_id}.jsonl"), recursive=True):
            usage = None
            try:
                for line in open(path, errors="replace"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        usage = info.get("total_token_usage") or info.get("last_token_usage") or usage
            except OSError:
                continue
            if usage:
                return {k: usage.get(k) for k in ("input_tokens", "cached_input_tokens",
                                                  "output_tokens", "reasoning_output_tokens", "total_tokens")}
    return None


def probe(variant, command, log_file, pre_wait=PRE_WAIT_SECS, settle=SETTLE_SECS,
          post_wait=POST_WAIT_SECS, force=False):
    """One verified ping: pre-read, ping, two post-reads, verdict. No retry.

    Outcomes: window_open (a window was already live, nothing sent), anchored,
    not_anchored, inconclusive (window live but not attributable to this ping),
    execution_error (the ping itself failed), observation_error (the ping ran
    but the quota could not be read afterwards). A failed pre-read never
    blocks the ping: anchoring is the point, verification is the bonus.
    """
    result = {"variant": variant, "started_at": time.time(), "pre": [], "reads": [],
              "outcome": "inconclusive"}
    try:
        result["pre"].append(read_rate_limits(command=command))
        state = quick_state(result["pre"][0])
        if state is None:
            time.sleep(pre_wait)
            result["pre"].append(read_rate_limits(command=command))
            state = window_state(result["pre"])
    except OBSERVATION_ERRORS as exc:
        state = "unknown"
        result["read_error"] = str(exc)
    result["pre_state"] = state
    if state == "active" and not force:
        result.update(outcome="window_open", finished_at=time.time())
        return result

    result["ping_start"] = time.time()
    try:
        result["returncode"], stderr = run_ping(variant, command, log_file)
        if result["returncode"] != 0 and stderr:
            result["error"] = stderr
    except OSError as exc:
        result["returncode"], result["error"] = None, str(exc)
    result["ping_end"] = time.time()
    result["log"] = str(log_file)
    result.update(ping_details(log_file, variant))

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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--command", default="codex", help="Codex executable or wrapper")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    parser.add_argument("--thread-source", default=None)
    parser.add_argument("--wait", type=int, default=POST_WAIT_SECS, help="seconds between verdict reads")
    parser.add_argument("--force", action="store_true", help="send even if a window is open (verdict inconclusive)")
    args = parser.parse_args()
    if args.wait < 2 * DRIFT_TOLERANCE_SECS:
        parser.error(f"--wait must be at least {2 * DRIFT_TOLERANCE_SECS} seconds to distinguish drift")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    variant = {name: getattr(args, name) or "" for name in ("model", "effort", "thread_source", "prompt")}
    result = probe(variant, args.command, log_dir / f"gap-anchor-test-{stamp}.log",
                   post_wait=args.wait, force=args.force)
    result_path = log_dir / f"gap-anchor-test-{stamp}.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"VERDICT: {result['outcome']}; evidence={result_path}")
    return {"anchored": 0, "window_open": 1, "not_anchored": 3}.get(result["outcome"], 2)


if __name__ == "__main__":
    sys.exit(main())
