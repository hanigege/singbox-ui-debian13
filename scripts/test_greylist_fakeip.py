#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "singbox-rule-ui"))

import app
import bootstrap_config


class GreylistFakeipTests(unittest.TestCase):
    def test_default_ranges_match_production_and_bootstrap(self):
        self.assertEqual(app.DEFAULT_FAKEIP4, "198.18.0.0/15")
        self.assertEqual(app.DEFAULT_FAKEIP6, "2001:2::198:18:0:0/96")
        self.assertEqual(bootstrap_config.DEFAULT_FAKE4, app.DEFAULT_FAKEIP4)
        self.assertEqual(bootstrap_config.DEFAULT_FAKE6, app.DEFAULT_FAKEIP6)

    def test_cache_file_follows_fakeip_ranges(self):
        legacy = {"fakeip": {"inet4_range": "28.0.0.0/8", "inet6_range": "2001:2::/64"}}
        default = {"fakeip": {"inet4_range": "198.18.0.0/15", "inet6_range": "2001:2::198:18:0:0/96"}}
        legacy_config = {}
        default_config = {}
        app.apply_cache_file_settings(legacy_config, legacy)
        app.apply_cache_file_settings(default_config, default)
        legacy_path = legacy_config["experimental"]["cache_file"]["path"]
        default_path = default_config["experimental"]["cache_file"]["path"]
        self.assertRegex(legacy_path, r"^/etc/sing-box/cache-fakeip-[0-9a-f]{16}\.db$")
        self.assertEqual(default_path, "/etc/sing-box/cache.db")
        self.assertNotEqual(legacy_path, default_path)
        self.assertEqual(app.fakeip_cache_name(default["fakeip"]), "cache.db")
        self.assertEqual(app.fakeip_cache_name(legacy["fakeip"]), legacy_path.rsplit("/", 1)[1])

    def test_rejects_current_fakeip_and_overlapping_ranges(self):
        groups = {"fakeip": {"inet4_range": "198.18.0.0/15", "inet6_range": "2001:2::198:18:0:0/96"}}
        with patch.object(app, "first_default_interface", return_value=""), patch.object(app, "resolved_outbound_servers", return_value=[]):
            for cidr in ("198.18.0.0/15", "198.18.1.0/24", "198.16.0.0/13", "2001:2::198:18:0:0/96", "2001:2::/64"):
                with self.subTest(cidr=cidr), self.assertRaisesRegex(ValueError, "overlaps FakeIP range"):
                    app.validate_greylist_ip_cidrs([{"type": "ip_cidr", "value": cidr}], nodes=[], groups=groups)

    def test_uses_updated_fakeip_ranges(self):
        groups = {"fakeip": {"inet4_range": "198.18.0.0/15", "inet6_range": "2001:db8:2::/64"}}
        with patch.object(app, "first_default_interface", return_value=""), patch.object(app, "resolved_outbound_servers", return_value=[]):
            app.validate_greylist_ip_cidrs([{"type": "ip_cidr", "value": "28.0.0.0/8"}], nodes=[], groups=groups)
            for cidr in ("198.18.1.0/24", "2001:db8:2::/64"):
                with self.subTest(cidr=cidr), self.assertRaisesRegex(ValueError, "overlaps FakeIP range"):
                    app.validate_greylist_ip_cidrs([{"type": "ip_cidr", "value": cidr}], nodes=[], groups=groups)


if __name__ == "__main__":
    unittest.main()
