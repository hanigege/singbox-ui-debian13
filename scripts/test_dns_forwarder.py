#!/usr/bin/env python3
"""国内 DNS 转发层自检：自定义上游列表归一/迁移/上限、转发层渲染与热同步、旧口径回退。

用法：在仓库根目录执行 python3 scripts/test_dns_forwarder.py
（不需要 sing-box 二进制；systemctl 相关调用在测试里打桩。）
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
if REPO.name == "scripts":
    REPO = REPO.parent
UI_DIR = REPO / "singbox-rule-ui"
ROOT = Path(tempfile.mkdtemp(prefix="dns-fwd-test-"))
print("test root:", ROOT)

os.environ["RULE_UI_MANAGER_DIR"] = str(ROOT / "manager")
os.environ["RULE_UI_RULE_DIR"] = str(ROOT / "custom-rules")
os.environ["RULE_UI_SING_BOX_CONFIG"] = str(ROOT / "config.json")
os.environ["RULE_UI_TOKEN_FILE"] = str(ROOT / "rule-ui" / "token")
os.environ["RULE_UI_DNS_UPSTREAMS"] = str(ROOT / "manager" / "dns-upstreams.conf")

sys.path.insert(0, str(UI_DIR))
import app  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name} {detail}")


def expect_value_error(name, fn):
    try:
        fn()
    except ValueError:
        check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"wrong exception: {exc!r}")
    else:
        check(name, False, "no ValueError raised")


# ---------- 1. 自定义上游归一：迁移 / 去重 / 上限 / 校验 ----------
legacy = {"local_custom_server": "223.5.5.5", "local_custom_port": 5353}
check("legacy-migration", app.normalize_custom_dns_servers(legacy) == [{"server": "223.5.5.5", "port": 5353}])

listed = {
    "local_custom_server": "9.9.9.9",
    "local_custom_servers": [
        {"server": "119.29.29.29", "port": 53},
        {"server": "119.29.29.29", "port": 53},
        {"server": "114.114.114.114", "port": 8053},
        "223.5.5.5",
    ],
}
check(
    "list-priority-dedupe",
    app.normalize_custom_dns_servers(listed)
    == [
        {"server": "119.29.29.29", "port": 53},
        {"server": "114.114.114.114", "port": 8053},
        {"server": "223.5.5.5", "port": 53},
    ],
)

six = {"local_custom_servers": [{"server": f"10.0.0.{i}", "port": 53} for i in range(1, 7)]}
check("cap-truncate-nonstrict", len(app.normalize_custom_dns_servers(six)) == app.LOCAL_DNS_CUSTOM_MAX == 5)
expect_value_error("cap-strict-raises", lambda: app.normalize_custom_dns_servers(six, strict=True))
expect_value_error(
    "bad-ip-strict-raises",
    lambda: app.normalize_custom_dns_servers({"local_custom_servers": [{"server": "not-an-ip", "port": 53}]}, strict=True),
)
check(
    "bad-ip-nonstrict-skips",
    app.normalize_custom_dns_servers({"local_custom_servers": [{"server": "not-an-ip", "port": 53}]}) == [],
)
check(
    "ipv6-ok",
    app.normalize_custom_dns_servers({"local_custom_servers": [{"server": "2400:3200::1", "port": 53}]})
    == [{"server": "2400:3200::1", "port": 53}],
)

# ---------- 2. normalize_payload_groups：列表落盘 + 单值镜像 + 自定义为空报错 ----------
MINIMAL_CONFIG = {
    "outbounds": [
        {"type": "selector", "tag": "Proxy", "outbounds": ["Auto"], "default": "Auto"},
        {"type": "urltest", "tag": "Auto", "outbounds": []},
        {"type": "direct", "tag": "direct"},
        {"type": "block", "tag": "block"},
    ],
    "dns": {
        "servers": [
            {"tag": "local-dns", "type": "udp", "server": "127.0.0.1", "server_port": 1053},
            {"tag": "fakeip-dns", "type": "fakeip", "inet4_range": "198.18.0.0/15", "inet6_range": "2001:2::198:18:0:0/96"},
        ]
    },
}
app.CONFIG_PATH.write_text(json.dumps(MINIMAL_CONFIG), encoding="utf-8")
app.ensure_manager_data()
base_groups = app.load_groups()
payload = json.loads(json.dumps(base_groups))
payload["dns"] = {
    "local": "custom_dns",
    "local_custom_servers": [
        {"server": "223.5.5.5", "port": 53},
        {"server": "119.29.29.29", "port": 5353},
    ],
}
normalized = app.normalize_payload_groups(payload)
check(
    "payload-list-stored",
    normalized["dns"]["local_custom_servers"]
    == [{"server": "223.5.5.5", "port": 53}, {"server": "119.29.29.29", "port": 5353}],
)
check(
    "payload-legacy-mirror",
    normalized["dns"]["local_custom_server"] == "223.5.5.5" and normalized["dns"]["local_custom_port"] == 53,
)

payload_legacy = json.loads(json.dumps(base_groups))
payload_legacy["dns"] = {"local": "custom_dns", "local_custom_server": "114.114.114.114", "local_custom_port": 53}
normalized_legacy = app.normalize_payload_groups(payload_legacy)
check(
    "payload-legacy-builds-list",
    normalized_legacy["dns"]["local_custom_servers"] == [{"server": "114.114.114.114", "port": 53}],
)

payload_empty = json.loads(json.dumps(base_groups))
payload_empty["dns"] = {
    "local": "custom_dns",
    "local_custom_servers": [],
    "local_custom_server": "",
    "local_custom_port": base_groups["dns"]["local_custom_port"],
}
normalized_empty = app.normalize_payload_groups(payload_empty)
check(
    "payload-custom-empty-keeps-previous",
    normalized_empty["dns"]["local_custom_servers"] == base_groups["dns"]["local_custom_servers"],
    str(normalized_empty["dns"]),
)

# ---------- 3. load_groups 回填：老 groups.json 只有单值字段 ----------
app.write_json(app.GROUPS_PATH, {"dns": {"local": "custom_dns", "local_custom_server": "119.29.29.29", "local_custom_port": 53}})
loaded = app.load_groups()
check(
    "load-backfills-list",
    loaded["dns"]["local_custom_servers"] == [{"server": "119.29.29.29", "port": 53}],
)

# ---------- 4. 渲染：转发层开 = 固定回环；关 = 旧口径 ----------
groups_custom = {"dns": {"local": "custom_dns", "local_custom_servers": [{"server": "223.5.5.5", "port": 53}, {"server": "119.29.29.29", "port": 53}]}}
groups_preset = {"dns": {"local": "alidns"}}

os.environ["RULE_UI_DNS_FORWARDER"] = "1"
cfg = app.local_dns_server_config("custom_dns", groups_custom["dns"])
check("render-forwarder-loopback", cfg["server"] == "127.0.0.1" and cfg["server_port"] == 1053)
cfg = app.local_dns_server_config("alidns", groups_preset["dns"])
check("render-forwarder-preset-also-loopback", cfg["server"] == "127.0.0.1")

os.environ["RULE_UI_DNS_FORWARDER"] = "0"
cfg = app.local_dns_server_config("custom_dns", groups_custom["dns"])
check("render-legacy-custom-first", cfg["server"] == "223.5.5.5" and cfg["server_port"] == 53)
cfg = app.local_dns_server_config("alidns", groups_preset["dns"])
check("render-legacy-preset", cfg["server"] == "223.5.5.5" and cfg["server_port"] == 53)
cfg = app.local_dns_server_config("dnspod", groups_preset["dns"])
check("render-legacy-dnspod", cfg["server"] == "119.29.29.29")

# apply_local_dns_settings 在两种模式下都精确替换 local-dns
os.environ["RULE_UI_DNS_FORWARDER"] = "1"
config = {"dns": {"servers": [{"tag": "local-dns", "type": "udp", "server": "8.8.8.8", "server_port": 53}]}}
app.apply_local_dns_settings(config, groups_preset)
check("apply-loopback", config["dns"]["servers"][0]["server"] == "127.0.0.1")
os.environ["RULE_UI_DNS_FORWARDER"] = "0"
app.apply_local_dns_settings(config, groups_preset)
check("apply-legacy", config["dns"]["servers"][0]["server"] == "223.5.5.5")

# ---------- 5. 转发层热同步：写文件 + SIGHUP，不写第二次 ----------
os.environ["RULE_UI_DNS_FORWARDER"] = "1"
calls = []
app.unit_status = lambda unit: "active"
app.run_command = lambda args, timeout=15: (calls.append(args), {"code": 0, "stdout": "", "stderr": ""})[1]

app.write_json(app.GROUPS_PATH, groups_custom)
result = app.sync_dns_forwarder()
content = app.DNS_UPSTREAMS_PATH.read_text(encoding="utf-8")
check("sync-writes-upstreams", content == "server=223.5.5.5\nserver=119.29.29.29\n", repr(content))
check("sync-changed-and-hup", result["changed"] and result["reloaded"] and any("HUP" in " ".join(c) for c in calls), str(result))

calls.clear()
result2 = app.sync_dns_forwarder()
check("sync-idempotent", not result2["changed"] and not result2["reloaded"] and not calls, str(result2))

app.write_json(app.GROUPS_PATH, {"dns": {"local": "custom_dns", "local_custom_servers": [{"server": "114.114.114.114", "port": 8053}]}})
app.sync_dns_forwarder()
content = app.DNS_UPSTREAMS_PATH.read_text(encoding="utf-8")
check("sync-port-suffix", content == "server=114.114.114.114#8053\n", repr(content))
check("upstreams-roundtrip", app.read_dns_upstreams_file() == [{"server": "114.114.114.114", "port": 8053}])

# ---------- 6. 反向解析：回环地址从 servers-file 还原选择 ----------
app.write_json(app.GROUPS_PATH, groups_preset)
app.sync_dns_forwarder()  # alidns -> server=223.5.5.5（预设 IP），不是 dnspod
_, _, parsed_groups = app.extract_initial_manager_data(MINIMAL_CONFIG)
check("reverse-preset", parsed_groups["dns"]["local"] == "alidns", str(parsed_groups["dns"]))

app.DNS_UPSTREAMS_PATH.write_text("server=223.5.5.5\nserver=119.29.29.29\n", encoding="utf-8")
_, _, parsed_groups = app.extract_initial_manager_data(MINIMAL_CONFIG)
check(
    "reverse-custom-list",
    parsed_groups["dns"]["local"] == "custom_dns"
    and parsed_groups["dns"]["local_custom_servers"]
    == [{"server": "223.5.5.5", "port": 53}, {"server": "119.29.29.29", "port": 53}],
    str(parsed_groups["dns"]),
)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL PASS")
