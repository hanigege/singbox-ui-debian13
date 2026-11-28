#!/usr/bin/env python3
"""备份导入完整性审计自检：能修的修、不能修的隔离、每处必有警告、绝不静默。

只用一次性的测试密钥对（本文件内生成常量，非任何真实部署的凭据）。
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="import-integrity-")
os.environ.setdefault("RULE_UI_MANAGER_DIR", str(Path(_TMP) / "manager"))
os.environ.setdefault("RULE_UI_SING_BOX_CONFIG", str(Path(_TMP) / "config.json"))
os.environ.setdefault("RULE_UI_TOKEN_FILE", str(Path(_TMP) / "token"))
_HOME_FILE = str(Path(_TMP) / "manager" / "home.json")
sys.path.insert(0, str(ROOT / "singbox-rule-ui"))
import app

# 一次性测试密钥对（sing-box generate reality-keypair 现场生成，与任何真实部署无关）。
KEY_A_PRIV = "OCWqH1P8G0MDmgOCYoAd6F5SLaVRLtUHPXK9_nhWWE8"
KEY_A_PUB = "2VOzwjgFp_lU42tLhvqXQtrdr3CDND6xGfRWHtxvNGI"
KEY_B_PRIV = "wN216oRdUySYMIwxxBn7z5M4Ktn2JlVaGZYXgT_pFWI"
KEY_B_PUB = "inl1SE4uXf0c1dmx2JSC-MzPfkLbyAOOHfvwQo2yS1A"
DEVICE_UUID = "7cedf3c6-a0a1-46a7-98ea-3e7549accd86"


def fake_keypair_run(cmd, timeout=None):
    if "reality-keypair" in cmd:
        return {"code": 0, "stdout": f"PrivateKey: {KEY_B_PRIV}\nPublicKey: {KEY_B_PUB}\n", "stderr": ""}
    return {"code": 1, "stdout": "", "stderr": "unexpected command"}


def clean_home():
    return {
        "version": 1,
        "server": {
            "host": "home.example.com",
            "lan_ip": "",
            "lan_cidrs": [],
            "reality": {
                "enabled": True, "port": 8443, "lan_port": 8445,
                "server_name": "www.microsoft.com",
                "private_key": KEY_A_PRIV, "public_key": KEY_A_PUB,
                "short_id": ["abcd1234"],
            },
            "hy2": {
                "enabled": True, "port": 8443, "lan_port": 8445,
                "server_name": "www.microsoft.com",
                "obfs_password": "test-obfs-password",
                "cert_path": "/tmp/x/cert.pem", "key_path": "/tmp/x/key.pem",
            },
        },
        "devices": [{
            "id": "d_test1", "name": "phone", "enabled": True, "mode": "full",
            "uuid": DEVICE_UUID, "hy2_password": "dev-pass", "sub_token": "tok123",
            "created_at": "2026-01-01 00:00:00",
        }],
    }


def codes(warnings):
    return [item["code"] for item in warnings]


class AuditImportedHomeTests(unittest.TestCase):
    def test_clean_backup_no_warnings_and_unchanged(self):
        raw = clean_home()
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(copy.deepcopy(raw))
        self.assertEqual(warnings, [])
        self.assertEqual(home, app.normalize_home(raw))

    def test_reality_section_type_corruption_regenerates(self):
        raw = clean_home()
        raw["server"]["reality"] = "AAAAinvalid-key"
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["reality-key-regenerated"])
        reality = home["server"]["reality"]
        self.assertTrue(reality["enabled"])
        self.assertEqual(reality["private_key"], KEY_B_PRIV)
        self.assertEqual(reality["public_key"], KEY_B_PUB)
        self.assertTrue(reality["short_id"])

    def test_reality_invalid_private_key_regenerates(self):
        raw = clean_home()
        raw["server"]["reality"]["private_key"] = "AAAA"
        raw["server"]["reality"]["public_key"] = "BBBB"
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["reality-key-regenerated"])
        self.assertEqual(home["server"]["reality"]["private_key"], KEY_B_PRIV)

    def test_reality_pubkey_mismatch_rederived(self):
        raw = clean_home()
        raw["server"]["reality"]["public_key"] = KEY_B_PUB
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["reality-pubkey-rederived"])
        self.assertEqual(home["server"]["reality"]["public_key"], KEY_A_PUB)
        self.assertEqual(home["server"]["reality"]["private_key"], KEY_A_PRIV)

    def test_reality_disabled_with_valid_keys_untouched(self):
        raw = clean_home()
        raw["server"]["reality"]["enabled"] = False
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(warnings, [])
        self.assertFalse(home["server"]["reality"]["enabled"])

    def test_keypair_generation_failure_warns_and_stays_off(self):
        raw = clean_home()
        raw["server"]["reality"] = "garbage"
        with patch.object(app, "run_command", return_value={"code": 1, "stdout": "", "stderr": "boom"}):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["reality-key-invalid"])
        self.assertFalse(home["server"]["reality"]["enabled"])
        self.assertEqual(home["server"]["reality"]["private_key"], "")

    def test_hy2_section_corruption_restored(self):
        raw = clean_home()
        raw["server"]["hy2"] = "garbage"
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["hy2-restored"])
        self.assertTrue(home["server"]["hy2"]["enabled"])
        self.assertTrue(home["server"]["hy2"]["obfs_password"])

    def test_device_bad_uuid_disabled_and_named(self):
        raw = clean_home()
        raw["devices"][0]["uuid"] = "not-a-uuid"
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["device-disabled"])
        self.assertEqual(warnings[0]["device"], "phone")
        self.assertIn("uuid", warnings[0]["detail"])
        self.assertFalse(home["devices"][0]["enabled"])

    def test_device_missing_uuid_dropped_and_named(self):
        raw = clean_home()
        del raw["devices"][0]["uuid"]
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["device-dropped"])
        self.assertEqual(warnings[0]["device"], "phone")
        self.assertEqual(home["devices"], [])

    def test_device_empty_sub_token_disabled(self):
        raw = clean_home()
        raw["devices"][0]["sub_token"] = ""
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(codes(warnings), ["device-disabled"])
        self.assertIn("sub_token", warnings[0]["detail"])

    def test_already_disabled_broken_device_left_alone(self):
        raw = clean_home()
        raw["devices"][0]["uuid"] = "not-a-uuid"
        raw["devices"][0]["enabled"] = False
        with patch.object(app, "run_command", side_effect=fake_keypair_run):
            home, warnings = app.audit_imported_home(raw)
        self.assertEqual(warnings, [])


class ImportWarningsIntegrationTests(unittest.TestCase):
    def test_import_result_carries_warnings(self):
        payload = {
            "kind": "sing-box-gateway-ui-backup",
            "version": app.BACKUP_VERSION,
            "lists": {},
            "nodes": [],
            "groups": {},
            "home": clean_home(),
        }
        payload["home"]["devices"][0]["uuid"] = "not-a-uuid"
        with (
            patch.object(app, "run_command", side_effect=fake_keypair_run),
            patch.object(app, "normalize_payload_groups", return_value={}),
            patch.object(app, "staged_check", return_value={"code": 0, "stdout": "", "stderr": ""}),
            patch.object(app, "apply_all", return_value={}),
            patch.object(app, "apply_import_runtime_later", return_value=None),
            patch.object(app, "maintenance_status", return_value={}),
            patch.object(app, "load_state", return_value={}),
        ):
            result = app.import_backup_payload(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(codes(result["warnings"]), ["device-disabled"])
        saved = json.loads(Path(_HOME_FILE).read_text(encoding="utf-8"))
        self.assertFalse(saved["devices"][0]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
