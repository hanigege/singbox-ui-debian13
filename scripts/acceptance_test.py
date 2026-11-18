#!/usr/bin/env python3
"""测试机上的真实 API 验收：拒绝坏配置，合法保存和导入后保持服务正常。"""
import copy
import hashlib
import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def api(path: str, payload: dict | None = None) -> dict:
    token = Path("/etc/sing-box/rule-ui/token").read_text().strip()
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request("http://127.0.0.1:9091" + path, data=body, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=90) as response:
        return json.load(response)


def config_hash() -> str:
    return hashlib.sha256(Path("/etc/sing-box/config.json").read_bytes()).hexdigest()


def assert_services_active() -> None:
    subprocess.run(["systemctl", "is-active", "sing-box", "sing-box-tproxy", "singbox-rule-ui", "sing-box-auto-proxy-watch"], check=True)


def main() -> None:
    state = api("/api/state")
    state = state.get("state", state)
    backup = api("/api/backup/export")
    before = config_hash()
    invalid = copy.deepcopy(state)
    invalid["groups"]["dns"]["local_custom_port"] = 70000
    try:
        api("/api/save", invalid)
    except HTTPError as exc:
        assert exc.code in (400, 422), exc.code
    else:
        raise AssertionError("Invalid DNS port was not rejected")
    assert config_hash() == before, "非法请求修改了配置"
    assert_services_active()
    invalid_backup = copy.deepcopy(backup)
    invalid_backup["nodes"] = [
        {
            "enabled": True,
            "outbound": {
                "type": "vless",
                "tag": "codex-invalid-test",
                "server": "127.0.0.1",
                "server_port": 443,
                "uuid": "00000000-0000-4000-8000-000000000001",
                "codex_invalid_option": True,
            },
        }
    ]
    invalid_backup["groups"]["proxy"]["default"] = "Auto"
    try:
        api("/api/backup/import", invalid_backup)
    except HTTPError as exc:
        assert exc.code == 422, exc.code
        response = json.loads(exc.read())
        assert response["check"]["code"] != 0
    else:
        raise AssertionError("Backup with an invalid sing-box outbound was not rejected")
    assert config_hash() == before, "非法备份修改了配置"
    assert_services_active()
    config_path = Path("/etc/sing-box/config.json")
    valid_config = config_path.read_bytes()
    try:
        config_path.write_text("{ invalid json", encoding="utf-8")
        try:
            api("/api/restart", {})
        except HTTPError as exc:
            assert exc.code == 422, exc.code
            response = json.loads(exc.read())
            assert "was not restarted" in response["error"]
        else:
            raise AssertionError("Restart endpoint accepted an invalid config")
        assert_services_active()
    finally:
        config_path.write_bytes(valid_config)
    result = api("/api/save", state)
    assert result["check"]["code"] == 0 and result["restart"]["code"] == 0, result.get("error")
    result = api("/api/backup/import", backup)
    assert result["ok"] and result["applyScheduled"], result.get("error")
    time.sleep(4)
    subprocess.run(["/usr/local/bin/sing-box", "check", "-c", "/etc/sing-box/config.json"], check=True)
    subprocess.run(["systemctl", "is-active", "sing-box", "sing-box-tproxy", "singbox-rule-ui", "sing-box-auto-proxy-watch"], check=True)
    maintenance = api("/api/maintenance")["maintenance"]
    schedule = maintenance["ruleUpdate"]["schedule"]
    schedule_result = api("/api/rules/schedule", {"frequency": "daily", "hour": 4, "minute": 20, "randomizedDelayMinutes": 0})
    assert schedule_result["scheduleUpdate"]["ok"]
    assert schedule_result["maintenance"]["ruleUpdate"]["schedule"]["frequency"] == "daily"
    assert api("/api/rules/schedule", schedule)["scheduleUpdate"]["ok"]
    assert maintenance["configHealth"]["ok"], maintenance["configHealth"]["summary"]
    assert maintenance["ruleUpdate"]["summary"]["requiredOk"]
    assert maintenance["tproxy"]["ipv6PrefixMatches"]
    api("/api/proxy")
    print("PASS: invalid save/import/restart preserve active service; valid save/import, timer update, config check, API, and recovery")


if __name__ == "__main__":
    main()
