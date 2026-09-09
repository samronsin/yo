#!/usr/bin/env python3
"""Unit tests for install helpers."""
import argparse
import io
import os
import subprocess
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest import mock

import install
from install import (
    BASE_CRON_PATH,
    MalformedCrontab,
    command_name,
    cron_path_for,
    format_status,
    hour_minute,
    installed_jobs,
    parse_cron_entry,
    render_cron,
    remove_managed_block,
    resolve_backend,
    to_system_times,
)


@contextmanager
def system_tz(tz):
    """Temporarily set the process's local timezone (what cron schedules against)."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


class HourMinuteTest(unittest.TestCase):
    def test_whole_hour(self):
        self.assertEqual(hour_minute(6), (6, 0))

    def test_midnight(self):
        self.assertEqual(hour_minute(0), (0, 0))

    def test_half_hour(self):
        self.assertEqual(hour_minute(9.5), (9, 30))

    def test_rounds_to_nearest_minute(self):
        # 11.033333h = 662.0 min -> 11:02; 16.066667h = 964.0 min -> 16:04
        self.assertEqual(hour_minute(11 + 2 / 60), (11, 2))
        self.assertEqual(hour_minute(16 + 4 / 60), (16, 4))

    def test_rounding_carries_into_next_hour(self):
        # 9.999h rounds up to 600 min -> 10:00
        self.assertEqual(hour_minute(9.999), (10, 0))

    def test_wraps_past_24(self):
        self.assertEqual(hour_minute(24), (0, 0))
        self.assertEqual(hour_minute(25), (1, 0))
        self.assertEqual(hour_minute(28.5), (4, 30))  # overnight window past midnight

    def test_wraps_before_midnight(self):
        # warm-up time before 0 wraps onto the previous evening
        self.assertEqual(hour_minute(-1), (23, 0))
        self.assertEqual(hour_minute(-0.5), (23, 30))

    def test_minute_never_reaches_60(self):
        # any input lands in valid 0-23 / 0-59 ranges
        t = -100.0
        while t <= 100.0:
            hour, minute = hour_minute(t)
            self.assertIn(hour, range(24))
            self.assertIn(minute, range(60))
            t += 0.123


class CronPathForTest(unittest.TestCase):
    def _which(self, mapping):
        """Stand-in for shutil.which backed by a name -> path mapping."""
        return lambda name, **kwargs: mapping.get(name)

    def test_base_dir_agent_not_duplicated(self):
        # an agent already in a BASE_CRON_PATH dir leaves the path unchanged
        base_dir = BASE_CRON_PATH.split(":")[0]
        with mock.patch("install.shutil.which", self._which({"codex": f"{base_dir}/codex"})):
            self.assertEqual(cron_path_for(["codex"]), BASE_CRON_PATH)

    def test_non_base_dir_prepended(self):
        with mock.patch("install.shutil.which", self._which({"codex": "/opt/foo/bin/codex"})):
            self.assertEqual(cron_path_for(["codex"]), "/opt/foo/bin:" + BASE_CRON_PATH)

    def test_multiple_agents_deduped(self):
        base_dir = BASE_CRON_PATH.split(":")[0]
        mapping = {"codex": "/opt/foo/bin/codex", "claude": f"{base_dir}/claude"}
        with mock.patch("install.shutil.which", self._which(mapping)):
            # codex's dir is prepended once; claude's (a base dir) isn't duplicated
            self.assertEqual(cron_path_for(["codex", "claude"]), "/opt/foo/bin:" + BASE_CRON_PATH)

    def test_missing_agent_exits(self):
        with mock.patch("install.shutil.which", self._which({})):
            with self.assertRaises(SystemExit) as cm:
                cron_path_for(["claude"])
        self.assertIn("executable on PATH", str(cm.exception))
        self.assertIn("not shell aliases", str(cm.exception))


class CommandNameTest(unittest.TestCase):
    def test_accepts_backend_and_custom_names(self):
        for name in ("codex", "claude", "work-ai", "perso", "codex-pro", "a.b_c"):
            self.assertEqual(command_name(name), name)

    def test_empty_name_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            command_name("")

    def test_unsafe_names_rejected(self):
        # Names that would split or be interpreted by cron's shell, or read as a
        # flag/dotfile, are refused before they reach the crontab.
        for bad in ("my agent", "a;rm -rf", "a|b", "$(id)", "a/b", "-flag", ".hidden"):
            with self.assertRaises(argparse.ArgumentTypeError):
                command_name(bad)


class ResolveBackendTest(unittest.TestCase):
    def test_bare_backend_infers_itself(self):
        self.assertEqual(resolve_backend("codex", None), "codex")
        self.assertEqual(resolve_backend("claude", None), "claude")

    def test_custom_command_takes_backend(self):
        self.assertEqual(resolve_backend("work-ai", "codex"), "codex")

    def test_bare_backend_with_matching_flag_ok(self):
        self.assertEqual(resolve_backend("codex", "codex"), "codex")

    def test_bare_backend_with_conflicting_flag_exits(self):
        with self.assertRaises(SystemExit):
            resolve_backend("codex", "claude")

    def test_custom_command_without_backend_exits(self):
        with self.assertRaises(SystemExit):
            resolve_backend("work-ai", None)


class ParseArgsTest(unittest.TestCase):
    def test_no_probe_is_an_install_option(self):
        self.assertFalse(self._install_parse("--command", "codex").no_probe)
        self.assertTrue(self._install_parse("--command", "codex", "--no-probe").no_probe)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._parse("--status", "--no-probe")

    def _parse(self, *args):
        return install.parse_args([*args])

    def _install_parse(self, *args):
        return self._parse("--tz", "Europe/Paris", "--hours", "9-18", *args)

    def test_command_flag_parsed(self):
        args = self._install_parse("--command", "codex-pro", "--backend", "codex")
        self.assertEqual(args.command, "codex-pro")
        self.assertEqual(args.backend, "codex")

    def test_status_does_not_require_install_args(self):
        args = self._parse("--status")
        self.assertTrue(args.status)
        self.assertIsNone(args.command)

    def test_remove_does_not_require_install_args(self):
        args = self._parse("--remove", "codex-pro")
        self.assertEqual(args.remove, "codex-pro")
        self.assertFalse(args.status)
        self.assertIsNone(args.command)

    def _usage_error(self, *argv):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                self._parse(*argv)
        self.assertEqual(cm.exception.code, 2)
        return stderr.getvalue()

    def test_install_args_required(self):
        # All missing flags are reported at once, and only the missing ones.
        for argv, expected in (((), "--tz, --hours, --command"),
                               (("--tz", "Europe/Paris", "--hours", "9-18"), "--command")):
            with self.subTest(argv=argv):
                self.assertIn(f"required: {expected}\n", self._usage_error(*argv))

    def test_remove_name_validated_like_command(self):
        # The name is matched against the crontab markers, so it's held to the
        # same charset as --command (a stray shell-ish name can't match anyway).
        self.assertIn("must start with a letter or digit", self._usage_error("--remove", "a;b"))

    def test_modes_are_exclusive(self):
        self.assertIn("not allowed with", self._usage_error("--status", "--remove", "codex"))

    def test_modes_reject_schedule_args(self):
        # Mixing a schedule with --status/--remove is almost certainly a mistake
        # (e.g. thinking --remove edits an install), so refuse rather than ignore.
        self.assertIn("--remove cannot be combined with --command, --backend\n",
                      self._usage_error("--remove", "codex", "--command", "codex", "--backend", "codex"))
        self.assertIn("--status cannot be combined with --tz\n",
                      self._usage_error("--status", "--tz", "Europe/Paris"))


class ManagedBlocksTest(unittest.TestCase):
    CRONTAB = (
        "MAILTO=me@example.com\n"
        + render_cron([6, 11 + 2 / 60], "Europe/Paris", "codex", "codex", "/bin")
        + "15 9 * * * echo unmanaged\n"
        + render_cron([16 + 4 / 60], "Europe/Paris", "codex-pro", "codex", "/opt/bin:/bin")
    )

    def test_installed_jobs_round_trips_rendered_blocks(self):
        # The PATH= and comment lines inside each block aren't entries.
        self.assertEqual(installed_jobs(self.CRONTAB), [
            ("codex", [("0 6 * * *", f"{install.JOB_CMD} codex --probe"),
                       ("2 11 * * *", f"{install.JOB_CMD} codex --probe")]),
            ("codex-pro", [("4 16 * * *", f"{install.JOB_CMD} codex-pro --backend codex --probe")]),
        ])

    def test_remove_managed_block_keeps_everything_else(self):
        kept = remove_managed_block(self.CRONTAB, "codex")
        self.assertNotIn("# >>> yo-codex >>>", kept)
        self.assertNotIn(f"2 11 * * * {install.JOB_CMD} codex", kept)
        for line in ("MAILTO=me@example.com", "15 9 * * * echo unmanaged",
                     "# >>> yo-codex-pro >>>", "# <<< yo-codex-pro <<<"):
            self.assertIn(line, kept)
        self.assertEqual(remove_managed_block("", "codex"), [])

    def test_malformed_blocks_are_refused(self):
        # Guessing where a broken block ends could drop the user's own lines, so
        # both status and install refuse instead.
        block = "# >>> yo-codex >>>\n0 6 * * * /repo/yo codex\n"
        for name, crontab in (
            ("unterminated", block + "15 9 * * * echo mine\n"),
            ("nested begin", block + "# >>> yo-codex-pro >>>\n# <<< yo-codex-pro <<<\n# <<< yo-codex <<<\n"),
            ("other command's end", block + "# <<< yo-other <<<\n"),
        ):
            with self.subTest(name):
                with self.assertRaises(MalformedCrontab):
                    installed_jobs(crontab)
                with self.assertRaises(MalformedCrontab):
                    remove_managed_block(crontab, "codex")

    def test_markers_must_match_exactly(self):
        # An indented marker is just a comment to install.py (as it is to cron's
        # eyes, which still runs the lines), so the block isn't managed.
        crontab = " # >>> yo-codex >>>\n0 6 * * * /repo/yo codex\n # <<< yo-codex <<<\n"
        self.assertEqual(installed_jobs(crontab), [])
        self.assertEqual(len(remove_managed_block(crontab, "codex")), 3)

    def test_parse_cron_entry(self):
        self.assertEqual(parse_cron_entry("0 6 * * * /repo/yo codex  \t"),
                         ("0 6 * * *", "/repo/yo codex"))
        self.assertEqual(parse_cron_entry("*/15 9-17 * * mon-fri /repo/yo codex --backend codex"),
                         ("*/15 9-17 * * mon-fri", "/repo/yo codex --backend codex"))
        for other in ("", "   ", "# a comment with several words in it",
                      "PATH=/usr/local/bin:/usr/bin:/bin a b c d e",
                      "MAILTO = someone with a long address here",
                      "@daily /repo/yo codex a b c d"):
            self.assertIsNone(parse_cron_entry(other), other)


class StatusTest(unittest.TestCase):
    def test_format_status(self):
        # Logs are located from the installed yo's own path, not this checkout.
        crontab = ("# >>> yo-codex-pro >>>\n"
                   "0 6 * * * /moved/yo codex-pro --backend codex\n"
                   "*/15 9-17 * * 1-5 /moved/yo codex-pro --backend codex\n"
                   "# <<< yo-codex-pro <<<\n")
        self.assertEqual(format_status(installed_jobs(crontab)), (
            "Installed yo cron jobs:\n"
            "\n"
            "codex-pro\n"
            "  schedule: 06:00, */15 9-17 * * 1-5 system time\n"
            "  command:  /moved/yo codex-pro --backend codex\n"
            "  logs:     /moved/logs/yo-codex-pro-<timestamp>.log\n"
            "  last:     /moved/logs/yo-codex-pro.last.txt\n"
        ))

    def test_format_status_block_without_entries(self):
        text = format_status(installed_jobs("# >>> yo-codex >>>\n# <<< yo-codex <<<\n"))
        self.assertIn("  schedule: (no cron entries)\n", text)
        self.assertIn(f"  logs:     {install.ROOT_DIR}/logs/yo-codex-<timestamp>.log\n", text)

    def test_format_status_when_empty(self):
        self.assertEqual(format_status([]), "No yo cron jobs installed.\n")

    def test_status_reads_crontab(self):
        crontab = render_cron([6], "Europe/Paris", "codex", "codex", "/bin")
        completed = subprocess.CompletedProcess(["crontab", "-l"], 0, crontab, "")
        with mock.patch("install.shutil.which", return_value="/usr/bin/crontab"):
            with mock.patch("install.subprocess.run", return_value=completed):
                out = io.StringIO()
                with redirect_stdout(out):
                    install.main(install.parse_args(["--status"]))
        self.assertIn("\ncodex\n  schedule: 06:00 system time\n", out.getvalue())

    def test_install_rejects_malformed_block_before_prompt(self):
        args = install.parse_args(["--tz", "Europe/Paris", "--hours", "9-18", "--command", "codex"])
        with mock.patch("install.shutil.which", return_value="/usr/bin/codex"):
            with mock.patch("install.read_crontab", return_value="# >>> yo-codex >>>\n"):
                with mock.patch("builtins.input") as input_mock:
                    with self.assertRaises(SystemExit) as cm:
                        install.main(args)
        self.assertIn("missing its end marker", str(cm.exception))
        input_mock.assert_not_called()

    def test_install_merges_into_crontab_as_of_confirmation(self):
        # The user may edit the crontab while reviewing the prompt; the merge
        # must build on what's there after they confirm, not the preflight read.
        args = install.parse_args(["--tz", "Europe/Paris", "--hours", "9-18", "--command", "codex"])
        before, after = "15 9 * * * echo before\n", "15 9 * * * echo after\n"
        with mock.patch("install.shutil.which", return_value="/usr/bin/codex"):
            with mock.patch("install.read_crontab", side_effect=[before, after]):
                with mock.patch("builtins.input", return_value="y"):
                    with mock.patch("install.subprocess.run") as run_mock:
                        with redirect_stdout(io.StringIO()):
                            install.main(args)
        merged = run_mock.call_args.kwargs["input"]
        self.assertTrue(merged.startswith(after))
        self.assertNotIn("before", merged)
        self.assertIn("# >>> yo-codex >>>", merged)


class RemoveTest(unittest.TestCase):
    CRONTAB = ManagedBlocksTest.CRONTAB

    def _remove(self, command, crontab, *flags, reply="y"):
        """Run `--remove command` against `crontab`; returns (stdout, crontab written or None)."""
        args = install.parse_args(["--remove", command, *flags])
        reads = crontab if isinstance(crontab, list) else [crontab, crontab]
        out = io.StringIO()
        with mock.patch("install.read_crontab", side_effect=reads):
            with mock.patch("builtins.input", return_value=reply) as self.input_mock:
                with mock.patch("install.subprocess.run") as run_mock:
                    with redirect_stdout(out):
                        install.main(args)
        written = run_mock.call_args.kwargs["input"] if run_mock.called else None
        return out.getvalue(), written

    def test_removes_only_the_selected_block(self):
        out, written = self._remove("codex", self.CRONTAB)
        # The block is shown for review before the prompt...
        self.assertIn("Cron block to remove (codex):\n\n# >>> yo-codex >>>\n", out)
        self.assertIn(f"2 11 * * * {install.JOB_CMD} codex --probe\n# <<< yo-codex <<<\n", out)
        self.assertIn("Crontab updated; yo-codex block removed.\n", out)
        # ...and only it is dropped: the user's lines and the other block survive.
        self.assertNotIn("yo-codex >>>", written)
        self.assertNotIn(f"{install.JOB_CMD} codex\n", written)
        self.assertEqual(written.count("yo-codex-pro"), 2)
        self.assertIn("MAILTO=me@example.com\n", written)
        self.assertIn("15 9 * * * echo unmanaged\n", written)
        self.assertTrue(written.endswith("# <<< yo-codex-pro <<<\n"))

    def test_removing_last_block_leaves_empty_crontab(self):
        crontab = render_cron([6], "Europe/Paris", "codex", "codex", "/bin")
        _, written = self._remove("codex", crontab)
        self.assertEqual(written, "")

    def test_yes_skips_prompt(self):
        _, written = self._remove("codex-pro", self.CRONTAB, "--yes")
        self.input_mock.assert_not_called()
        self.assertIsNotNone(written)

    def test_decline_changes_nothing(self):
        with self.assertRaises(SystemExit) as cm:
            self._remove("codex", self.CRONTAB, reply="n")
        self.assertEqual(cm.exception.code, "Aborted; nothing changed.")

    def test_missing_block_is_an_error_listing_installed(self):
        # The stale-name case from a rename: point at what's actually there.
        with self.assertRaises(SystemExit) as cm:
            self._remove("claude", self.CRONTAB)
        self.assertEqual(cm.exception.code,
                         "error: no yo-claude block in the crontab; installed: codex, codex-pro (see --status)")
        self.input_mock.assert_not_called()
        with self.assertRaises(SystemExit) as cm:
            self._remove("claude", "15 9 * * * echo mine\n")
        self.assertIn("no yo cron jobs installed", cm.exception.code)

    def test_malformed_crontab_refused_before_prompt(self):
        # Same safeguard as install: never guess a broken block's extent.
        with self.assertRaises(SystemExit) as cm:
            self._remove("codex", "# >>> yo-codex >>>\n0 6 * * * /repo/yo codex\n")
        self.assertIn("missing its end marker", cm.exception.code)
        self.input_mock.assert_not_called()

    def test_removes_from_crontab_as_of_confirmation(self):
        # Like install, act on the crontab as it stands after the user confirms.
        before = self.CRONTAB
        after = "15 9 * * * echo added meanwhile\n" + self.CRONTAB
        _, written = self._remove("codex", [before, after])
        self.assertTrue(written.startswith("15 9 * * * echo added meanwhile\n"))
        self.assertNotIn("yo-codex >>>", written)


class RenderCronTest(unittest.TestCase):
    def test_backend_name_line_stays_bare(self):
        # A canonical command lets yo infer the backend; no --backend emitted.
        block = render_cron([6], "Europe/Paris", "claude", "claude", "/bin")
        self.assertIn(f"{install.JOB_CMD} claude\n", block)
        self.assertNotIn("--backend", block)
        self.assertIn("# >>> yo-claude >>>", block)

    def test_custom_command_emits_backend(self):
        # A custom command name carries its backend into the cron line.
        block = render_cron([6], "Europe/Paris", "perso", "claude", "/bin")
        self.assertIn(f"{install.JOB_CMD} perso --backend claude\n", block)
        self.assertIn("# >>> yo-perso >>>", block)

    def test_codex_lines_probe_by_default_and_only_codex(self):
        self.assertIn(f"{install.JOB_CMD} codex --probe\n", render_cron([6], "UTC", "codex", "codex", "/bin"))
        self.assertIn(f"{install.JOB_CMD} work-ai --backend codex --probe\n",
                      render_cron([6], "UTC", "work-ai", "codex", "/bin"))
        self.assertIn(f"{install.JOB_CMD} codex\n", render_cron([6], "UTC", "codex", "codex", "/bin", probe=False))
        self.assertNotIn("--probe", render_cron([6], "UTC", "claude", "claude", "/bin", probe=True))


class ToSystemTimesTest(unittest.TestCase):
    def test_fixed_offset_zone_converted_to_utc(self):
        # Etc/GMT-5 is UTC+5 (POSIX sign flip) and has no DST, so the offset is
        # stable year-round: 06:00 there is 01:00 UTC, 23:30 is 18:30 UTC.
        with system_tz("UTC"):
            self.assertEqual(
                [hour_minute(t) for t in to_system_times([6, 23.5], "Etc/GMT-5")],
                [(1, 0), (18, 30)],
            )

    def test_same_zone_is_identity(self):
        # When the daemon already runs in the requested tz, times pass through
        # unchanged regardless of DST.
        with system_tz("Europe/Paris"):
            self.assertEqual(
                [hour_minute(t) for t in to_system_times([6, 11 + 2 / 60], "Europe/Paris")],
                [(6, 0), (11, 2)],
            )

    def test_wraps_across_midnight(self):
        # A pre-start warm-up time (negative fractional hour) wraps onto the
        # previous evening, then converts like any other wall-clock time.
        with system_tz("UTC"):
            # -1h -> 23:00 in Etc/GMT-5 -> 18:00 UTC
            self.assertEqual(hour_minute(to_system_times([-1], "Etc/GMT-5")[0]), (18, 0))


if __name__ == "__main__":
    unittest.main()
