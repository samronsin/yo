"""Offline checks for the Codex runner: invocation, verdicts, records, dispatch."""
import argparse
from contextlib import redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from tests.support import ScratchCase
from utils import codex_anchor_probe as anchor
from utils import codex_runner as runner


def sample(at, reset=None, used=0):
    return {"read_at": at, "resets_at": at + 18000 if reset is None else reset, "used_percent": used}


def args_for(**overrides):
    base = dict(command="wrapper", backend="codex", model="", effort="", thread_source="", probe=True)
    base.update(overrides)
    return argparse.Namespace(**base)


class ProbeTests(ScratchCase):
    def test_window_state_and_quick_state(self):
        self.assertEqual(anchor.window_state([sample(100, 18100), sample(115, 18100)]), "active")
        self.assertEqual(anchor.window_state([sample(100), sample(115)]), "idle")
        self.assertEqual(anchor.quick_state(sample(100, used=3)), "active")
        self.assertEqual(anchor.quick_state(sample(100, 50)), "idle")
        self.assertIsNone(anchor.quick_state(sample(100)))

    def test_five_hour_window_is_found_wherever_it_sits(self):
        five_hour = {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1788975739}
        weekly = {"usedPercent": 36, "windowDurationMins": 10080, "resetsAt": 1789446610}
        self.assertIs(anchor.five_hour_window({"primary": five_hour, "secondary": weekly}), five_hour)
        self.assertIs(anchor.five_hour_window({"primary": weekly, "secondary": five_hour}), five_hour)
        for limits in ({"primary": weekly}, {"primary": None, "secondary": None}, {}):
            with self.assertRaisesRegex(RuntimeError, "no five-hour quota window"):
                anchor.five_hour_window(limits)

    def test_open_window_skips_the_ping_after_one_read(self):
        result = self.check_probe([sample(100, 18100, used=7)], "window_open", calls=0, sleeps=[])
        self.assertEqual(result["pre_state"], "active")
        self.assertNotIn("ping_start", result)

    def test_ambiguous_pre_read_takes_a_second_and_anchored_needs_the_ping_interval(self):
        reads = [sample(100), sample(115)] + [sample(t, 18118) for t in (120, 300)]
        result = self.check_probe(reads, "anchored")
        self.assertEqual(result["post_state"], "active")
        self.assertEqual(result["session_id"], "s1")  # the sender's details land in the result
        self.check_probe([sample(100), sample(115)] + [sample(t, 18000) for t in (120, 300)], "inconclusive")

    def test_drift_and_execution_errors_are_distinct(self):
        reads = [sample(t) for t in (100, 115, 120, 300)]
        self.assertEqual(self.check_probe(reads, "not_anchored")["post_state"], "idle")
        result = self.check_probe(reads, "execution_error", rc=7)
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["post_state"], "idle")
        result = self.check_probe(reads, "execution_error", send=mock.Mock(side_effect=OSError("no such file")))
        self.assertEqual((result["returncode"], result["error"]), (None, "no such file"))

    def test_read_failures_never_block_the_ping(self):
        failing = RuntimeError("no app-server")
        result = self.check_probe([failing, failing], "observation_error", sleeps=[10])
        self.assertEqual(result["pre_state"], "unknown")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("no app-server", result["read_error"])
        self.assertNotIn("post_state", result)

    def check_probe(self, reads, outcome, calls=1, rc=0, sleeps=(15, 10, 60), send=None):
        send = send or mock.Mock(return_value={"returncode": rc, "session_id": "s1"})
        with mock.patch.object(anchor, "read_rate_limits", side_effect=reads) as read, \
                mock.patch.object(anchor.time, "sleep") as sleep, \
                mock.patch.object(anchor.time, "time", side_effect=[90, 116, 120, 400, 401]):
            result = anchor.probe(send, "wrapper")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(sleeps))
        self.assertEqual(send.call_count, calls)
        self.assertEqual(read.call_count, len(reads))
        self.assertTrue(all(c.kwargs == {"command": "wrapper"} for c in read.call_args_list))
        self.assertEqual(result["outcome"], outcome)
        return result


