"""Offline checks for recorded Codex pings: verdicts, persistence, dispatch."""
import argparse
from contextlib import contextmanager, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from utils import codex_anchor_probe as anchor
from utils import codex_pings as pings


def sample(at, reset=None, used=0):
    return {"read_at": at, "resets_at": at + 18000 if reset is None else reset, "used_percent": used}


def args_for(**overrides):
    base = dict(command="wrapper", backend="codex", model="", effort="", thread_source="", prompt="")
    base.update(overrides)
    return argparse.Namespace(**base)


class ScratchCase(unittest.TestCase):
    """A scratch directory per test, plus mocks that are undone automatically."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


class ProbeTests(ScratchCase):
    def test_window_state_and_quick_state(self):
        self.assertEqual(anchor.window_state([sample(100, 18100), sample(115, 18100)]), "active")
        self.assertEqual(anchor.window_state([sample(100), sample(115)]), "idle")
        self.assertEqual(anchor.quick_state(sample(100, used=3)), "active")
        self.assertEqual(anchor.quick_state(sample(100, 50)), "idle")
        self.assertIsNone(anchor.quick_state(sample(100)))

    def test_open_window_skips_the_ping_after_one_read(self):
        result = self.check_probe([sample(100, 18100, used=7)], "window_open", calls=0, sleeps=[])
        self.assertEqual(result["pre_state"], "active")
        self.assertNotIn("ping_start", result)

    def test_ambiguous_pre_read_takes_a_second_and_anchored_needs_the_ping_interval(self):
        reads = [sample(100), sample(115)] + [sample(t, 18118) for t in (120, 300)]
        result = self.check_probe(reads, "anchored")
        self.assertEqual(result["post_state"], "active")
        self.check_probe([sample(100), sample(115)] + [sample(t, 18000) for t in (120, 300)], "inconclusive")

    def test_drift_and_execution_errors_are_distinct(self):
        reads = [sample(t) for t in (100, 115, 120, 300)]
        self.assertEqual(self.check_probe(reads, "not_anchored")["post_state"], "idle")
        result = self.check_probe(reads, "execution_error", rc=7)
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["post_state"], "idle")

    def test_read_failures_never_block_the_ping(self):
        failing = RuntimeError("no app-server")
        result = self.check_probe([failing, failing], "observation_error", sleeps=[10])
        self.assertEqual(result["pre_state"], "unknown")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("no app-server", result["read_error"])
        self.assertNotIn("post_state", result)

    @contextmanager
    def probe_doubles(self, reads, rc):
        """Stand in for the quota reads, the ping and the clock around one probe() call."""
        with mock.patch.object(anchor, "read_rate_limits", side_effect=reads) as read, \
                mock.patch.object(anchor, "run_ping", return_value=(rc, "")) as ping, \
                mock.patch.object(anchor.time, "sleep") as sleep, \
                mock.patch.object(anchor.time, "time", side_effect=[90, 116, 120, 400, 401]):
            yield read, ping, sleep

    def check_probe(self, reads, outcome, calls=1, rc=0, sleeps=(15, 10, 60)):
        variant = {"model": "gpt-5.6-terra", "effort": "medium", "thread_source": "", "prompt": "yo"}
        with self.probe_doubles(reads, rc) as (read, ping, sleep):
            result = anchor.probe(variant, "wrapper", self.root / "ping.log")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(sleeps))
        self.assertEqual(ping.call_count, calls)
        self.assertEqual(read.call_count, len(reads))
        self.assertTrue(all(c.kwargs == {"command": "wrapper"} for c in read.call_args_list))
        self.assertEqual(result["outcome"], outcome)
        return result

    def test_ping_details_come_from_the_ping_log(self):
        log = self.root / "yo-wrapper-x.log"
        log.write_text("agent=wrapper backend=codex model=gpt-5.6-terra reasoning_effort=medium thread_source=user(default)\n"
                       "OpenAI Codex v0.153.4\nsession id: 01a08697-1a0a\nuser\nyo\ncodex\nYo\ntokens used\n2,403\n")
        usage = self.patch(anchor, "session_usage", return_value={"cached_input_tokens": 11008})
        details = anchor.ping_details(log, {"model": "", "effort": "", "thread_source": "", "prompt": ""})
        usage.assert_called_once_with("01a08697-1a0a")
        self.assertEqual(details["effective"], {"model": "gpt-5.6-terra", "effort": "medium",
                                                "thread_source": "", "prompt": "yo"})
        self.assertEqual((details["cli_version"], details["tokens_used"]), ("0.153.4", 2403))
        self.assertEqual(details["usage"], {"cached_input_tokens": 11008})
        log.write_text("agent=wrapper backend=codex model=m reasoning_effort=high thread_source=scheduled\n")
        details = anchor.ping_details(log, {"model": "m", "effort": "high", "thread_source": "scheduled", "prompt": "hi"})
        self.assertEqual(details["effective"]["thread_source"], "scheduled")
        self.assertEqual(details["effective"]["prompt"], "hi")
        self.assertNotIn("usage", details)

    def test_session_usage_is_the_cumulative_total_across_requests(self):
        rollout = self.root / "sessions" / "2026" / "09" / "09" / "rollout-2026-09-09T04-30-04-abc123.jsonl"
        rollout.parent.mkdir(parents=True)

        def token_count(last, total):
            return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": last, "total_token_usage": total}}})
        first = {"input_tokens": 13000, "cached_input_tokens": 11000, "output_tokens": 40,
                 "reasoning_output_tokens": 0, "total_tokens": 13040}
        second = {"input_tokens": 13500, "cached_input_tokens": 13000, "output_tokens": 12,
                  "reasoning_output_tokens": 0, "total_tokens": 13512}
        total = {"input_tokens": 26500, "cached_input_tokens": 24000, "output_tokens": 52,
                 "reasoning_output_tokens": 0, "total_tokens": 26552}
        rollout.write_text("not json\n" + json.dumps({"type": "session_meta", "payload": {}}) + "\n"
                           + token_count(first, first) + "\n" + token_count(second, total) + "\n")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            self.assertEqual(anchor.session_usage("abc123"), total)
            self.assertIsNone(anchor.session_usage("missing"))


class RecordTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.log_dir = self.root / "logs"
        self.patch(pings, "ROOT_DIR", self.root)
        self.patch(pings, "cli_version", return_value="0.153.4")
        self.patch(pings.shutil, "which", return_value="/opt/bin/wrapper")
        self.patch(pings.sys.stdout, "isatty", return_value=False)

    def probe_returning(self, outcome, rc=0):
        def fake_probe(variant, command, log_file):
            self.assertEqual(command, "wrapper")
            self.assertRegex(str(log_file), r"logs/yo-wrapper-\d{8}T\d{6}Z(-\d+)?\.log$")
            return {"variant": variant, "outcome": outcome, "returncode": rc, "started_at": time.time()}
        return mock.patch.object(pings, "probe", side_effect=fake_probe)

    def records(self):
        return pings.load_records("wrapper", self.log_dir)

    def test_each_run_appends_one_line_with_identity_and_exit_codes(self):
        with self.probe_returning("anchored") as probe:
            self.assertEqual(pings.run(args_for()), 0)
        self.assertEqual(probe.call_args.args[0], {"model": "", "effort": "", "thread_source": "", "prompt": ""})
        (record,) = self.records()
        self.assertEqual({k: record[k] for k in ("schema", "command", "executable", "cli_version", "source", "outcome")},
                         {"schema": 1, "command": "wrapper", "executable": "/opt/bin/wrapper",
                          "cli_version": "0.153.4", "source": "default", "outcome": "anchored"})
        for outcome, rc, expected in (("not_anchored", 0, 3), ("window_open", 0, 0), ("inconclusive", 0, 0),
                                      ("observation_error", 0, 0), ("execution_error", 7, 7)):
            with self.probe_returning(outcome, rc):
                self.assertEqual(pings.run(args_for(model="gpt-5.6-luna")), expected)
            self.assertEqual(self.records()[-1]["source"], "manual")
        self.assertEqual(len(self.records()), 6)

    def test_concurrent_run_is_skipped_and_other_backends_are_refused(self):
        self.log_dir.mkdir(parents=True)
        with (self.log_dir / "yo-wrapper.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.probe_returning("anchored") as probe, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(pings.run(args_for()), 0)
        probe.assert_not_called()
        self.assertIn("skipped", err.getvalue())
        self.assertEqual(self.records(), [])
        with self.probe_returning("anchored") as probe:
            with self.assertRaisesRegex(ValueError, "only supported with the codex backend"):
                pings.run(args_for(backend="claude"))
        probe.assert_not_called()


class DispatchTests(ScratchCase):
    def setUp(self):
        super().setUp()
        source = Path(__file__).resolve().parent
        shutil.copy(source / "yo", self.root / "yo")
        shutil.copytree(source / "utils", self.root / "utils", ignore=shutil.ignore_patterns("__pycache__"))
        cli = self.root / "wrapper"
        shutil.copy(source / "tests/fixtures/fake_codex.py", cli)
        cli.chmod(0o755)
        self.argv = self.root / "argv.json"
        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}", FAKE_CODEX_ARGV=str(self.argv))
        self.env.pop("CODEX_HOME", None)
        self.log_dir = self.root / "logs"

    def yo(self, *args, env=None, backend="codex"):
        return subprocess.run([str(self.root / "yo"), "wrapper", "--backend", backend, *args],
                              env=env or self.env, capture_output=True, text=True, timeout=30)

    def sent_prompt(self):
        prompt = json.loads(self.argv.read_text())[-1]
        self.argv.unlink()
        return prompt

    def loaded(self):
        return pings.load_records("wrapper", self.log_dir)

    def test_a_codex_ping_flows_through_the_recorder_and_back_into_its_log(self):
        # One real recorded ping (it pays the post-ping settle); the mocked
        # RecordTests cover the other outcomes and exit codes.
        run = self.yo("--prompt", "test prompt", "--model", "gpt-5.6-luna", "--thread-source", "scheduled")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "")  # cron stays quiet; the verdict lives in the logs
        self.assertEqual(self.sent_prompt(), "test prompt")
        (record,) = self.loaded()
        self.assertEqual((record["source"], record["outcome"], record["cli_version"], record["returncode"]),
                         ("manual", "observation_error", "codex-cli test-version", 0))
        self.assertEqual(record["effective"],
                         {"model": "gpt-5.6-luna", "effort": "medium", "thread_source": "scheduled", "prompt": "test prompt"})
        self.assertIn("fixture has no quota api", record["read_error"])
        (ping_log,) = pings.run_logs("wrapper", self.log_dir)
        self.assertEqual(ping_log.resolve(), Path(record["log"]).resolve())
        text = ping_log.read_text()
        self.assertIn("agent=wrapper backend=codex model=gpt-5.6-luna reasoning_effort=medium thread_source=scheduled", text)
        self.assertLess(text.index("yo end rc=0"), text.index("manual ping: observation_error"))
        self.assertTrue(text.rstrip().splitlines()[-1].startswith("record: "))

    def test_open_window_sends_nothing_and_no_record_is_raw(self):
        open_window = json.dumps({"usedPercent": 12, "windowDurationMins": 300, "resetsAt": time.time() + 7200})
        skipped = self.yo(env=self.env | {"FAKE_CODEX_QUOTA": open_window})
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertFalse(self.argv.exists())
        self.assertEqual([r["outcome"] for r in self.loaded()], ["window_open"])

        raw = self.yo("--no-record", "--prompt", '17 * 23; $(touch not-executed) "quoted"')
        self.assertEqual(raw.returncode, 0, raw.stderr)
        self.assertEqual(self.sent_prompt(), '17 * 23; $(touch not-executed) "quoted"')
        self.assertFalse((self.root / "not-executed").exists())
        self.assertEqual(len(self.loaded()), 1)

    def test_claude_stays_plain(self):
        plain = self.yo(backend="claude")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(self.sent_prompt(), "yo")
        self.assertEqual(self.loaded(), [])
        self.assertEqual(len(pings.run_logs("wrapper", self.log_dir)), 1)  # the plain run log, no record

    def test_hyphen_leading_prompts_reach_the_model_not_the_option_parser(self):
        for backend in ("codex", "claude"):
            run = self.yo("--no-record", "--prompt", "--help", backend=backend)
            self.assertEqual(run.returncode, 0, run.stderr)
            argv = json.loads(self.argv.read_text())
            self.argv.unlink()
            self.assertEqual(argv[-2:], ["--", "--help"], backend)


if __name__ == "__main__":
    unittest.main()
