#!/usr/bin/env python3
"""Unit tests for install helpers."""
import unittest
from unittest import mock

import install
from install import BASE_CRON_PATH, cron_path_for, hour_minute


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
            with self.assertRaises(SystemExit):
                cron_path_for(["claude"])


if __name__ == "__main__":
    unittest.main()
