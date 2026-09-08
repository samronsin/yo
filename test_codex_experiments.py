"""Offline checks for grid traversal, shared probe, persistence, and dispatch."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from utils import experiments
from utils import codex_anchor_probe as anchor


def sample(at, reset=None):
    return {"read_at": at, "resets_at": at + 18000 if reset is None else reset,
            "used_percent": 0}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(command="wrapper", model="", effort="",
                                       thread_source="", status=False)
        self.grid = experiments.variants(self.args)

    def test_full_grid_and_pinned_axes(self):
        self.assertEqual(len(self.grid), 24)
        self.args.model, self.args.effort = "custom-model", "high"
        pinned = experiments.variants(self.args)
        self.assertEqual(len(pinned), 4)
        self.assertTrue(all(v["model"] == "custom-model" and v["effort"] == "high" for v in pinned))

    def test_claude_grid_uses_only_supported_axes(self):
        self.args.backend = "claude"
        grid = experiments.variants(self.args)
        self.assertEqual(len(grid), 4)
        self.assertTrue(all(v["effort"] == "" and v["thread_source"] == "" for v in grid))
        self.args.effort = "high"
        with self.assertRaisesRegex(ValueError, "do not support"):
            experiments.variants(self.args)

    def test_screen_untried_before_repeating_success(self):
        trials = [{"variant": self.grid[0], "outcome": "anchored"}]
        chosen = experiments.choose(experiments.statistics(self.grid, trials))
        self.assertNotEqual(chosen["variant"], self.grid[0])

    def test_success_does_not_promote_the_last_variant(self):
        trials = [{"variant": v, "outcome": "anchored"} for v in self.grid]
        trials += [{"variant": self.grid[0], "outcome": "anchored"}] * 2
        self.assertNotEqual(experiments.choose(experiments.statistics(self.grid, trials))["variant"], self.grid[0])

    def test_unknowns_do_not_score_and_no_per_variant_success_count(self):
        trials = [{"variant": self.grid[0], "outcome": outcome} for outcome in
                  ("anchored", "observation_error", "active_window", "execution_error")]
        stats = experiments.statistics(self.grid, trials)[0]
        self.assertEqual(stats["trials"], 2)
        self.assertNotIn("successes", stats)

    def test_all_failures_do_not_retry_one_combination_forever(self):
        trials = [{"variant": v, "outcome": "execution_error"} for v in self.grid]
        trials.append(trials[0])
        self.assertNotEqual(experiments.choose(experiments.statistics(self.grid, trials))["variant"], self.grid[0])

    def test_history_uses_probe_and_persists_evidence(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(experiments, "ROOT_DIR", Path(temp)):
            with mock.patch.object(experiments.subprocess, "run", return_value=mock.Mock(stdout="version")):
                def result(variant, command, log_path):
                    self.assertEqual(command, "wrapper")
                    return {"variant": variant, "outcome": "anchored", "reads": [sample(100)]}
                with mock.patch.object(experiments, "probe", side_effect=result) as probe:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(experiments.run(self.args), 0)
                    probe.assert_called_once()
                history = next(Path(temp).glob("logs/experiments/*/*.json"))
                saved = json.loads(history.read_text())
                self.assertEqual(len(saved["trials"]), 1)
                self.assertNotIn("sequences", saved)
                saved["sequences"] = [{"outcome": "open"}]
                history.write_text(json.dumps(saved))
                self.args.effort = "high"
                with mock.patch.object(experiments, "probe", side_effect=result), redirect_stdout(io.StringIO()):
                    experiments.run(self.args)
                self.assertEqual(len(json.loads(history.read_text())["trials"]), 2)
                self.assertEqual(json.loads(history.read_text())["trials"][0], saved["trials"][0])
                self.assertNotIn("sequences", json.loads(history.read_text()))
                self.assertEqual(len(list(history.parent.glob("*.json"))), 1)
                self.args.status = True
                with mock.patch.object(experiments, "probe") as probe, redirect_stdout(io.StringIO()) as output:
                    experiments.run(self.args)
                    probe.assert_not_called()
                self.assertNotIn("sequences", json.loads(output.getvalue()))

    def attempt(self, variant, at, outcome):
        return {"variant": self.grid[variant], "outcome": outcome,
                "ping_start": at, "ping_end": at + 5,
                "reads": [sample(t, at + 18000 if outcome == "anchored" else None)
                          for t in (at + 10, at + 130, at + 250)]}

    def test_report_preserves_individual_evidence_and_omits_private_metadata(self):
        trials = [self.attempt(0, 1700000000, "not_anchored"),
                  self.attempt(1, 1700000120, "anchored")]
        for trial in trials:
            trial.update(started_at=trial["ping_start"], command="private-wrapper",
                         error="/private/home/token", settings={"version": "codex-test",
                                                                 "executable": "/private/bin/codex"})
        original = json.dumps(trials, sort_keys=True)
        text = experiments.report({"trials": trials})
        payload = json.loads(text.split("```json\n")[1].split("\n```")[0])
        self.assertNotIn("sequences", payload)
        self.assertEqual(payload["report_schema"], 3)
        self.assertNotIn("delay_seconds", payload["trials"][1])
        single = experiments.report({"trials": trials[1:]})
        single_payload = json.loads(single.split("```json\n")[1].split("\n```")[0])
        self.assertEqual(payload["trials"][1], single_payload["trials"][0])
        self.assertEqual(payload["trials"][1]["duration_seconds"], 5)
        self.assertEqual(payload["trials"][0]["verification"], {
            "elapsed_seconds": 240, "reset_drift_seconds": [0, 120, 240]})
        self.assertEqual(payload["trials"][1]["verification"]["reset_drift_seconds"], [0, 0, 0])
        self.assertEqual(payload["trials"][1]["variant"], self.grid[1])
        self.assertFalse(payload["traffic_complete"])
        for private in ("private-wrapper", "/private/", "1700000000"):
            self.assertNotIn(private, text)
        self.assertEqual(text, experiments.report({"trials": trials}))
        self.assertEqual(original, json.dumps(trials, sort_keys=True))
        summary = text.split("<details>")[0]
        self.assertLess(len(summary.splitlines()), 20)
        self.assertIn("2 trials.", summary)
        self.assertNotIn("Sequence", summary)
        self.assertNotIn("report_schema", summary)
        for redundant in ("started_at_seconds", "finished_at_seconds", "ping_start_seconds",
                          "ping_end_seconds", "read_seconds", "reset_seconds", "used_percent"):
            self.assertNotIn('"' + redundant + '"', text)

    def test_report_timing_handles_skips_and_missing_end(self):
        trials = [self.attempt(0, 100, "anchored"),
                  {"variant": self.grid[0], "outcome": "observation_error"},
                  self.attempt(1, 20000, "execution_error")]
        trials[-1].pop("ping_end")
        trials[-1]["reads"] = []
        text = experiments.report({"trials": trials})
        payload = json.loads(text.split("```json\n")[1].split("\n```")[0])
        self.assertIsNone(payload["trials"][1]["duration_seconds"])
        self.assertIsNone(payload["trials"][2]["duration_seconds"])
        self.assertIsNone(payload["trials"][2]["verification"])

    def test_already_open_window_reports_no_data_for_new_and_old_records(self):
        for outcome in ("no_data", "active_window"):
            trial = {"variant": self.grid[0], "outcome": outcome,
                     "reason": "window_already_open"} if outcome == "no_data" else {
                         "variant": self.grid[0], "outcome": outcome}
            trial["pre"] = [sample(100, 18100), sample(115, 18100)]
            text = experiments.report({"trials": [trial]})
            payload = json.loads(text.split("```json\n")[1].split("\n```")[0])
            self.assertEqual(payload["trials"][0]["outcome"], "no_data")
            self.assertEqual(payload["trials"][0]["reason"], "window_already_open")
            self.assertNotIn("sequences", payload)
            self.assertIn("No data: 1 run(s) skipped", text)
            self.assertEqual(experiments.statistics(self.grid, [trial])[0]["trials"], 0)

    def test_report_table_escapes_prompt_markup(self):
        trial = self.attempt(0, 100, "anchored")
        trial["variant"] = dict(trial["variant"], prompt='<script>x</script>|line\nnext')
        text = experiments.report({"trials": [trial]})
        summary = text.split("<details>")[0]
        self.assertNotIn("<script>", summary)
        self.assertIn("&#124;line<br>next", summary)
        self.assertIn("&lt;script&gt;", summary)

    def test_report_is_offline_and_does_not_create_history(self):
        self.args.report = True
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(experiments, "ROOT_DIR", Path(temp)):
            with mock.patch.object(experiments.subprocess, "run") as run:
                with mock.patch.object(experiments, "probe") as probe, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(experiments.run(self.args), 0)
                run.assert_not_called()
                probe.assert_not_called()
            self.assertIn("0 trials.", output.getvalue())
            self.assertFalse((Path(temp) / "logs").exists())


class ProbeTests(unittest.TestCase):
    def test_stable_zero_percent_is_active_and_drifting_is_idle(self):
        self.assertEqual(anchor.window_state([sample(100, 18100), sample(115, 18100)]), "active")
        self.assertEqual(anchor.window_state([sample(100), sample(115)]), "idle")

    def test_active_window_skips_ping_and_post_readings(self):
        result = self.check_probe([sample(100, 18100), sample(115, 18100)], "no_data", calls=0)
        self.assertEqual(result["reason"], "window_already_open")

    def test_stable_anchor_in_request_interval_succeeds(self):
        self.check_probe([sample(100), sample(115)] + [sample(t, 18118) for t in (120, 240, 360)], "anchored")

    def test_external_anchor_is_inconclusive(self):
        self.check_probe([sample(100), sample(115)] + [sample(t, 18000) for t in (120, 240, 360)], "inconclusive")

    def test_drift_and_execution_errors_are_distinct(self):
        reads = [sample(t) for t in (100, 115, 120, 240, 360)]
        self.check_probe(reads, "not_anchored")
        self.check_probe(reads, "execution_error", rc=7)

    def check_probe(self, reads, outcome, calls=1, rc=0):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(anchor, "read_rate_limits", side_effect=reads) as read:
                with mock.patch.object(anchor, "run_ping", return_value=rc) as ping:
                    with mock.patch.object(anchor.time, "sleep"), mock.patch.object(anchor.time, "time", side_effect=[90, 116, 120, 400]):
                        variant = {name: values[0] for name, values in experiments.PARAMETERS.items()}
                        result = anchor.probe(variant, "wrapper", Path(temp) / "probe.log")
                self.assertEqual(ping.call_count, calls)
                self.assertTrue(all(c.kwargs == {"command": "wrapper"} for c in read.call_args_list))
            self.assertEqual(result["outcome"], outcome)
            return result


class DispatchTests(unittest.TestCase):
    def test_wrapper_status_and_manual_prompt_without_recursion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = Path(__file__).resolve().parent
            shutil.copy(source / "yo", root / "yo")
            shutil.copytree(source / "utils", root / "utils", ignore=shutil.ignore_patterns("__pycache__"))
            cli = root / "wrapper"
            shutil.copy(source / "tests/fixtures/fake_codex.py", cli)
            cli.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", FAKE_CODEX_ARGV=str(root / "argv.json"))
            cmd = [str(root / "yo"), "wrapper", "--backend", "codex"]
            status = subprocess.run(cmd + ["--experiment-status", "--effort", "high"], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(len(json.loads(status.stdout)["results"]), 8)
            self.assertFalse((root / "argv.json").exists())
            ordinary = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
            self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
            self.assertEqual(json.loads((root / "argv.json").read_text())[-1], "yo")
            self.assertEqual(list((root / "logs/experiments").glob("*/history.json")), [])
            codex_trial = subprocess.run(cmd + ["--experiment"], env=env, capture_output=True, timeout=10)
            self.assertEqual(codex_trial.returncode, 2)  # fixture has no quota API
            codex_history = next((root / "logs/experiments").glob("*/history.json"))
            self.assertEqual(json.loads(codex_history.read_text())["trials"][0]["outcome"], "observation_error")
            claude = [str(root / "yo"), "wrapper", "--backend", "claude"]
            ordinary_claude = subprocess.run(claude, env=env, capture_output=True, timeout=10)
            self.assertEqual(ordinary_claude.returncode, 0, ordinary_claude.stderr)
            claude_trial = subprocess.run(claude + ["--experiment", "--prompt", "test prompt"],
                                          env=env, capture_output=True, timeout=10)
            self.assertEqual(claude_trial.returncode, 0, claude_trial.stderr)
            self.assertEqual(json.loads((root / "argv.json").read_text())[-1], "test prompt")
            histories = [json.loads(p.read_text()) for p in (root / "logs/experiments").glob("*/history.json")]
            self.assertEqual(len(histories), 2)
            record = next(h for h in histories if h["settings"]["backend"] == "claude")["trials"][0]
            self.assertEqual(record["outcome"], "unverified")
            self.assertEqual(record["reads"], [])
            report = subprocess.run([str(root / "yo"), "missing-wrapper", "--backend", "codex",
                                     "--experiment-report"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("0 trials.", report.stdout)
            prompt = '17 * 23; $(touch not-executed) "quoted"'
            result = subprocess.run(cmd + ["--prompt", prompt], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "argv.json").read_text())[-1], prompt)
            failed = subprocess.run(cmd + ["--no-experiment"], env=env | {"FAKE_CODEX_RC": "7"},
                                    capture_output=True, timeout=10)
            self.assertEqual(failed.returncode, 7)
            with mock.patch.object(anchor.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                anchor.run_ping(None, None, None, root / "probe.log", "wrapper", prompt)
            self.assertIn("--no-experiment", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
