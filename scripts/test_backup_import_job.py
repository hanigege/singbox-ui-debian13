#!/usr/bin/env python3
"""验证备份导入后台任务的完成、回滚和并发边界。"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "singbox-rule-ui"))
import app


def wait_for_job(operation_id: str, timeout: float = 2) -> app.ImportJob:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = app.get_import_job(operation_id)
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    raise TimeoutError(f"备份导入任务未结束: {operation_id}")


class BackupImportJobTests(unittest.TestCase):
    def setUp(self) -> None:
        with app.IMPORT_JOB_LOCK:
            app.IMPORT_JOBS.clear()

    def test_success_records_one_runtime_probe(self) -> None:
        operation = app.create_import_job()
        runtime = {
            "proxy": {"ok": True, "data": {"now": "Auto"}},
            "delays": {
                "delays": {
                    "hy2-a": {"ok": True, "delay": 100, "error": None},
                    "hy2-b": {"ok": True, "delay": 120, "error": None},
                }
            },
        }
        with (
            patch.object(app, "restart_sing_box", return_value={"code": 0, "service": "active", "stderr": ""}) as restart,
            patch.object(app, "service_status", return_value="active"),
            patch.object(app, "sync_tproxy", return_value={"code": 0, "stderr": ""}) as sync,
            patch.object(app, "current_proxy_payload_after_probe", return_value=runtime) as probe,
        ):
            app.apply_import_runtime_later([], {}, {}, {}, operation["id"], delay=0)
            result = wait_for_job(operation["id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["runtime"], runtime)
        restart.assert_called_once_with()
        sync.assert_called_once_with(nodes=[], groups={}, normalized_lists={})
        probe.assert_called_once_with(test_delays=True)

    def test_restart_failure_rolls_back_and_reports_error(self) -> None:
        operation = app.create_import_job()
        failed = {"code": 1, "service": "failed", "stderr": "invalid runtime"}
        restored = {"code": 0, "service": "active", "stderr": ""}
        with (
            patch.object(app, "restart_sing_box", side_effect=[failed, restored]) as restart,
            patch.object(app, "service_status", return_value="failed"),
            patch.object(app, "rollback_apply") as rollback,
            patch.object(app, "sync_tproxy") as sync,
        ):
            app.apply_import_runtime_later([], {}, {}, {"backups": {}}, operation["id"], delay=0)
            result = wait_for_job(operation["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "rolled-back")
        self.assertIn("invalid runtime", result["error"])
        self.assertEqual(restart.call_args_list, [call(), call()])
        rollback.assert_called_once_with({"backups": {}})
        sync.assert_not_called()

    def test_concurrent_import_is_rejected(self) -> None:
        first = app.create_import_job()
        with self.assertRaisesRegex(RuntimeError, first["id"]):
            app.create_import_job()


if __name__ == "__main__":
    unittest.main()
