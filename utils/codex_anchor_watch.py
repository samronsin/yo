#!/usr/bin/env python3
"""Verify that the 04:30Z codex cron ping anchored the 5h usage window.

Waits for the next 04:30Z cron firing (or checks immediately if one fired
within the last 5h, or with --now), then reads account rate limits twice via
`codex app-server` (spends no tokens) and reports:

  ANCHORED BY CRON  resetsAt locked at cron time + 5h        exit 0
  ANCHORED BY OTHER resetsAt locked, but anchor != cron time exit 2
  NOT ANCHORED      resetsAt drifts with query time          exit 1

Also inspects the cron's own log for the silent-failure mode where the ping
dies before reaching the API (e.g. a flag the installed codex CLI rejects).

Typical use the evening before:  nohup python3 utils/codex_anchor_watch.py >/tmp/anchor-check.log 2>&1 &
"""

import argparse
import datetime
import glob
import re
import sys
import time
from pathlib import Path

from codex_anchor_probe import DRIFT_TOLERANCE_SECS, WINDOW_SECS, read_rate_limits, utc

ROOT_DIR = Path(__file__).resolve().parent.parent  # repo root
ATTRIBUTION_TOLERANCE_SECS = 120  # cron starts at :30:01; request lands seconds later
GRACE_SECS = 90  # after the firing, give the ping time to complete before reading
SAMPLE_END_MARGIN_SECS = 60  # slack for read round-trips near window expiry


def parse_hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or not (0 <= int(m[1]) < 24 and 0 <= int(m[2]) < 60):
        raise argparse.ArgumentTypeError(f"expected HH:MM (UTC), got {s!r}")
    return int(m[1]), int(m[2])


def cron_log_status(cron_dt: datetime.datetime) -> str:
    """Summarize the cron's own log to catch pings that died client-side."""
    pattern = str(ROOT_DIR / "logs" / f"yo-codex-{cron_dt:%Y%m%d}T{cron_dt:%H%M}*.log")
    logs = sorted(glob.glob(pattern))
    if not logs:
        return f"WARNING: no cron log matching {Path(pattern).name} — did cron run?"
    text = Path(logs[-1]).read_text(errors="replace")
    rc = re.search(r"yo end rc=(\d+)", text)
    config = re.search(r"agent=codex.*", text)
    parts = [Path(logs[-1]).name]
    if config:
        parts.append(config.group(0))
    if rc is None:
        parts.append("WARNING: no 'yo end rc=' line — ping still running or killed")
    elif rc.group(1) != "0":
        parts.append(f"WARNING: ping exited rc={rc.group(1)} — it likely never "
                     "reached the API (check for a flag/version error in the log)")
    else:
        parts.append("rc=0")
    return "; ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cron-time", type=parse_hhmm, default=(4, 30),
                        metavar="HH:MM", help="cron firing time, UTC (default 04:30)")
    parser.add_argument("--wait", type=int, default=120,
                        help="seconds between the two verdict reads")
    parser.add_argument("--now", action="store_true",
                        help="skip waiting; judge against the most recent cron time")
    args = parser.parse_args()

    def say(msg: str) -> None:
        print(f"[{utc(time.time())}] {msg}", flush=True)

    hour, minute = args.cron_time
    now = datetime.datetime.now(datetime.timezone.utc)
    cron_dt = now.replace(hour=hour, minute=minute, second=1, microsecond=0)
    if cron_dt > now:
        cron_dt -= datetime.timedelta(days=1)
    # A firing is only judgeable inside its usable sampling window: from
    # GRACE_SECS after the firing (the ping must have finished, or read #1
    # races it) until early enough that BOTH reads complete before the
    # would-be window expires at cron+5h (past that, an anchored window
    # expires between reads and the drift check yields a false NOT ANCHORED).
    def sampling_window(dt: datetime.datetime) -> tuple[float, float]:
        start = dt.timestamp() + GRACE_SECS
        end = dt.timestamp() + WINDOW_SECS - args.wait - SAMPLE_END_MARGIN_SECS
        return start, end

    sample_start, sample_end = sampling_window(cron_dt)
    if time.time() > sample_end:
        if args.now:
            say("WARNING: past the usable sampling window for this firing "
                f"(ended {utc(sample_end)}); its 5h window expires before or "
                "while we read, so the verdict may be a false NOT ANCHORED.")
        else:
            cron_dt += datetime.timedelta(days=1)
            sample_start, _ = sampling_window(cron_dt)
            say(f"last firing no longer judgeable; waiting for the "
                f"{cron_dt:%Y-%m-%d %H:%M}Z cron...")
    if time.time() < sample_start:
        say(f"sleeping until {utc(sample_start)} (ping settled)...")
        time.sleep(max(0, sample_start - time.time()))

    cron_ts = cron_dt.timestamp()
    say(f"judging cron firing at {utc(cron_ts)}")
    say(f"cron log: {cron_log_status(cron_dt)}")

    reads = [read_rate_limits()]
    say(f"read #1: resetsAt={utc(reads[0]['resets_at'])} used%={reads[0]['used_percent']}")
    time.sleep(args.wait)
    reads.append(read_rate_limits())
    say(f"read #2: resetsAt={utc(reads[1]['resets_at'])} used%={reads[1]['used_percent']}")

    drift = reads[1]["resets_at"] - reads[0]["resets_at"]
    locked = abs(drift) <= DRIFT_TOLERANCE_SECS
    anchor_ts = reads[1]["resets_at"] - WINDOW_SECS
    say(f"drift over {reads[1]['read_at'] - reads[0]['read_at']:.0f}s: {drift:+d}s; "
        f"implied anchor {utc(anchor_ts)} (cron {anchor_ts - cron_ts:+.0f}s)")

    if not locked:
        say("VERDICT: NOT ANCHORED — resetsAt drifts; the cron ping did not "
            "anchor (and nothing else has since).")
        return 1
    if abs(anchor_ts - cron_ts) <= ATTRIBUTION_TOLERANCE_SECS:
        say("VERDICT: ANCHORED BY CRON — the cron ping locked the window; "
            "config confirmed end-to-end.")
        return 0
    say("VERDICT: ANCHORED, BUT NOT BY CRON — something else anchored at "
        f"{utc(anchor_ts)}; the cron ping's own effect is unproven (it joined "
        "an already-open window, or its window came and went). Re-check after "
        "a cron firing that lands in a clean gap.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
