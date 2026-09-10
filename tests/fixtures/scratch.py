"""Shared test scaffolding: a scratch directory, auto-undone mocks, and the fake Codex CLI."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CODEX = Path(__file__).resolve().with_name("fake_codex.py")


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

    def install_fake_cli(self):
        """The fake Codex CLI on PATH as `wrapper`, recording its argv to a file."""
        cli = self.root / "wrapper"
        shutil.copy(FAKE_CODEX, cli)
        cli.chmod(0o755)
        self.argv = self.root / "argv.json"
        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}", FAKE_CODEX_ARGV=str(self.argv))
        self.env.pop("CODEX_HOME", None)

    def sent_argv(self):
        argv = json.loads(self.argv.read_text())
        self.argv.unlink()
        return argv
