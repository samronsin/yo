"""`yo --status`: quota reads per backend, the last-run summary, rendering, and dispatch."""
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from datetime import datetime
import io
import json
import os
import time
import unittest
from zoneinfo import ZoneInfo
from unittest import mock

from tests.helpers import REPO_ROOT, ScratchCase
from utils import claude_runner, codex_anchor_probe as anchor, codex_runner, settings, status

SPEC = spec_from_loader("yo_cli_status", SourceFileLoader("yo_cli_status", str(REPO_ROOT / "yo")))
yo = module_from_spec(SPEC)
SPEC.loader.exec_module(yo)

WEEKLY = {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 1789446610}


def five_hour(used, resets_in):
    return {"usedPercent": used, "windowDurationMins": 300, "resetsAt": time.time() + resets_in}


class FakeCliCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.install_fake_cli()
        self.patch(codex_runner, "ROOT_DIR", self.root)
        self.log_dir = self.root / "logs"
        self.sleep = self.patch(anchor.time, "sleep")

    def with_env(self, **extra):
        env = dict(self.env)
        for key, value in extra.items():
            env[key] = value if isinstance(value, str) else json.dumps(value)
        return mock.patch.dict(os.environ, env, clear=True)


class CodexQuotaTests(FakeCliCase):
    def test_open_window_and_weekly_from_one_read(self):
        with self.with_env(FAKE_CODEX_QUOTA={"primary": five_hour(12, 7200), "secondary": WEEKLY}):
            quota = codex_runner.quota("wrapper")
        five = quota["five_hour"]
        self.assertEqual((five["state"], five["used_percent"], len(five["reads"])), ("active", 12, 1))
        self.assertAlmostEqual(five["anchored_at"], five["resets_at"] - anchor.WINDOW_SECS)
        self.assertEqual(quota["weekly"], [{"window_minutes": 10080, "used_percent": 31, "resets_at": 1789446610}])
        self.sleep.assert_not_called()

    def test_idle_account_needs_one_read_too(self):
        with self.with_env(FAKE_CODEX_QUOTA={"secondary": five_hour(0, -60), "primary": WEEKLY}):
            quota = codex_runner.quota("wrapper")
        self.assertEqual(quota["five_hour"]["state"], "idle")
        self.assertIsNone(quota["five_hour"]["anchored_at"])
        self.assertEqual(quota["weekly"][0]["used_percent"], 31)
        self.sleep.assert_not_called()

    def test_ambiguous_read_takes_a_second_after_the_pre_wait(self):
        with self.with_env(FAKE_CODEX_QUOTA=five_hour(0, anchor.WINDOW_SECS)):
            quota = codex_runner.quota("wrapper")
        self.assertEqual(len(quota["five_hour"]["reads"]), 2)
        self.assertEqual(quota["five_hour"]["state"], "unknown")  # sleep is mocked: no time passed between reads
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [anchor.PRE_WAIT_SECS])
        self.assertEqual(quota["weekly"], [])

    def test_account_without_a_five_hour_window_still_reports_the_rest(self):
        with self.with_env(FAKE_CODEX_QUOTA={"primary": WEEKLY}):
            quota = codex_runner.quota("wrapper")
        self.assertIsNone(quota["five_hour"])
        self.assertEqual(quota["weekly"][0]["window_minutes"], 10080)

    def test_read_failure_becomes_a_report_error(self):
        with self.with_env():
            out = status.report("wrapper", "codex")
        self.assertIsNone(out["quota"])
        self.assertIn("fixture has no quota api", out["error"])
        self.assertIsNone(out["last_run"])
        with self.with_env():
            for backend in ("codex", "claude"):
                self.assertEqual(status.report("missing-cmd", backend)["error"],
                                 "'missing-cmd' is not an executable on PATH (shell functions and aliases do not count)")


