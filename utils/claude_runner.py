"""Send a plain Claude ping, keeping its output and status in the run log."""
import datetime
from pathlib import Path
import subprocess

ROOT_DIR = Path(__file__).resolve().parent.parent


def run(args):
    model = args.model or "haiku"
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = log_dir / f"yo-{args.command}-{timestamp}.log"
    last_message_file = log_dir / f"yo-{args.command}.last.txt"
    with log_file.open("a") as log:
        log.write(f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] yo start\n")
        log.write(f"root={ROOT_DIR}\nagent={args.command} backend=claude model={model}\n")
        log.flush()
        # Claude's stdout is the last message; stderr goes directly to the run log.
        with last_message_file.open("w") as last:
            try:
                proc = subprocess.run([
                    args.command, "--print", "--model", model,
                    "--output-format", "text", "--permission-mode", "default", "yo",
                ], stdout=last, stderr=log, cwd=ROOT_DIR)
                rc = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
            except OSError as exc:
                log.write(f"{args.command}: {exc}\n")
                rc = 127 if isinstance(exc, FileNotFoundError) else 126
        log.write(last_message_file.read_text(errors="replace"))
        log.write(f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] yo end rc={rc}\n")
    return rc
