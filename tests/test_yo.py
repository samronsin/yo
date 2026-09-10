"""CLI parsing, backend dispatch, and the executable entry point."""
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest import mock

from tests.helpers import REPO_ROOT, ScratchCase
from utils import claude_runner, codex_anchor_probe as anchor, codex_runner, settings

SPEC = spec_from_loader("yo_cli", SourceFileLoader("yo_cli", str(REPO_ROOT / "yo")))
yo = module_from_spec(SPEC)
SPEC.loader.exec_module(yo)


class DispatchTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.install_fake_cli()
        self.patch(codex_runner, "ROOT_DIR", self.root)  # claude_runner logs through codex_runner's helpers

    def invoke(self, *args, backend="codex", env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or self.env), redirect_stdout(stdout), redirect_stderr(stderr):
            rc = yo.main(["wrapper", "--backend", backend, *args])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        return rc

    def test_arguments_reach_the_selected_runner(self):
        for backend, module in (("codex", codex_runner), ("claude", claude_runner)):
            with self.subTest(backend=backend), mock.patch.object(module, "run", return_value=7) as run:
                self.assertEqual(yo.main([backend]), 7)
                self.assertEqual(vars(run.call_args.args[0]), {
                    "command": backend, "backend": backend, "model": "", "effort": "",
                    "thread_source": "", "probe": False, "status": False,
                    "override_source": None, "tz": None,
                })
        with mock.patch.object(codex_runner, "run", return_value=0) as run:
            self.assertEqual(yo.main(["wrapper", "--backend", "codex", "--probe", "--model", "m",
                                      "--effort", "high", "--thread-source", "scheduled"]), 0)
            self.assertEqual(vars(run.call_args.args[0]), {
                "command": "wrapper", "backend": "codex", "model": "m", "effort": "high",
                "thread_source": "scheduled", "probe": True, "status": False,
                "override_source": "manual", "tz": None,
            })

    def test_invalid_arguments_fail_before_running(self):
        cases = [[], [""], ["wrapper"], ["codex", "--backend", "claude"],
                 ["claude", "--probe"], ["codex", "--model"], ["codex", "--backend", "other"],
                 ["codex", "--unknown"], ["codex", "--pro"], ["codex", "--prompt", "hi"],
                 ["sub/dir", "--backend", "codex"], ["--backend", "claude", "--", "--probe"]]
        with mock.patch.object(codex_runner, "run") as codex, mock.patch.object(claude_runner, "run") as claude:
            for argv in cases:
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    yo.main(argv)
                self.assertEqual(exc.exception.code, 2)
            codex.assert_not_called()
            claude.assert_not_called()

    def test_plain_pings_preserve_arguments_logs_and_failure_status(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                self.assertEqual(self.invoke("--model", "custom-model", backend=backend,
                                             env=self.env | {"FAKE_CODEX_RC": "7"}), 7)
                argv = self.sent_argv()
                if backend == "claude":
                    self.assertEqual(argv, ["--print", "--model", "custom-model", "--output-format",
                                            "text", "--permission-mode", "default", "yo"])
                else:
                    self.assertEqual(argv, codex_runner.codex_command("wrapper",
                        {**codex_runner.DEFAULTS, "model": "custom-model"},
                        self.log_dir / "yo-wrapper.last.txt")[1:])
                log = codex_runner.run_logs("wrapper", self.log_dir)[-1].read_text()
                self.assertIn("yo start", log)
                self.assertIn(f"agent=wrapper backend={backend} model=custom-model", log)
                self.assertTrue(log.rstrip().endswith("yo end rc=7"))
                self.assertEqual((self.log_dir / "yo-wrapper.last.txt").read_text(), "391\n")
                self.assertEqual(codex_runner.load_records("wrapper", self.log_dir), [])

    def test_claude_run_log_is_terminated_even_when_the_last_message_file_fails(self):
        last = self.log_dir / "yo-wrapper.last.txt"
        self.log_dir.mkdir(parents=True)
        last.write_text("stale")
        last.chmod(0o444)
        self.addCleanup(last.chmod, 0o644)
        self.assertEqual(self.invoke(backend="claude"), 1)  # invoke() asserts stderr stayed empty
        self.assertFalse(self.argv.exists())  # nothing was sent
        log = codex_runner.run_logs("wrapper", self.log_dir)[-1].read_text()
        self.assertIn("Permission denied", log)
        self.assertTrue(log.rstrip().endswith("yo end rc=1"), log)

    def test_probed_ping_records_and_open_window_skips(self):
        with mock.patch.object(anchor.time, "sleep"):
            self.assertEqual(self.invoke("--probe", "--model", "gpt-5.6-luna",
                                         "--thread-source", "scheduled"), 0)
        argv = self.sent_argv()
        self.assertEqual((argv[-1], argv[argv.index("--thread-source") + 1]), ("yo", "scheduled"))
        (record,) = codex_runner.load_records("wrapper", self.log_dir)
        self.assertEqual((record["source"], record["outcome"], record["returncode"], record["cli_version"]),
                         ("manual", "observation_error", 0, "codex-cli test-version"))
        self.assertEqual(record["effective"], {"model": "gpt-5.6-luna", "effort": "medium",
                                               "thread_source": "scheduled", "prompt": "yo"})
        self.assertIn("fixture has no quota api", record["read_error"])
        log = codex_runner.run_logs("wrapper", self.log_dir)[-1].read_text()
        self.assertLess(log.index("yo end rc=0"), log.index("manual ping: observation_error"))
        self.assertTrue(log.rstrip().splitlines()[-1].startswith("record: "))
        quota = json.dumps({"usedPercent": 12, "windowDurationMins": 300, "resetsAt": time.time() + 7200})
        self.assertEqual(self.invoke("--probe", env=self.env | {"FAKE_CODEX_QUOTA": quota}), 0)
        self.assertFalse(self.argv.exists())
        self.assertEqual(codex_runner.load_records("wrapper", self.log_dir)[-1]["outcome"], "window_open")

    def test_registered_command_takes_backend_and_overrides_from_settings(self):
        parser = settings.load()
        settings.update_command(parser, "wrapper", {"backend": "codex", "model": "gpt-5.6-luna", "tz": "Asia/Tokyo"})
        settings.save(parser)
        with mock.patch.object(codex_runner, "run", return_value=0) as codex, \
                mock.patch.object(claude_runner, "run", return_value=0) as claude:
            self.assertEqual(yo.main(["wrapper"]), 0)
            args = codex.call_args.args[0]
            self.assertEqual((args.backend, args.model, args.effort, args.override_source, args.tz),
                             ("codex", "gpt-5.6-luna", "", "settings", "Asia/Tokyo"))
            yo.main(["wrapper", "--model", "m"])  # a flag wins for this run
            args = codex.call_args.args[0]
            self.assertEqual((args.model, args.override_source), ("m", "manual"))
            yo.main(["wrapper", "--backend", "claude"])  # so does an explicit backend
            self.assertEqual(claude.call_args.args[0].backend, "claude")
            # An unregistered command still needs --backend, and the error says how to register it.
            with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                yo.main(["other"])
            self.assertIn("install.py --command other --backend", err.getvalue())
            # A misspelt backend in the file is a usage error, never a silent fallback to a runner.
            settings.update_command(parser, "wrapper", {"backend": "cdoex"})
            settings.save(parser)
            with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exc:
                yo.main(["wrapper"])
            self.assertEqual(exc.exception.code, 2)
            self.assertIn("backend 'cdoex' for command 'wrapper' is not one of codex, claude", err.getvalue())
            self.assertEqual((codex.call_count, claude.call_count), (2, 1))
            # [DEFAULT] tz applies to unregistered commands too.
            parser[settings.DEFAULT_SECTION]["tz"] = "UTC"
            settings.save(parser)
            yo.main(["codex"])
            self.assertEqual(codex.call_args.args[0].tz, "UTC")

    def test_executable_on_path_works_outside_the_repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        for name in ("yo", "install.py"):
            shutil.copy(REPO_ROOT / name, repo / name)
        shutil.copytree(REPO_ROOT / "utils", repo / "utils", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "yo").symlink_to(repo / "yo")
        # self.env carries the scratch HOME, so the subprocess's ~/.yo/ is root/.yo (never the real one).
        result = subprocess.run(["yo", "wrapper", "--backend", "codex"], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.sent_argv()[-1], "yo")
        (log,) = (self.root / ".yo" / "logs").glob("*.log")
        self.assertIn(f"root={repo.resolve()}", log.read_text())
        self.assertFalse((repo / "logs").exists())


if __name__ == "__main__":
    unittest.main()
