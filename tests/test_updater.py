#!/usr/bin/env python3
"""
Unit tests for Plex Media Server macOS Auto-Updater.
Run with: python3 -m unittest discover tests
"""

import hashlib
import json
import os
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plex_updater


class TestPlexUpdater(unittest.TestCase):

    def test_parse_version_string(self):
        # 4-part with git hash
        v1 = plex_updater.parse_version_string("1.43.4.10903-e5521bd8c")
        self.assertEqual(v1, (1, 43, 4, 10903))

        # 3-part semver
        v2 = plex_updater.parse_version_string("1.42.2")
        self.assertEqual(v2, (1, 42, 2, 0))

        # Leading 'v'
        v3 = plex_updater.parse_version_string("v1.40.1.8000")
        self.assertEqual(v3, (1, 40, 1, 8000))

        # None / empty / invalid
        self.assertIsNone(plex_updater.parse_version_string(None))
        self.assertIsNone(plex_updater.parse_version_string(""))
        self.assertIsNone(plex_updater.parse_version_string("invalid"))

    def test_is_version_newer(self):
        # Newer build
        self.assertTrue(plex_updater.is_version_newer("1.43.4.10903-e5521bd8c", "1.42.2.10156-f737b826c"))
        self.assertTrue(plex_updater.is_version_newer("1.43.5.1000", "1.43.4.9999"))

        # Same version
        self.assertFalse(plex_updater.is_version_newer("1.43.4.10903-e5521bd8c", "1.43.4.10903-e5521bd8c"))

        # Older version
        self.assertFalse(plex_updater.is_version_newer("1.42.0.1000", "1.43.0.2000"))

    def test_fetch_latest_release_parsing(self):
        mock_api_payload = {
            "computer": {
                "MacOS": {
                    "version": "1.43.4.10903-e5521bd8c",
                    "items_added": "Feature A",
                    "items_fixed": "Bug B",
                    "releases": [
                        {
                            "build": "darwin-x86_64",
                            "distro": "macos",
                            "url": "https://downloads.plex.tv/macos/PlexMediaServer-1.43.4-universal.zip",
                            "checksum": "00cb7ec1ed95b503db9d5a07a0055a9e4e9be952",
                            "release_date": 1786974203,
                        }
                    ],
                }
            }
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_api_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            result = plex_updater.fetch_latest_release(channel="public")
            self.assertEqual(result["version"], "1.43.4.10903-e5521bd8c")
            self.assertEqual(result["url"], "https://downloads.plex.tv/macos/PlexMediaServer-1.43.4-universal.zip")
            self.assertEqual(result["checksum"], "00cb7ec1ed95b503db9d5a07a0055a9e4e9be952")
            self.assertEqual(result["items_added"], "Feature A")

    def test_detect_plex_token(self):
        # 1. Configured token takes priority
        token = plex_updater.detect_plex_token("my_configured_token")
        self.assertEqual(token, "my_configured_token")

        # 2. Environment variable
        with patch.dict(os.environ, {"PLEX_TOKEN": "env_token_123"}):
            token = plex_updater.detect_plex_token("")
            self.assertEqual(token, "env_token_123")

        # 3. Preferences.xml
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write('<Preferences MachineIdentifier="xyz" PlexOnlineToken="xml_token_456" />')
            f_path = f.name

        try:
            with patch.dict(os.environ, {}, clear=True), patch("plex_updater.PLEX_PREFERENCES_PATH", f_path):
                token = plex_updater.detect_plex_token("")
                self.assertEqual(token, "xml_token_456")
        finally:
            os.remove(f_path)

    def test_checksum_verification(self):
        test_content = b"Plex Media Server Mock Binary Content"
        expected_sha1 = hashlib.sha1(test_content).hexdigest()

        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f_path = f.name

        try:
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.side_effect = [test_content, b""]
                mock_resp.headers = {"Content-Length": str(len(test_content))}
                mock_resp.__enter__.return_value = mock_resp
                mock_urlopen.return_value = mock_resp

                # Valid checksum
                plex_updater.download_file_with_checksum("https://fake.url/file.zip", expected_sha1, f_path)

                # Invalid checksum should raise ValueError
                mock_resp.read.side_effect = [test_content, b""]
                with self.assertRaises(ValueError):
                    plex_updater.download_file_with_checksum(
                        "https://fake.url/file.zip", "badchecksum00000000000000000000000000", f_path
                    )
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_generate_launchagent_plist(self):
        config_interval = {
            "schedule": {
                "type": "interval",
                "interval_hours": 4,
            },
            "log_file": "/tmp/plex.log",
        }
        plist = plex_updater.generate_launchagent_plist("/path/to/plex_updater.py", config_interval)
        self.assertEqual(plist["Label"], "com.plex.macos-auto-updater")
        self.assertEqual(plist["StartInterval"], 4 * 3600)
        self.assertIn("/path/to/plex_updater.py", plist["ProgramArguments"])
        self.assertEqual(plist["StandardOutPath"], "/tmp/plex.log")
        self.assertEqual(plist["StandardErrorPath"], "/tmp/plex.error.log")

        config_calendar = {
            "schedule": {
                "type": "calendar",
                "calendar_hour": 3,
                "calendar_minute": 30,
            },
            "log_file": "/tmp/plex.log",
        }
        plist_cal = plex_updater.generate_launchagent_plist("/path/to/plex_updater.py", config_calendar)
        self.assertIn("StartCalendarInterval", plist_cal)
        self.assertEqual(plist_cal["StartCalendarInterval"]["Hour"], 3)
        self.assertEqual(plist_cal["StartCalendarInterval"]["Minute"], 30)

    def test_active_stream_count(self):
        # When Plex is not running, stream count is 0
        with patch("plex_updater.is_plex_running", return_value=False):
            count = plex_updater.get_active_stream_count(token="test")
            self.assertEqual(count, 0)

        # When Plex is running and reports 2 active sessions
        with patch("plex_updater.is_plex_running", return_value=True), patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"MediaContainer": {"size": 2}}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            count = plex_updater.get_active_stream_count(token="test")
            self.assertEqual(count, 2)

    def test_get_installed_version_from_app(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            contents_dir = os.path.join(tmpdir, "Contents")
            os.makedirs(contents_dir)
            plist_path = os.path.join(contents_dir, "Info.plist")

            # Write Info.plist
            mock_plist = {
                "CFBundleShortVersionString": "1.41.0.8992-8463b6070",
                "CFBundleVersion": "1.41.0",
            }
            with open(plist_path, "wb") as f:
                plistlib.dump(mock_plist, f)

            ver = plex_updater.get_installed_version_from_app(tmpdir)
            self.assertEqual(ver, "1.41.0.8992-8463b6070")

    def test_dry_run_workflow(self):
        config = {
            "app_path": "/fake/Applications/Plex Media Server.app",
            "channel": "public",
            "token": "",
            "check_active_streams": False,
        }

        mock_release = {
            "version": "1.43.4.10903",
            "url": "https://example.com/plex.zip",
            "checksum": "abc123",
        }

        with patch("plex_updater.check_updates", return_value=(True, mock_release, "1.40.0")):
            status = plex_updater.perform_update(config, dry_run=True)
            self.assertEqual(status, 0)

    def test_active_stream_deferral(self):
        config = {
            "app_path": "/fake/Applications/Plex Media Server.app",
            "channel": "public",
            "token": "fake_token",
            "check_active_streams": True,
            "notify": False,
        }

        mock_release = {
            "version": "1.43.4.10903",
            "url": "https://example.com/plex.zip",
            "checksum": "abc123",
        }

        # Simulate 1 active stream
        with patch("plex_updater.check_updates", return_value=(True, mock_release, "1.40.0")), \
             patch("plex_updater.get_active_stream_count", return_value=1):
            status = plex_updater.perform_update(config, dry_run=False, ignore_streams=False)
            self.assertEqual(status, 2)  # 2 = Deferral status code


if __name__ == "__main__":
    unittest.main()

