#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "singbox-rule-ui"))

import app


class GreylistFakeipTests(unittest.TestCase):
    def test_cache_file_follows_fakeip_ranges(self):
        old = {"fakeip": {"inet4_range": "28.0.0.0/8", "inet6_range": "2001:2::/64"}}
        new = {"fakeip": {"inet4_range": "198.18.0.0/15", "inet6_range": "2001:2::198:18:0:0/96"}}
        old_config = {}
        new_config = {}
        app.apply_cache_file_settings(old_config, old)
        app.apply_cache_file_settings(new_config, new)
        old_path = old_config["experimental"]["cache_file"]["path"]
        new_path = new_config["experimental"]["cache_file"]["path"]
        self.assertEqual(old_path, "/etc/sing-box/cache.db")
        self.assertRegex(new_path, r"^/etc/sing-box/cache-fakeip-[0-9a-f]{16}\.db$")
        self.assertNotEqual(old_path, new_path)
        self.assertEqual(app.fakeip_cache_name(old["fakeip"]), "cache.db")
        self.assertEqual(app.fakeip_cache_name(new["fakeip"]), new_path.rsplit("/", 1)[1])

    def test_rejects_current_fakeip_and_overlapping_ranges(self):
        groups = {"fakeip": {"inet4_range": "28.0.0.0/8", "inet6_range": "2001:2::/64"}}
        with patch.object(app, "first_default_interface", return_value=""), patch.object(app, "resolved_outbound_servers", return_value=[]):
            for cidr in ("28.0.0.0/8", "28.1.0.0/16", "24.0.0.0/5", "2001:2::/64", "2001:2::/63"):
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
