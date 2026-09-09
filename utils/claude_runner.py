"""yo's Claude backend: send a plain ping, keeping its output and status in the run log."""
import subprocess

if __package__:
    from . import codex_runner as shared
else:
    import codex_runner as shared


def run(args):
    """args are yo's parsed arguments (command, model); Claude has no quota observer, so no --probe."""
    model = args.model or "haiku"
    log_file, last_message_file = shared.open_run_log(args.command)
    with log_file.open("a") as log:
        log.write(f"agent={args.command} backend=claude model={model}\n")
        log.flush()
        try:
            # Claude's stdout is the last message; stderr goes directly to the run log.
            with last_message_file.open("w") as last:
                proc = subprocess.run([
                    args.command, "--print", "--model", model,
                    "--output-format", "text", "--permission-mode", "default", "yo",
                ], stdout=last, stderr=log, cwd=shared.ROOT_DIR)
            rc = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
            log.write(last_message_file.read_text(errors="replace"))
        except OSError as exc:
            # Whatever failed (the executable, the last-message file), the run
            # log still gets the error and its terminator, like the shell did.
            log.write(f"{args.command}: {exc}\n")
            if exc.filename == str(last_message_file):
                rc = 1
            else:
                rc = 127 if isinstance(exc, FileNotFoundError) else 126
        log.write(f"[{shared.stamp()}] yo end rc={rc}\n")
    return rc