class ClaudeQuotaTests(FakeCliCase):
    def test_parses_the_usage_view_without_spending_a_turn(self):
        with self.with_env():
            quota = claude_runner.quota("wrapper")
        self.assertEqual(self.sent_argv(), claude_runner.USAGE_ARGS)
        self.assertEqual((quota["five_hour"]["state"], quota["five_hour"]["used_percent"]), ("active", 18))
        self.assertEqual(quota["five_hour"]["resets"], "Sep 10 at 4:30pm (Europe/Paris)")
        self.assertIsInstance(quota["five_hour"]["resets_at"], float)
        self.assertEqual([(w["scope"], w["used_percent"], w["window_minutes"]) for w in quota["weekly"]],
                         [(None, 26, 10080), ("Fable", 49, 10080)])
        self.assertEqual(quota["weekly"][1]["resets"], "Sep 14 at 5am (Europe/Paris)")
        self.assertEqual((quota["spent_turns"], quota["cost_usd"]), (0, 0))

    def test_partial_and_unrecognized_views(self):
        with self.with_env(FAKE_CLAUDE_USAGE="Current week (all models): 2.5% used - resets soon\n"):
            quota = claude_runner.quota("wrapper")
        self.assertIsNone(quota["five_hour"])
        self.assertEqual(quota["weekly"], [{"scope": None, "used_percent": 2.5, "resets": "soon",
                                            "resets_at": None, "window_minutes": 10080}])
        with self.with_env(FAKE_CLAUDE_USAGE="Current session: 0% used - resets soon\n"):
            self.assertEqual(claude_runner.quota("wrapper")["five_hour"]["state"], "unknown")
        with self.with_env(FAKE_CLAUDE_USAGE="/usage isn't available in this environment.\n"), \
                self.assertRaisesRegex(RuntimeError, "unrecognized /usage output: \"/usage isn't"):
            claude_runner.quota("wrapper")

    def test_cli_failure_and_spent_turns_are_surfaced(self):
        with self.with_env(FAKE_CODEX_RC="3"), self.assertRaisesRegex(RuntimeError, "exited 3"):
            claude_runner.quota("wrapper")
        with self.with_env(FAKE_CLAUDE_TURNS="1"):
            out = status.report("wrapper", "claude")
        self.assertEqual(out["quota"]["spent_turns"], 1)
        self.assertRegex(status.format_report(out), r"\n  warning +the /usage call spent 1 model turn")


class ParseResetTests(unittest.TestCase):
    NOW = 1789040640.0  # 2026-09-10T11:44:00Z

    def test_known_shapes_resolve_to_the_nearest_year(self):
        paris = ZoneInfo("Europe/Paris")
        cases = {"Sep 10 at 4:30pm (Europe/Paris)": (2026, 9, 10, 16, 30),
                 "Sep 14 at 5am (Europe/Paris)": (2026, 9, 14, 5, 0),
                 "Jan 2 at 12am (Europe/Paris)": (2027, 1, 2, 0, 0),  # ahead of a September read: next year
                 "Dec 30 at 12pm (Europe/Paris)": (2026, 12, 30, 12, 0)}  # ahead too, still this year
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(claude_runner.parse_reset(text, now=self.NOW),
                                 datetime(*expected, tzinfo=paris).timestamp())
        january = datetime(2027, 1, 5, tzinfo=paris).timestamp()  # a stale reset read just after New Year
        self.assertEqual(claude_runner.parse_reset("Dec 30 at 12pm (Europe/Paris)", now=january),
                         datetime(2026, 12, 30, 12, 0, tzinfo=paris).timestamp())
        utc = datetime(2026, 9, 10, 14, 30, tzinfo=ZoneInfo("UTC")).timestamp()
        for text in ("Sep 10 at 2:30pm (UTC)", "Sep 10, 2:30pm (UTC)", "Sep 10, 2026, 2:30 PM (UTC)",
                     "Sep 10 2026 at 2:30pm (UTC)"):
            with self.subTest(text=text):
                self.assertEqual(claude_runner.parse_reset(text, now=self.NOW), utc)
        self.assertEqual(claude_runner.parse_reset("Sep 10, 2025, 2:30pm (UTC)", now=self.NOW),
                         utc - 365 * 86400)  # an explicit year is not second-guessed

    def test_unknown_shapes_give_none(self):
        for text in ("soon", "Sep 10 at 16:30 (Europe/Paris)", "Sep 10 at 4:30pm (Mars/Olympus)",
                     "Xyz 10 at 4:30pm (Europe/Paris)", ""):
            with self.subTest(text=text):
                self.assertIsNone(claude_runner.parse_reset(text, now=self.NOW))


