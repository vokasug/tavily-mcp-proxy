#!/usr/bin/env python3
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import quota_checker


class QuotaCheckerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="tavily-mcp-quota-test-")
        self.config_dir = Path(self.tempdir.name)
        self.keys_file = self.config_dir / "tavily-keys.list"
        self.active_key = self.config_dir / "active-key"
        self.active_status = self.config_dir / "active-key.status"
        self.keys_file.write_text("tvly-test\n")
        self.active_key.write_text("tvly-test\n")
        self.active_status.write_text("ok\n")
        self.paths = mock.patch.multiple(
            quota_checker,
            KEYS_FILE=self.keys_file,
            ACTIVE_KEY_FILE=self.active_key,
            ACTIVE_KEY_STATUS=self.active_status,
        )
        self.paths.start()
        self.sleep = mock.patch.object(quota_checker.time, "sleep").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tempdir.cleanup()

    def test_temporary_failure_preserves_active_key(self):
        with mock.patch.object(
            quota_checker.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ) as urlopen:
            self.assertEqual(quota_checker.main(), 2)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 5])
        self.assertEqual(self.active_key.read_text(), "tvly-test\n")
        self.assertEqual(self.active_status.read_text(), "ok\n")

    def test_temporary_failure_without_active_key_fails_loud(self):
        self.active_key.write_text("")
        self.active_status.write_text("unknown\n")
        with mock.patch.object(
            quota_checker.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            self.assertEqual(quota_checker.main(), 2)

        self.assertEqual(self.active_key.read_text(), "")
        self.assertEqual(self.active_status.read_text(), "unknown\n")

    def test_permanent_failure_clears_active_key_without_retries(self):
        error = urllib.error.HTTPError(
            quota_checker.USAGE_URL, 401, "unauthorized", {}, None,
        )
        with mock.patch.object(
            quota_checker.urllib.request, "urlopen", side_effect=error,
        ) as urlopen:
            self.assertEqual(quota_checker.main(), 2)

        self.assertEqual(urlopen.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(self.active_key.read_text(), "")
        self.assertEqual(self.active_status.read_text(), "unknown\n")

    def test_retry_recovers_and_selects_key(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps({
            "account": {"plan_limit": 1000, "plan_usage": 25},
        }).encode()
        with mock.patch.object(
            quota_checker.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("timeout"), response],
        ) as urlopen:
            self.assertEqual(quota_checker.main(), 0)

        self.assertEqual(urlopen.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(self.active_key.read_text(), "tvly-test\n")
        self.assertEqual(self.active_status.read_text(), "ok\n")


if __name__ == "__main__":
    unittest.main()