class InvocationTests(ScratchCase):
    def test_codex_command_defaults_and_overrides(self):
        argv = runner.codex_command("codex-pro", runner.DEFAULTS, "/tmp/last.txt")
        self.assertEqual(argv[:2], ["codex-pro", "exec"])
        self.assertNotIn("--thread-source", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[-1], "yo")
        config = {"model": "gpt-5.6-luna", "effort": "high", "thread_source": "scheduled"}
        argv = runner.codex_command("codex", config, "/tmp/last.txt")
        self.assertEqual(argv[2:4], ["--thread-source", "scheduled"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-luna")
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_send_ping_logs_the_run_and_reads_details_back(self):
        self.install_fake_cli()
        log = self.root / "run.log"
        with mock.patch.dict(os.environ, self.env):
            details = runner.send_ping("wrapper", runner.DEFAULTS, log, self.root / "last.txt")
        self.assertEqual(details["returncode"], 0)
        self.assertEqual(self.sent_argv()[-1], "yo")
        self.assertEqual((self.root / "last.txt").read_text(), "391\n")
        text = log.read_text()
        self.assertIn("agent=wrapper backend=codex model=gpt-5.6-terra reasoning_effort=medium thread_source=user(default)\n", text)
        self.assertLess(text.index("turn.completed"), text.index("yo end rc=0"))
        with mock.patch.dict(os.environ, self.env | {"FAKE_CODEX_RC": "7"}):
            self.assertEqual(runner.send_ping("wrapper", runner.DEFAULTS, log, self.root / "last.txt")["returncode"], 7)
        self.assertIn("yo end rc=7", log.read_text())

    def test_parse_output_takes_version_session_and_tokens(self):
        details = runner.parse_output("Reading additional input from stdin...\nOpenAI Codex v0.153.4\n--------\n"
                                      "session id: 01a08697-1a0a\n--------\nuser\nyo\ncodex\nYo\ntokens used\n2,403\nYo\n")
        self.assertEqual(details, {"cli_version": "0.153.4", "session_id": "01a08697-1a0a", "tokens_used": 2403})
        self.assertEqual(runner.parse_output('{"type": "turn.completed"}\n'), {})

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
        rollout.write_text("not json\n" + json.dumps({"type": "session_meta", "payload": None}) + "\n"
                           + token_count(first, first) + "\n" + token_count(second, total) + "\n")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root)}):
            self.assertEqual(runner.session_usage("abc123"), total)
            self.assertIsNone(runner.session_usage("missing"))


class RunTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.log_dir = self.root / "logs"
        self.patch(runner, "ROOT_DIR", self.root)
        self.patch(runner, "cli_version", return_value="0.153.4")
        self.patch(runner.shutil, "which", return_value="/opt/bin/wrapper")

    def probe_returning(self, outcome, rc=0):
        def fake_probe(send, command):
            self.assertEqual(command, "wrapper")
            return {"outcome": outcome, "returncode": rc, "started_at": time.time()}
        return mock.patch.object(runner, "probe", side_effect=fake_probe)

    def records(self):
        return runner.load_records("wrapper", self.log_dir)

    def test_plain_run_sends_and_logs_without_probing(self):
        with self.probe_returning("anchored") as probe, \
                mock.patch.object(runner, "send_ping", return_value={"returncode": 7}) as send:
            self.assertEqual(runner.run(args_for(probe=False, model="gpt-5.6-luna")), 7)
        probe.assert_not_called()
        self.assertEqual(send.call_args.args[1], {**runner.DEFAULTS, "model": "gpt-5.6-luna"})
        (log,) = runner.run_logs("wrapper", self.log_dir)
        self.assertIn("yo start", log.read_text())
        self.assertEqual(self.records(), [])
        self.assertFalse((self.log_dir / "yo-wrapper.lock").exists())

    def test_each_run_writes_one_log_with_identity_verdict_record_and_exit_code(self):
        with self.probe_returning("anchored"):
            self.assertEqual(runner.run(args_for()), 0)
        (record,) = self.records()
        self.assertEqual({k: record[k] for k in ("schema", "command", "executable", "cli_version", "source", "outcome")},
                         {"schema": 1, "command": "wrapper", "executable": "/opt/bin/wrapper",
                          "cli_version": "0.153.4", "source": "default", "outcome": "anchored"})
        self.assertEqual(record["effective"], {**runner.DEFAULTS, "prompt": "yo"})
        (log,) = runner.run_logs("wrapper", self.log_dir)
        lines = log.read_text().splitlines()
        self.assertIn("yo start", lines[0])
        self.assertIn("default ping: anchored", lines[-2])
        self.assertTrue(lines[-1].startswith("record: "))
        for outcome, rc, expected in (("not_anchored", 0, 3), ("window_open", 0, 0), ("inconclusive", 0, 0),
                                      ("observation_error", 0, 0), ("execution_error", 7, 7)):
            with self.probe_returning(outcome, rc):
                self.assertEqual(runner.run(args_for(model="gpt-5.6-luna")), expected)
            self.assertEqual(self.records()[-1]["source"], "manual")
            self.assertEqual(self.records()[-1]["effective"]["model"], "gpt-5.6-luna")
        self.assertEqual(len(self.records()), 6)

    def test_concurrent_run_is_skipped(self):
        self.log_dir.mkdir(parents=True)
        with (self.log_dir / "yo-wrapper.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.probe_returning("anchored") as probe, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(runner.run(args_for()), 0)
        probe.assert_not_called()
        self.assertIn("skipped", err.getvalue())
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
