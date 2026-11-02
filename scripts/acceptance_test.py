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
        raise AssertionError("非法 DNS 端口未被拒绝")
    assert config_hash() == before, "非法请求修改了配置"
    result = api("/api/save", state)
    assert result["check"]["code"] == 0 and result["restart"]["code"] == 0, result.get("error")
    result = api("/api/backup/import", backup)
    assert result["ok"] and result["applyScheduled"], result.get("error")
    time.sleep(4)
    subprocess.run(["/usr/local/bin/sing-box", "check", "-c", "/etc/sing-box/config.json"], check=True)
    subprocess.run(["systemctl", "is-active", "sing-box", "sing-box-tproxy", "singbox-rule-ui"], check=True)
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
    print("PASS: 非法输入不落盘、合法保存、备份导入导出、timer 修改、配置检查、API 和服务恢复")


if __name__ == "__main__":
    main()