class LastRunTests(ScratchCase):
    def write_log(self, stamp, *lines, command="my-cmd"):
        self.root.mkdir(exist_ok=True)
        (self.root / f"yo-{command}-{stamp}.log").write_text("".join(line + "\n" for line in lines))

    def test_a_wrapper_sharing_the_prefix_is_not_confused_with_the_command(self):
        # "yo-my-cmd-pro-<older>" sorts after "yo-my-cmd-<newer>" whatever the dates.
        self.write_log("20260910T040004Z", "yo end rc=0")
        self.write_log("20260901T040004Z", "yo end rc=7", command="my-cmd-pro")
        self.write_log("20260909T040004Z", "yo end rc=5", command="my-cmd-2")
        self.assertEqual([p.name for p in codex_runner.run_logs("my-cmd", self.root)],
                         ["yo-my-cmd-20260910T040004Z.log"])
        self.assertEqual(status.last_run("my-cmd", self.root)["returncode"], 0)
        self.assertEqual(status.last_run("my-cmd-pro", self.root)["returncode"], 7)
        self.assertEqual(status.last_run("my-cmd-2", self.root)["returncode"], 5)

    def test_newest_log_with_status_and_verdict(self):
        self.write_log("20260910T040004Z", "[x] yo start", "agent=my-cmd", "[x] yo end rc=3",
                       "[x] default ping: not_anchored",
                       "record: " + json.dumps({"outcome": "not_anchored", "source": "default"}))
        self.write_log("20260909T040004Z", "[x] yo start", "[x] yo end rc=0")
        run = status.last_run("my-cmd", self.root)
        self.assertEqual((run["returncode"], run["outcome"], run["source"]), (3, "not_anchored", "default"))
        self.assertEqual(run["started_at"], 1789012804.0)  # 2026-09-10T04:00:04Z
        self.assertTrue(run["log"].endswith("yo-my-cmd-20260910T040004Z.log"))

    def test_unfinished_run_and_no_logs(self):
        self.assertIsNone(status.last_run("my-cmd", self.root))
        self.write_log("20260910T040004Z", "[x] yo start", "agent=my-cmd")
        run = status.last_run("my-cmd", self.root)
        self.assertEqual((run["returncode"], run.get("outcome")), (None, None))


class FormatTests(unittest.TestCase):
    NOW = 1789040640.0  # 2026-09-10T11:44:00Z

    def render(self, backend, quota, last_run=None, error=None):
        out = {"command": "wrapper", "backend": backend, "read_at": self.NOW, "quota": quota, "last_run": last_run}
        if error:
            out["error"] = error
        return status.format_report(out)

    def test_codex_states(self):
        resets = self.NOW + 2 * 3600 + 46 * 60
        text = self.render("codex", {
            "five_hour": {"state": "active", "used_percent": 12, "resets_at": resets,
                          "anchored_at": resets - anchor.WINDOW_SECS, "reads": []},
            "weekly": [{"window_minutes": 10080, "used_percent": 31, "resets_at": self.NOW + 4 * 86400}],
        }, last_run={"log": "logs/yo-wrapper-x.log", "returncode": 0, "started_at": self.NOW - 3600,
                     "outcome": "anchored", "source": "default"})
        self.assertIn("5h window   open, 12% used, anchored ", text)
        self.assertIn("(2h46m left)", text)
        self.assertIn("weekly      31% used, resets ", text)
        self.assertIn("anchored (default), rc=0, logs/yo-wrapper-x.log", text)
        idle = self.render("codex", {"five_hour": {"state": "idle", "used_percent": 0, "resets_at": self.NOW - 5,
                                                   "anchored_at": None, "reads": []}, "weekly": []})
        self.assertIn("5h window   idle, no window open, 0% used\n", idle)
        self.assertIn("last run    none (no run logs)", idle)
        unknown = self.render("codex", {"five_hour": {"state": "unknown", "used_percent": None, "resets_at": self.NOW,
                                                      "anchored_at": None, "reads": []},
                                        "weekly": [{"window_minutes": None, "used_percent": None, "resets_at": None}]})
        self.assertIn("open or idle? cannot tell, ?% used, reports reset ", unknown)
        self.assertIn("other window ?% used, resets ?", unknown)
        self.assertIn("5h window   not reported", self.render("codex", {"five_hour": None, "weekly": []}))

    def test_claude_rows_match_codex_shape(self):
        resets = self.NOW + 2 * 3600 + 46 * 60
        text = self.render("claude", {
            "five_hour": {"state": "active", "used_percent": 18, "resets": "raw", "resets_at": resets},
            "weekly": [{"scope": None, "window_minutes": 10080, "used_percent": 26, "resets": "raw",
                        "resets_at": self.NOW + 4 * 86400},
                       {"scope": "Fable", "window_minutes": 10080, "used_percent": 49, "resets": "unparsed text",
                        "resets_at": None}],
            "spent_turns": 0, "cost_usd": 0,
        }, last_run={"log": "logs/yo-wrapper-x.log", "returncode": None, "started_at": None})
        self.assertIn("5h window      open, 18% used, anchored ", text)
        self.assertIn("(2h46m left)", text)
        self.assertIn("\n  weekly         26% used, resets 2026-09-1", text)  # padded to the widest label
        self.assertIn("weekly (Fable) 49% used, resets unparsed text", text)
        self.assertIn("last run       unknown time, still running or crashed, logs/yo-wrapper-x.log", text)
        zero = self.render("claude", {"five_hour": {"state": "unknown", "used_percent": 0,
                                                    "resets": "Sep 10 at 4pm (X)", "resets_at": None},
                                      "weekly": [], "spent_turns": 0, "cost_usd": 0})
        self.assertIn("5h window   open or idle? cannot tell, 0% used, resets Sep 10 at 4pm (X)", zero)

    def test_errors(self):
        self.assertIn("quota       unavailable: no app-server", self.render("codex", None, error="no app-server"))


