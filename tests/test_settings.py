"""The settings file: round trip, updates, and where it lives."""
import unittest

from tests.helpers import ScratchCase
from utils import settings


class SettingsFileTests(ScratchCase):
    def test_round_trip(self):
        parser = settings.load()  # no file yet
        settings.update_command(parser, "codex-pro", {"backend": "codex", "tz": "Europe/Paris", "hours": "9-18",
                                                      "window_hours": 5, "num_windows": 3, "probe": None})
        settings.update_command(parser, "claude-perso", {"backend": "claude", "hours": "19-23", "probe": False})
        settings.save(parser)
        self.assertEqual(settings.settings_path().read_text(),
                         "[codex-pro]\nbackend = codex\ntz = Europe/Paris\nhours = 9-18\nwindow_hours = 5\n"
                         "num_windows = 3\n\n"
                         "[claude-perso]\nbackend = claude\nhours = 19-23\nprobe = false\n\n")
        again = settings.load()
        self.assertEqual(again.sections(), ["codex-pro", "claude-perso"])
        self.assertEqual(dict(again["codex-pro"]), {"backend": "codex", "tz": "Europe/Paris", "hours": "9-18",
                                                    "window_hours": "5", "num_windows": "3"})
        self.assertIs(again["claude-perso"].getboolean("probe"), False)
        self.assertEqual(again["codex-pro"].getint("num_windows"), 3)

    def test_update_overwrites_only_the_given_keys(self):
        parser = settings.load()
        settings.update_command(parser, "codex", {"backend": "codex", "hours": "9-18"})
        settings.update_command(parser, "codex", {"hours": "19-23", "backend": None, "model": "gpt-5.6-luna"})
        self.assertEqual(dict(parser["codex"]), {"backend": "codex", "hours": "19-23", "model": "gpt-5.6-luna"})

    def test_paths_follow_home_and_save_creates_the_directory(self):
        self.assertEqual(settings.home_dir(), self.root / ".yo")
        self.assertEqual(settings.log_dir(), self.root / ".yo" / "logs")
        self.assertFalse(settings.home_dir().exists())
        settings.save(settings.load())
        self.assertEqual(settings.settings_path().read_text(), "")
        self.assertFalse(list(settings.home_dir().glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
