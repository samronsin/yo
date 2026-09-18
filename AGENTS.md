# Working on yo

## Installing or configuring yo

Use this workflow only when asked to change an actual installation, settings,
or schedule, not when developing the code that implements those operations.

1. Read the README's [usage](README.md#usage), [settings](README.md#settings),
   [custom commands](README.md#custom-commands), [requirements](README.md#requirements),
   and [timezones](README.md#timezones), plus `./install.py --help`.
2. Confirm the target machine, commands/accounts, timezone, and working hours.
   Cron runs `yo` from the checkout that installed it: install from this one, or
   from a persistent clone on the target host. Check prerequisites without
   sending inference requests or exposing credentials; the Quickstart's `./yo`
   smoke test is optional and spends a paid ping.
3. Inspect `./install.py --status`. Build a command with the approved settings
   explicitly supplied, rather than relying on saved values, and run it with
   `--dry-run`. Show the proposed additions/replacements/removals and settings
   changes; obtain approval before applying them.
4. Rerun the approved command without `--dry-run`, adding `--yes`. Preview and
   apply are separate invocations: if values cannot be pinned or inputs have
   changed, stop and obtain a fresh approval rather than assuming they are bound.
5. Compare the apply output and `./install.py --status` with the approved preview.
   Report discrepancies; this verification detects changes but cannot prevent
   them. Do not send a paid ping merely to test installation.

## Development

- Keep changes scoped and preserve existing user edits.
- Run `python3 -m unittest` and `git diff --check` before handing off changes.
  Tests must mock crontab writes and inference calls, not modify real schedules
  or spend account quota.
- Keep examples generic. Do not publish private hostnames, paths, account
  identifiers, credentials, or logs without explicit approval.