class DispatchTests(FakeCliCase):
    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.with_env(FAKE_CODEX_QUOTA={"primary": five_hour(12, 7200), "secondary": WEEKLY}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            rc = yo.main(list(argv))
        self.assertEqual(stderr.getvalue(), "")
        return rc, stdout.getvalue()

    def test_status_reports_either_backend(self):
        rc, text = self.invoke("wrapper", "--backend", "claude", "--status")
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("wrapper (claude), read "))
        self.assertIn("5h window      open, 18% used, anchored ", text)  # padded to the widest label
        self.assertIn("\n  weekly (Fable) 49% used, resets 20", text)
        self.assertEqual(self.sent_argv(), claude_runner.USAGE_ARGS)
        rc, text = self.invoke("wrapper", "--status", "--backend", "codex")
        self.assertEqual(rc, 0)
        self.assertIn("5h window   open, 12% used", text)
        self.assertIn("weekly      31% used", text)
        self.assertFalse(self.argv.exists())  # nothing was sent
        self.assertEqual(codex_runner.run_logs("wrapper", self.log_dir), [])  # and no run log written

    def test_registered_command_renders_in_its_timezone(self):
        parser = settings.load()
        settings.update_command(parser, "wrapper", {"backend": "claude", "tz": "Asia/Tokyo"})
        settings.save(parser)
        rc, text = self.invoke("wrapper", "--status")  # no --backend needed
        self.assertEqual(rc, 0)
        self.assertRegex(text.splitlines()[0], r"^wrapper \(claude\), read \d{4}-\d{2}-\d{2} \d{2}:\d{2} JST$")
        self.assertIn(" JST (", text)  # the 5h reset too

    def test_failed_read_prints_the_error_and_exits_1(self):
        with self.with_env(), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(yo.main(["wrapper", "--backend", "codex", "--status"]), 1)
        self.assertIn("quota       unavailable: rateLimits read failed", stdout.getvalue())

    def test_usage_errors(self):
        for argv in (["--status"], ["wrapper", "--status"], ["codex", "--backend", "claude", "--status"],
                     ["codex", "--status", "--probe"], ["codex", "--status", "--model", "m"],
                     ["claude", "--status", "--effort", "high"], ["codex", "--status", "--json"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                yo.main(argv)
            self.assertEqual(exc.exception.code, 2)
        self.assertFalse(self.argv.exists())


if __name__ == "__main__":
    unittest.main()
