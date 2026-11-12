#!/usr/bin/env python3
"""验证 HTTPS 域名发现、真实学习文件更新，以及外部探测失败的回滚。"""

import fcntl
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "singbox-rule-ui"))
import app
from auto_proxy_learning import empty_state

with patch.dict(os.environ, {"RULE_UI_APP_DIR": str(ROOT / "singbox-rule-ui")}):
    spec = importlib.util.spec_from_file_location("auto_proxy_watch", ROOT / "scripts/auto_proxy_watch.py")
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)


def connection() -> dict:
    return {
        "metadata": {"network": "tcp", "destinationPort": "443", "host": "Blocked.Example.COM."},
        "chains": ["direct"],
        "rule": "rule_set=[geosite-cn geosite-geolocation-cn geoip-cn] => route(direct)",
    }


class DomainDiscoveryTests(unittest.TestCase):
    def test_direct_cn_https_domain_is_normalized(self):
        self.assertEqual(watch.direct_cn_domain(connection()), "blocked.example.com")

    def test_manual_routes_proxy_flows_and_non_https_are_excluded(self):
        for rule in ("custom-whitelist", "custom-ddns", "geosite-speedtest"):
            value = connection()
            value["rule"] = f"rule_set={rule} => route(direct)"
            self.assertIsNone(watch.direct_cn_domain(value))
        for key, value in (("network", "udp"), ("destinationPort", "80"), ("host", "printer.local")):
            item = connection()
            item["metadata"][key] = value
            self.assertIsNone(watch.direct_cn_domain(item))
        item = connection()
        item["chains"] = ["Proxy"]
        self.assertIsNone(watch.direct_cn_domain(item))

    def test_malformed_runtime_data_is_not_a_candidate(self):
        for value in (None, [], {}, {"metadata": "invalid"}, {"metadata": {}, "chains": [], "rule": 1}):
            self.assertIsNone(watch.direct_cn_domain(value))


@unittest.skipUnless(Path("/usr/local/bin/sing-box").is_file(), "需要测试机上的真实 sing-box 编译和配置校验")
class ProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="https-learning-test-")))
        manager = self.root / "manager"
        paths = {
            "MANAGER_DIR": manager,
            "BACKUP_DIR": manager / "backups",
            "CONFIG_PATH": self.root / "config.json",
            "BASE_CONFIG_PATH": manager / "base.json",
            "NODES_PATH": manager / "nodes.json",
            "GROUPS_PATH": manager / "groups.json",
            "AUTO_PROXY_STATE_PATH": manager / "auto-proxy-learning.json",
            "AUTO_PROXY_RULE_SET_PATH": manager / "auto-proxy-learned.json",
            "RULE_DIR": self.root / "rules",
            "TOKEN_FILE": self.root / "token",
        }
        original_check = app.check_config
        self.stack.enter_context(patch.multiple(app, **paths))
        self.stack.enter_context(patch.object(app, "check_config", side_effect=lambda: original_check(paths["CONFIG_PATH"])))
        self.stack.enter_context(patch.object(watch, "LOCK_PATH", self.root / "learning.lock"))
        app.ensure_dirs()
        for path, value in ((app.BASE_CONFIG_PATH, {}), (app.NODES_PATH, []), (app.GROUPS_PATH, {}), (app.AUTO_PROXY_STATE_PATH, empty_state())):
            app.write_json(path, value)
        for name in app.LISTS:
            app.write_json(app.rule_path(name), app.entries_to_rule_set([]))
        app.write_auto_proxy_rule_set([], app.AUTO_PROXY_RULE_SET_PATH)
        app.write_json(app.CONFIG_PATH, {
            "log": {"disabled": True},
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "route": {
                "rule_set": [{"type": "local", "tag": app.AUTO_PROXY_RULE_SET_TAG, "format": "source", "path": str(app.AUTO_PROXY_RULE_SET_PATH)}],
                "rules": [{"rule_set": app.AUTO_PROXY_RULE_SET_TAG, "outbound": "direct"}],
                "final": "direct",
            },
        })
        self.assertEqual(app.check_config()["code"], 0)

    def network(self, results):
        # URL 探测是结果不确定的外部网络边界；状态、规则编译和配置校验均使用真实实现。
        return self.stack.enter_context(patch.object(app, "test_node_delay", side_effect=results))

    def failed(self):
        return {"ok": False, "delay": None, "error": "TLS EOF"}

    def reachable(self):
        return {"ok": True, "delay": 100, "error": None}

    def test_two_direct_failures_and_proxy_success_hot_reload_learning(self):
        before = app.CONFIG_PATH.read_bytes()
        probe = self.network([self.failed(), self.failed(), self.reachable()])
        result = watch.probe_domain("blocked.example.com")
        self.assertEqual(result["status"], "learned")
        self.assertEqual([call.args[0] for call in probe.call_args_list], ["direct", "direct", "Proxy"])
        self.assertIn("blocked.example.com", app.load_auto_proxy_state()["learned"])
        self.assertIn("blocked.example.com", app.load_json(app.AUTO_PROXY_RULE_SET_PATH, {})["rules"][0]["domain"])
        self.assertEqual(app.CONFIG_PATH.read_bytes(), before)

    def test_recovered_direct_connection_does_not_learn(self):
        self.network([self.failed(), self.reachable()])
        self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "direct-ok")
        self.assertEqual(app.load_auto_proxy_state()["learned"], {})

    def test_proxy_failure_stays_unlearned_and_enters_cooldown(self):
        self.network([self.failed(), self.failed(), self.failed()])
        self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "proxy-failed")
        state = app.load_auto_proxy_state()
        self.assertEqual(state["learned"], {})
        self.assertGreater(state["candidates"]["blocked.example.com"]["lastProbeAt"], 0)
        self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "cooldown")

    def test_manual_override_skips_all_probes(self):
        app.write_json(app.rule_path("whitelist"), app.entries_to_rule_set([{"type": "domain_suffix", "value": "example.com"}]))
        probe = self.network([])
        self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "manual")
        probe.assert_not_called()

    def test_pause_during_external_probe_preserves_latest_state(self):
        def probe(tag, **kwargs):
            if tag == "Proxy":
                state = app.load_auto_proxy_state()
                state["enabled"] = False
                state["learned"]["kept.example.org"] = {"learnedAt": 1000, "expiresAt": 0}
                app.write_json(app.AUTO_PROXY_STATE_PATH, state)
                return self.reachable()
            return self.failed()
        self.network(probe)
        self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "disabled")
        state = app.load_auto_proxy_state()
        self.assertEqual(sorted(state["learned"]), ["kept.example.org"])

    def test_shared_lock_blocks_a_concurrent_learning_write(self):
        with watch.LOCK_PATH.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe = self.network([])
            self.assertEqual(watch.probe_domain("blocked.example.com")["status"], "busy")
            probe.assert_not_called()

    def test_invalid_config_rolls_back_rule_set_and_learning(self):
        before = app.AUTO_PROXY_RULE_SET_PATH.read_bytes()
        app.CONFIG_PATH.write_text("{ invalid json", encoding="utf-8")
        self.network([self.failed(), self.failed(), self.reachable()])
        with self.assertRaisesRegex(RuntimeError, "自动学习规则应用失败"):
            watch.probe_domain("blocked.example.com")
        self.assertEqual(app.AUTO_PROXY_RULE_SET_PATH.read_bytes(), before)
        self.assertEqual(app.load_auto_proxy_state()["learned"], {})


if __name__ == "__main__":
    unittest.main()
