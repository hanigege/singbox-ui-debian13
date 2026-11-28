#!/usr/bin/env python3
"""日志学习通道自检：学之前必须复探直连，两次都失败且代理可达才学。

用桩 app 模块驱动 scripts/auto_proxy_learn.py 的 run_tick，不碰真实配置。
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="auto-proxy-recheck-"))
os.environ["RULE_UI_AUTO_PROXY_LOCK"] = str(_TMP / "learn.lock")
sys.path.insert(0, str(ROOT / "singbox-rule-ui"))
import auto_proxy_learning  # noqa: E402  真实模块：advance_state/record_probe/解析

DOMAIN = "blocked.example.com"
LOG_LINE = (
    "2026-11-28T00:00:00+08:00 ERROR [1] connection: open connection to "
    f"{DOMAIN}:443 using outbound/direct[direct]: dial tcp 203.0.113.1:443: i/o timeout\n"
)


def load_learn_script():
    spec = importlib.util.spec_from_file_location("auto_proxy_learn", ROOT / "scripts" / "auto_proxy_learn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApp(types.ModuleType):
    def __init__(self, log_path, config_path, direct_ok, proxy_ok):
        super().__init__("app")
        self.CONFIG_PATH = config_path
        self.AUTO_PROXY_STATE_PATH = _TMP / "state.json"
        self.LISTS = ("whitelist", "blacklist", "greylist", "ddns")
        self._direct_ok = direct_ok
        self._proxy_ok = proxy_ok
        self.delay_calls = []
        self.applied_states = []
        self.written_states = []
        stat = log_path.stat()
        self._state = {
            "version": 1, "enabled": True,
            "cursor": {"device": stat.st_dev, "inode": stat.st_ino, "offset": 0},
            "candidates": {}, "learned": {},
        }

    def load_auto_proxy_state(self):
        return json.loads(json.dumps(self._state))

    def load_json(self, path, fallback):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback

    def read_entries(self, name):
        return []

    def is_manually_handled(self, domain, lists):
        return False

    def get_proxy_state(self):
        return {"ok": True, "data": {"now": "NodeA"}}

    def load_nodes(self):
        return []

    def enabled_node_tags(self, nodes):
        return ["NodeA"]

    def test_node_delay(self, tag, url=None, timeout_ms=5000):
        self.delay_calls.append(tag)
        ok = self._direct_ok if tag == "direct" else self._proxy_ok
        return {"tag": tag, "ok": ok, "delay": 100 if ok else None, "error": None if ok else "fail"}

    def write_json(self, path, data):
        self.written_states.append(json.loads(json.dumps(data)))
        if Path(path) == self.AUTO_PROXY_STATE_PATH:
            self._state = json.loads(json.dumps(data))

    def apply_auto_proxy_state(self, state):
        self.applied_states.append(json.loads(json.dumps(state)))
        self._state = json.loads(json.dumps(state))
        return {"ok": True, "changed": True}


class RecheckTests(unittest.TestCase):
    def run_scenario(self, direct_ok, proxy_ok):
        log_path = _TMP / "sing-box.log"
        log_path.write_text(LOG_LINE, encoding="utf-8")
        config_path = _TMP / "config.json"
        config_path.write_text(json.dumps({"log": {"output": str(log_path)}}), encoding="utf-8")
        fake = FakeApp(log_path, config_path, direct_ok, proxy_ok)
        previous = sys.modules.get("app")
        sys.modules["app"] = fake
        try:
            learn = load_learn_script()
            result = learn.run_tick()
        finally:
            if previous is not None:
                sys.modules["app"] = previous
            else:
                sys.modules.pop("app", None)
        return fake, result

    def test_direct_recovered_not_learned(self):
        fake, result = self.run_scenario(direct_ok=True, proxy_ok=True)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.applied_states, [])
        final = fake.written_states[-1] if fake.written_states else fake._state
        self.assertEqual(final["learned"], {})
        self.assertEqual(final["candidates"], {})
        self.assertEqual(fake.delay_calls, ["direct"])

    def test_direct_dead_proxy_ok_learned(self):
        fake, result = self.run_scenario(direct_ok=False, proxy_ok=True)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.delay_calls, ["direct", "direct", "NodeA"])
        self.assertEqual(len(fake.applied_states), 1)
        self.assertIn(DOMAIN, fake.applied_states[0]["learned"])

    def test_direct_dead_proxy_dead_not_learned(self):
        fake, result = self.run_scenario(direct_ok=False, proxy_ok=False)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.applied_states, [])
        final = fake.written_states[-1] if fake.written_states else fake._state
        self.assertEqual(final["learned"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
