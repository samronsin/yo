# Working on yo

For ordinary coding, review, and documentation tasks, follow the Development
section. Do not run the installation workflow merely because you are working
on the installer or another part of this repository.

## Only when installing or configuring yo

Apply this section only when the user asks to install yo, register a command,
change deployed settings or schedules, or remove an installed job. Editing or
testing the code that implements those operations is development, not a request
to change the user's installation.

Read README.md and `./install.py --help` before proceeding. Use the flags in
this checkout; do not assume features from other branches are available.

- Confirm the target machine, timezone, working hours, and commands/accounts
  with the user. Do not assume the agent's local machine is the installation target.
- Use a persistent checkout on a host that will be running at the scheduled
  times. Cron references this checkout; moving or deleting it breaks the jobs.
- Check prerequisites without sending a model request: Python 3.10+, cron,
  and the chosen CLI or executable wrapper, already authenticated by the user.
  Do not read or print authentication secrets.
- Commands must be executables on the installer's inherited PATH. Shell aliases
  and functions are not supported. Locate the command in that environment;
  if missing, correct PATH explicitly or ask about a wrapper. Do not source
  arbitrary shell startup files or change account settings automatically.
- For a custom executable, specify `--backend codex` or `--backend claude`.
  The installer adds the discovered command directory to cron's PATH; it does
  not validate executables or dependencies referenced inside a wrapper.
- Inspect existing schedules with `./install.py --status`, then preview each
  requested schedule, for example:

  ```sh
  ./install.py --tz Europe/Paris --hours 9-18 --command codex --dry-run
  ```

- Show the user the resolved executable, schedule in their timezone and system
  time, proposed cron block, and settings changes. Ask approval before changing
  the crontab or settings.
  After approval, rerun the same command without `--dry-run`, adding `--yes`
  for non-interactive execution. Install once per command.
- Verify with `./install.py --status`. Do not send a paid ping merely to test
  installation. Codex schedules use `--probe` by default to check anchoring;
  `--no-probe` schedules plain pings. Claude has no quota probe.
- Preserve unrelated cron entries and other commands' managed blocks. Use
  `./install.py --remove NAME --dry-run` before an approved removal. Never
  replace the entire crontab manually.
- Timezone conversion is fixed at installation: reinstall after a daylight-saving
  change. Settings live in `~/.yo/settings.ini` and logs in `~/.yo/logs/`.
  Omitted install options preserve saved values; review them in the preview.
  With no working hours supplied or saved, installation only registers settings,
  without creating a cron block. Removal also removes the command's settings.

## Development

- Keep changes scoped and preserve existing user edits.
- Run `python3 -m unittest` and `git diff --check` before handing off changes.
  Tests must mock crontab writes and inference calls, not modify real schedules
  or spend account quota.
- Keep examples generic. Do not publish private hostnames, paths, account
  identifiers, credentials, or logs without explicit approval.
