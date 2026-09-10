"""The settings file: round trip, [DEFAULT] inheritance, minimal overrides, removal."""
import unittest

from tests.helpers import ScratchCase
from utils import settings


class SettingsFileTests(ScratchCase):
    def test_round_trip_with_default_inheritance(self):
        parser = settings.load()  # no file yet
        self.assertEqual(settings.commands(parser), [])
        settings.seed_defaults(parser, {"tz": "Europe/Paris", "hours": "9-18", "window_hours": 5, "num_windows": 3})
        settings.update_command(parser, "codex-pro", {"backend": "codex", "hours": "9-18", "probe": None})
        settings.update_command(parser, "claude-perso", {"backend": "claude", "hours": "19-23", "probe": False})
        settings.save(parser)
        self.assertEqual(settings.settings_path().read_text(),
                         "[DEFAULT]\ntz = Europe/Paris\nhours = 9-18\nwindow_hours = 5\nnum_windows = 3\n\n"
                         "[codex-pro]\nbackend = codex\n\n"
                         "[claude-perso]\nbackend = claude\nhours = 19-23\nprobe = false\n\n")
        again = settings.load()
        self.assertEqual(settings.commands(again), ["codex-pro", "claude-perso"])
        self.assertEqual(settings.command_settings(again, "codex-pro"),
                         {"tz": "Europe/Paris", "hours": "9-18", "window_hours": "5", "num_windows": "3",
                          "backend": "codex"})
        self.assertEqual(settings.command_settings(again, "claude-perso")["hours"], "19-23")
        self.assertIsNone(settings.command_settings(again, "other"))
        self.assertEqual(settings.defaults(again)["num_windows"], "3")

    def test_overrides_stay_minimal_and_defaults_are_seeded_once(self):
        parser = settings.load()
        settings.seed_defaults(parser, {"hours": "9-18", "tz": None})
        settings.seed_defaults(parser, {"hours": "8-17"})  # already set: kept
        self.assertEqual(settings.defaults(parser), {"hours": "9-18"})
        settings.update_command(parser, "codex", {"backend": "codex", "hours": "19-23"})
        self.assertIn("[codex]\nbackend = codex\nhours = 19-23\n", settings.dump(parser))
        settings.update_command(parser, "codex", {"hours": "9-18"})  # back to the default: override dropped
        self.assertEqual(settings.dump(parser).count("hours"), 1)
        self.assertEqual(settings.command_settings(parser, "codex")["hours"], "9-18")

    def test_remove_and_bool(self):
        parser = settings.load()
        settings.update_command(parser, "codex", {"backend": "codex"})
        self.assertTrue(settings.remove_command(parser, "codex"))
        self.assertFalse(settings.remove_command(parser, "codex"))
        self.assertEqual(settings.commands(parser), [])
        for text, expected in (("true", True), ("Yes", True), ("1", True), ("on", True),
                               ("false", False), ("no", False), ("", False), ("maybe", False)):
            self.assertEqual(settings.to_bool(text), expected, text)
        self.assertEqual((settings.format_value(True), settings.format_value(False), settings.format_value(5)),
                         ("true", "false", "5"))

    def test_save_creates_the_directory(self):
        nested = self.root / "nested" / ".yo"
        self.patch(settings, "HOME_DIR", nested)
        self.assertEqual(settings.log_dir(), nested / "logs")
        settings.save(settings.load())
        self.assertEqual(settings.settings_path().read_text(), "")
        self.assertFalse(list(nested.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
