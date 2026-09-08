#!/usr/bin/env python3
"""Opt-in Codex config/prompt trials, each verified against the observed quota reset."""
import argparse
import fcntl
import hashlib
import html
import itertools
import json
import random
import shutil
import subprocess
import sys

if __package__:
    from .codex_anchor_probe import ROOT_DIR, probe, window_state
else:
    from codex_anchor_probe import ROOT_DIR, probe, window_state

PARAMETERS = {
    "model": ["gpt-5.6-terra", "gpt-5.6-luna"],
    "effort": ["low", "medium", "high"],
    "thread_source": ["", "scheduled"],
    "prompt": ["yo", "Calculate 17 * 23 and explain in three short sentences. Do not use tools."],
}
BACKEND = "codex"  # the only backend with a quota observer; see check_backend
SCORED = {"anchored", "not_anchored", "execution_error"}
RECOVERY = {
    "model": "gpt-5.6-sol", "effort": "high", "thread_source": "",
    "prompt": (
        "Solve this scheduling problem without tools or file changes. Tasks A through F "
        "take 3, 5, 2, 7, 4, and 6 minutes respectively. C depends on A; D depends "
        "on A and B; E depends on C; F depends on D and E. There are two identical "
        "workers; tasks cannot be interrupted and each uses one worker. Find a "
        "minimum-makespan schedule. Give task start/end times and worker assignments, "
        "prove optimality with a lower bound, and explain how the optimum changes "
        "if task B takes 9 minutes instead. Provide a self-contained explanation."
    ),
}


def check_backend(args):
    backend = getattr(args, "backend", BACKEND)
    if backend != BACKEND:
        raise ValueError(f"experiments are only supported with the {BACKEND} backend, not {backend}")
    return backend


def variants(args):
    axes = {key: [getattr(args, key)] if getattr(args, key, None) else values
            for key, values in PARAMETERS.items()}
    return [dict(zip(axes, values)) for values in itertools.product(*axes.values())]


def statistics(grid, trials):
    # Counts guide coverage only, not automatic promotion.
    return [{"variant": variant, "trials": sum(
        t["variant"] == variant and t["outcome"] in SCORED for t in trials)}
        for variant in grid]


def choose(stats):
    least = min(s["trials"] for s in stats)
    return random.choice([s for s in stats if s["trials"] == least])


def report(data):
    """An allowlisted, portable report; never publish raw logs or account paths."""
    trials = data["trials"]
    difference = lambda end, start: round(end - start, 3) if end is not None and start is not None else None
    exported = []
    for trial in trials:
        item = {"variant": {k: trial["variant"].get(k) for k in PARAMETERS},
                "backend": trial.get("settings", {}).get("backend", "codex"),
                "outcome": trial["outcome"], "returncode": trial.get("returncode"),
                "cli_version": trial.get("settings", {}).get("version")}
        if trial["outcome"] == "active_window" or trial.get("reason") == "window_already_open":
            item.update(outcome="no_data", reason="window_already_open")
        start = trial.get("ping_start")
        item["duration_seconds"] = difference(trial.get("ping_end"), start)
        reads = trial.get("reads", [])
        item["verification"] = ({
            "elapsed_seconds": difference(reads[-1]["read_at"], reads[0]["read_at"]),
            "reset_drift_seconds": [difference(r["resets_at"], reads[0]["resets_at"]) for r in reads],
        } if reads else None)
        exported.append(item)
    payload = {"report_schema": 3, "traffic_complete": False, "token_usage_recorded": False,
               "trials": exported}
    report_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    skipped = sum(t["outcome"] == "no_data" for t in exported)
    no_data = f"No data: {skipped} run(s) skipped because a window was already open.\n\n" if skipped else ""
    def cell(value):
        return "<code>" + html.escape(str(value)).replace("|", "&#124;").replace("\n", "<br>") + "</code>"

    lines = ["## yo experiments", "", f"{len(trials)} trials.", ""]
    if exported:
        lines += ["| # | Config | Prompt | Outcome |", "| --- | --- | --- | --- |"]
    for index, trial in enumerate(exported):
        v = trial["variant"]
        config = "/".join(str(part) for part in (trial["backend"], v["model"], v["effort"],
                                                v["thread_source"] or "default") if part)
        outcome = "no data: window already open" if trial.get("reason") == "window_already_open" else trial["outcome"]
        lines.append(f"| {index + 1} | {cell(config)} | {cell(v['prompt'])} | {cell(outcome)} |")
    lines.append("")
    lines += ["", no_data.rstrip(), "Outside traffic and token counts unknown.", "",
              "<details>", "<summary>Evidence JSON</summary>", "", f"Report ID: `{report_id}`", "",
              "```json", json.dumps(payload, indent=2), "```", "", "</details>", ""]
    return "\n".join(lines)


def run(args):
    backend = check_backend(args)
    command_key = hashlib.sha256(args.command.encode()).hexdigest()[:16]
    folder = ROOT_DIR / "logs" / "experiments" / command_key
    path = folder / "history.json"
    if getattr(args, "report", False):
        # Atomic history replacement makes an unlocked snapshot safe, even during a trial.
        print(report(json.loads(path.read_text()) if path.exists() else {"trials": []}), end="")
        return 0
    grid = variants(args)
    version = subprocess.run([args.command, "--version"], capture_output=True,
                             text=True, timeout=30, check=True).stdout.strip()
    settings = {"schema": 5, "command": args.command, "backend": backend,
                "executable": shutil.which(args.command), "version": version, "grid": grid}
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("experiment: another trial is running; skipped")
            return 0
        data = json.loads(path.read_text()) if path.exists() else {"settings": settings, "trials": []}
        stats = statistics(grid, [t for t in data["trials"] if t.get("settings") == settings])
        selected = choose(stats)
        if args.status:
            print(json.dumps({"path": str(path), "automatic_selection": False,
                              "next": selected["variant"], "results": stats}, indent=2))
            return 0
        result = probe(selected["variant"], args.command, folder / "probe.log")
        result["settings"] = settings
        data["settings"] = settings
        data["trials"].append(result)
        data.pop("sequences", None)  # Drop the obsolete derived cache, not raw trials.
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n")
        temp.replace(path)
        print(f"experiment: {result['outcome']}; evidence={path}")
        # Persist the experimental verdict before spending any recovery traffic.
        reads = result.get("reads", [])
        if (result["outcome"] in {"not_anchored", "execution_error"}
                and len(reads) >= 2 and window_state(reads) == "idle"):
            result = probe(RECOVERY, args.command, folder / "recovery.log")
            print(f"recovery: {result['outcome']}; evidence={folder / 'recovery.log'}")
        return {"anchored": 0, "no_data": 0, "not_anchored": 3}.get(result["outcome"], 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    parser.add_argument("--backend", default=BACKEND, help=f"only {BACKEND} is supported")
    for name in ("model", "effort", "thread-source", "prompt"):
        parser.add_argument(f"--{name}", default="", help="pin this axis instead of exploring it")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--status", action="store_true")
    view.add_argument("--report", action="store_true", help="print an issue-ready Markdown report offline")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"experiment error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
