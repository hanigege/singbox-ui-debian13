#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "singbox-rule-ui"))

import app
from auto_proxy_learning import advance_state, empty_state, normalize_domain, normalize_state, parse_direct_network_failure, record_probe


class AutoProxyLearningTests(unittest.TestCase):
    def test_only_retryable_direct_https_failures_are_candidates(self):
        direct = "+0000 2026-11-06 01:34:46 ERROR [1 40s] connection: open connection to init.push.apple.com:443 using outbound/direct[direct]: dial tcp 17.188.1.1:443: i/o timeout"
        failed_proxy = "+0000 2026-11-05 13:55:01 ERROR [1 82ms] connection: open connection to 149.154.167.92:443 using outbound/vless[EdgeTunnel-vless]: read: connection reset by peer"
        intentional_block = "+0000 2026-11-06 01:34:46 ERROR [1 1ms] connection: open connection to example.com:443 using outbound/block[block]: rejected"
        non_https = "+0000 2026-11-06 01:34:46 ERROR [1 20s] connection: open connection to courier.example.com:5223 using outbound/direct[direct]: i/o timeout"
        self.assertEqual(parse_direct_network_failure(direct), ("init.push.apple.com", 443))
        self.assertIsNone(parse_direct_network_failure(failed_proxy))
        self.assertIsNone(parse_direct_network_failure(intentional_block))
        self.assertIsNone(parse_direct_network_failure(non_https))

    def test_domain_normalization_rejects_ip_and_lan_names(self):
        self.assertEqual(normalize_domain("Example.COM."), "example.com")
        self.assertIsNone(normalize_domain("10.0.0.1"))
        self.assertIsNone(normalize_domain("printer.local"))
        self.assertIsNone(normalize_domain("localhost"))

    def test_one_direct_failure_triggers_a_proxy_probe(self):
        state = empty_state()
        state, ready = advance_state(state, ["blocked.example.com"], 1000)
        self.assertEqual(ready, ["blocked.example.com"])
        state = record_probe(state, "blocked.example.com", 1000, True)
        self.assertEqual(state["learned"]["blocked.example.com"]["expiresAt"], 0)
        state, ready = advance_state(state, [], 1000 + 365 * 24 * 60 * 60)
        self.assertEqual(ready, [])
        self.assertIn("blocked.example.com", state["learned"])

    def test_legacy_expiring_entries_migrate_to_permanent(self):
        state = normalize_state({
            "version": 1,
            "enabled": True,
            "cursor": None,
            "candidates": {},
            "learned": {"blocked.example.com": {"learnedAt": 1000, "expiresAt": 87400}},
        })
        self.assertEqual(state["learned"]["blocked.example.com"]["expiresAt"], 0)

    def test_learned_domains_are_not_evicted_by_a_count_limit(self):
        state = empty_state()
        state["learned"] = {
            f"d{index:04}.example.net": {"learnedAt": index + 1, "expiresAt": 0}
            for index in range(500)
        }
        state = record_probe(state, "new.example.net", 1000, True)
        self.assertEqual(len(state["learned"]), 501)
        self.assertIn("d0000.example.net", state["learned"])

    def test_candidates_expire_after_the_failure_window(self):
        state = empty_state()
        state, _ = advance_state(state, ["blocked.example.com"], 1000)
        state, ready = advance_state(state, [], 1000 + 10 * 60 + 1)
        self.assertEqual(ready, [])
        self.assertNotIn("blocked.example.com", state["candidates"])

    def test_rules_respect_manual_direct_and_ddns_overrides(self):
        config = {
            "dns": {"rules": [
                {"rule_set": "custom-blacklist", "action": "reject"},
                {"rule_set": "custom-whitelist", "action": "route", "server": "local-dns"},
                {"rule_set": "custom-ddns", "action": "route", "server": "local-dns"},
                {"rule_set": "custom-greylist", "action": "route", "server": "fakeip-dns"},
                {"rule_set": "geosite-cn", "action": "route", "server": "local-dns"},
            ]},
            "route": {"rules": [
                {"rule_set": "custom-blacklist", "outbound": "block"},
                {"rule_set": "custom-whitelist", "outbound": "direct"},
                {"rule_set": "custom-ddns", "outbound": "direct"},
                {"rule_set": "custom-greylist", "outbound": "Proxy"},
                {"rule_set": "geosite-speedtest", "outbound": "direct"},
                {"rule_set": "geosite-telegram", "outbound": "Proxy"},
                {"rule_set": "geosite-geolocation-cn", "outbound": "direct"},
            ]},
        }
        app.apply_auto_proxy_domain_rules(config, {"fakeip": {"ipv6_enabled": False}})
        route = config["route"]["rules"]
        auto_rule = next(rule for rule in route if rule.get("rule_set") == app.AUTO_PROXY_RULE_SET_TAG)
        self.assertLess(route.index(route[4]), route.index(auto_rule))
        self.assertEqual(route[route.index(auto_rule) + 1]["rule_set"], "geosite-telegram")
        dns = config["dns"]["rules"]
        auto_dns = next(rule for rule in dns if rule.get("rule_set") == app.AUTO_PROXY_RULE_SET_TAG)
        self.assertEqual(auto_dns["query_type"], ["A"])
        self.assertLess(dns.index(dns[3]), dns.index(auto_dns))

    def test_auto_rule_set_uses_sing_box_source_format(self):
        path = Path(__file__).resolve().parent / "auto-proxy-test.json"
        try:
            app.write_auto_proxy_rule_set(["RFA.org", "www.rfa.org"], path)
            self.assertEqual(app.load_json(path, {}), {
                "version": 5,
                "rules": [{"domain": ["rfa.org", "www.rfa.org"]}],
            })
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
