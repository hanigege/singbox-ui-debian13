#!/usr/bin/env python3
"""自动学习状态文件损坏时的宽容兜底自检：不抛错、按规则集文件重建已学名单。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="auto-proxy-fallback-")
os.environ.setdefault("RULE_UI_MANAGER_DIR", str(Path(_TMP) / "manager"))
os.environ.setdefault("RULE_UI_SING_BOX_CONFIG", str(Path(_TMP) / "config.json"))
os.environ.setdefault("RULE_UI_TOKEN_FILE", str(Path(_TMP) / "token"))
sys.path.insert(0, str(ROOT / "singbox-rule-ui"))
import app


class TolerantLoadTests(unittest.TestCase):
    def setUp(self):
        app.MANAGER_DIR.mkdir(parents=True, exist_ok=True)
        # 让 ensure_manager_data 提前返回，避免它去读不存在的 config.json 做初始化抽取。
        app.write_json(app.BASE_CONFIG_PATH, {})
        app.write_json(app.NODES_PATH, [])
        app.write_json(app.GROUPS_PATH, {})
        app.write_json(app.AUTO_PROXY_RULE_SET_PATH, {"version": 5, "rules": [{"domain": ["a.example.com", "b.example.com"]}]})

    def tearDown(self):
        for path in (app.AUTO_PROXY_STATE_PATH, app.AUTO_PROXY_RULE_SET_PATH):
            if path.exists():
                path.unlink()

    def test_corrupt_json_rebuilds_from_rule_set(self):
        app.AUTO_PROXY_STATE_PATH.write_text("{not json", encoding="utf-8")
        state = app.load_auto_proxy_state()
        self.assertEqual(sorted(state["learned"]), ["a.example.com", "b.example.com"])
        self.assertTrue(all(item["learnedAt"] > 0 for item in state["learned"].values()))

    def test_wrong_version_rebuilds_from_rule_set(self):
        app.write_json(app.AUTO_PROXY_STATE_PATH, {"version": 99, "learned": {"x.example.com": {"learnedAt": 1, "expiresAt": 0}}})
        state = app.load_auto_proxy_state()
        self.assertEqual(sorted(state["learned"]), ["a.example.com", "b.example.com"])

    def test_both_corrupt_returns_empty_state(self):
        app.AUTO_PROXY_STATE_PATH.write_text("garbage", encoding="utf-8")
        app.AUTO_PROXY_RULE_SET_PATH.write_text("garbage", encoding="utf-8")
        state = app.load_auto_proxy_state()
        self.assertEqual(state["learned"], {})
        self.assertTrue(state["enabled"])

    def test_valid_state_untouched(self):
        good = {
            "version": 1, "enabled": True, "cursor": None,
            "candidates": {},
            "learned": {"keep.example.com": {"learnedAt": 1700000000, "expiresAt": 0}},
        }
        app.write_json(app.AUTO_PROXY_STATE_PATH, good)
        state = app.load_auto_proxy_state()
        self.assertEqual(sorted(state["learned"]), ["keep.example.com"])
        self.assertEqual(state["learned"]["keep.example.com"]["learnedAt"], 1700000000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
