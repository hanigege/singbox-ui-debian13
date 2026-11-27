#!/usr/bin/env python3
"""回家功能自检：全新安装零回归 / 迁移收编 / 设备变更 / 客户端配置生成与真实启动。

用法：在仓库根目录执行 SING_BOX_BIN=/path/to/sing-box python3 scripts/test_home_feature.py
（sing-box 二进制也可放在 /usr/local/bin/sing-box，免设环境变量。）
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
if REPO.name == "scripts":
    REPO = REPO.parent
UI_DIR = REPO / "singbox-rule-ui"
SING_BOX = os.environ.get("SING_BOX_BIN", "/usr/local/bin/sing-box")
ROOT = Path(tempfile.mkdtemp(prefix="home-test-"))
print("test root:", ROOT)

os.environ["RULE_UI_MANAGER_DIR"] = str(ROOT / "manager")
os.environ["RULE_UI_RULE_DIR"] = str(ROOT / "custom-rules")
os.environ["RULE_UI_SING_BOX_CONFIG"] = str(ROOT / "config.json")
os.environ["RULE_UI_TOKEN_FILE"] = str(ROOT / "rule-ui" / "token")
os.environ["RULE_UI_HOME_HY2_CERT_DIR"] = str(ROOT / "home-hy2")
os.environ["RULE_UI_REALITY_CREDS"] = str(ROOT / "no-creds.json")

sys.path.insert(0, str(UI_DIR))
sys.path.insert(0, str(REPO / "scripts"))
import app  # noqa: E402
import bootstrap_config as bootstrap  # noqa: E402

app.default_lan_ip = lambda: "192.168.99.6"
bootstrap.default_lan_ip = lambda: "192.168.99.6"
for name in ("CONFIG_DIR", "MANAGER_DIR", "RULE_DIR", "CONFIG_PATH", "BASE_CONFIG_PATH", "NODES_PATH", "GROUPS_PATH"):
    setattr(bootstrap, name, getattr(app, name, None) or getattr(bootstrap, name))
bootstrap.CONFIG_DIR = ROOT
bootstrap.MANAGER_DIR = app.MANAGER_DIR
bootstrap.RULE_DIR = app.RULE_DIR
bootstrap.CONFIG_PATH = app.CONFIG_PATH
bootstrap.BASE_CONFIG_PATH = app.BASE_CONFIG_PATH
bootstrap.NODES_PATH = app.NODES_PATH
bootstrap.GROUPS_PATH = app.GROUPS_PATH

fails = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f" {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def sing_box_check(path):
    result = subprocess.run([SING_BOX, "check", "-c", str(path)], capture_output=True, text=True)
    return result.returncode, (result.stderr or result.stdout).strip()[:300]


def gateway_check(rendered, name):
    """网关渲染结果引用 /etc/sing-box/rules 下的 .srs（真实安装由规则更新脚本下载，
    仓库里没有）。自检时把路径改写到临时目录并编译最小占位规则集，只验证结构。"""
    rules_root = ROOT / "rules"
    text = json.dumps(rendered, indent=2).replace("/etc/sing-box/rules/", str(rules_root) + "/")
    data = json.loads(text)
    for rs in data.get("route", {}).get("rule_set", []):
        path = Path(rs.get("path", ""))
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            if "/geoip/" in str(path):
                rules = [{"ip_cidr": ["1.2.3.0/24"]}]
            else:
                rules = [{"domain_suffix": ["example.com"]}]
            src = path.with_suffix(".json")
            src.write_text(json.dumps({"version": 3, "rules": rules}))
            subprocess.run([SING_BOX, "rule-set", "compile", "-o", str(path), str(src)], capture_output=True)
    target = ROOT / name
    target.write_text(json.dumps(data, indent=2))
    return sing_box_check(target)


# --- 1. 全新安装（bootstrap） ---
bootstrap.main()
check("bootstrap wrote config.json", app.CONFIG_PATH.exists())
rendered = app.render_config()
home_inbounds = [i for i in rendered["inbounds"] if i.get("tag") in app.HOME_TAGS]
check("fresh install: no home inbounds", home_inbounds == [])
check("fresh install: home.json not created", not app.HOME_PATH.exists())
code, out = gateway_check(rendered, "rendered-fresh.json")
check("fresh install: sing-box check rendered", code == 0, out)

# --- 2. 迁移收编：base.json 手工 Reality 入站（用真实 sing-box 生成的密钥对）---
kp = subprocess.run([SING_BOX, "generate", "reality-keypair"], capture_output=True, text=True).stdout
keys = dict(line.split(":", 1) for line in kp.strip().splitlines())
priv, pub = keys["PrivateKey"].strip(), keys["PublicKey"].strip()
uuid_out = subprocess.run([SING_BOX, "generate", "uuid"], capture_output=True, text=True).stdout.strip()
base = app.load_json(app.BASE_CONFIG_PATH, {})
base["inbounds"].append(
    {
        "type": "vless",
        "tag": "vless-reality-home",
        "listen": "::",
        "listen_port": 8443,
        "users": [{"uuid": uuid_out}],
        "tls": {
            "enabled": True,
            "server_name": "www.microsoft.com",
            "reality": {
                "enabled": True,
                "handshake": {"server": "www.microsoft.com", "server_port": 443},
                "private_key": priv,
                "short_id": ["0123abcd"],
            },
        },
    }
)
app.write_json(app.BASE_CONFIG_PATH, base)
home = app.load_home()
check("migration: home.json written", app.HOME_PATH.exists())
check("migration: reality enabled", home["server"]["reality"]["enabled"] is True)
check("migration: private key verbatim", home["server"]["reality"]["private_key"] == priv)
check("migration: derived public key matches sing-box", home["server"]["reality"]["public_key"] == pub,
      f"derived={home['server']['reality']['public_key']} real={pub}")
check("migration: one device with original uuid", len(home["devices"]) == 1 and home["devices"][0]["uuid"] == uuid_out)
rendered2 = app.render_config()
reality_inbounds = [i for i in rendered2["inbounds"] if i.get("tag") == "vless-reality-home"]
check("migration: exactly one reality inbound after render", len(reality_inbounds) == 1)
if reality_inbounds:
    u = reality_inbounds[0]
    check("migration: inbound uuid/port/sid intact",
          u["users"][0]["uuid"] == uuid_out and u["listen_port"] == 8443
          and u["tls"]["reality"]["short_id"] == ["0123abcd"] and u["tls"]["reality"]["private_key"] == priv)
rendered3 = app.render_config()
check("render idempotent", json.dumps(rendered2, sort_keys=True) == json.dumps(rendered3, sort_keys=True))
code, out = gateway_check(rendered2, "rendered-migrated.json")
check("migration: sing-box check rendered", code == 0, out)

# --- 3. 设备变更链路（伪造 check/restart）---
app.check_config = lambda path: {"code": 0, "stdout": "", "stderr": ""}
app.restart_sing_box = lambda: {"code": 0, "stdout": "", "stderr": ""}
app.service_status = lambda: "active"
result = app.apply_home_change(lambda h: h["devices"].append({
    "id": "d_testphone", "name": "测试手机", "uuid": "11111111-2222-3333-4444-555555555555",
    "hy2_password": "test-hy2-pass", "sub_token": "test-sub-token", "enabled": True, "created_at": "2026-11-27 23:00:00",
}))
check("apply add device ok", result["ok"] is True, str(result.get("error", "")))
rendered4 = app.render_config()
users = [i for i in rendered4["inbounds"] if i.get("tag") == "vless-reality-home"][0]["users"]
check("rendered reality users = 2", len(users) == 2)

# --- 4. 客户端配置生成 + sing-box check ---
home_now = app.load_home()
device = home_now["devices"][0]
try:
    app.build_home_client_profile(home_now, device)
    check("profile without host raises", False)
except ValueError:
    check("profile without host raises", True)
home_now["server"]["host"] = "home.example.com"
app.save_home(home_now)
profile = app.build_home_client_profile(home_now, device)
lan_rule = [r for r in profile["route"]["rules"] if r.get("ip_cidr")]
check("profile LAN cidr derived from gateway /24", lan_rule and lan_rule[0]["ip_cidr"] == ["192.168.99.0/24"], str(lan_rule))
pfile = ROOT / "client-profile.json"
pfile.write_text(json.dumps(profile, indent=2, ensure_ascii=False))
code, out = sing_box_check(pfile)
check("client profile (reality only): sing-box check", code == 0, out)
links = app.build_home_share_links(home_now, device)
check("vless share link shape", links.get("vless", "").startswith("vless://") and f"pbk={pub}" in links["vless"], links.get("vless", ""))

# --- 5. 启用 Hy2：证书生成 + 渲染 + 双协议客户端配置 ---
def enable_hy2(h):
    h["server"]["hy2"]["enabled"] = True
    h["server"]["hy2"]["obfs_password"] = "obfs-test-pass"
    cert = app.ensure_home_hy2_cert(h)
    assert cert["ok"], cert

result = app.apply_home_change(enable_hy2)
check("apply enable hy2 ok", result["ok"] is True, str(result.get("error", "")))
check("hy2 cert generated", Path(home_now["server"]["hy2"]["cert_path"]).exists())
home_now = app.load_home()
rendered5 = app.render_config()
hy2_in = [i for i in rendered5["inbounds"] if i.get("tag") == "hy2-home"]
check("rendered hy2 inbound present with 2 users", len(hy2_in) == 1 and len(hy2_in[0]["users"]) == 2)
code, out = gateway_check(rendered5, "rendered-hy2.json")
check("gateway with hy2: sing-box check", code == 0, out)
profile2 = app.build_home_client_profile(home_now, device)
pfile2 = ROOT / "client-profile2.json"
pfile2.write_text(json.dumps(profile2, indent=2, ensure_ascii=False))
code, out = sing_box_check(pfile2)
check("client profile (dual): sing-box check", code == 0, out)
tags = [o["tag"] for o in profile2["outbounds"]]
check("client profile outbounds", tags == ["home-vless", "home-hy2", "home-auto", "direct"], str(tags))
check("client dns final is gw-dns", profile2["dns"]["final"] == "gw-dns")
check("client route final is home-auto", profile2["route"]["final"] == "home-auto")

# --- 5b. 客户端配置必须能真正启动（check 查不出 detour 空 direct 这类启动期错误）---
start_profile = json.loads(json.dumps(profile2))
start_profile["inbounds"] = [{"type": "mixed", "tag": "tun-in", "listen": "127.0.0.1", "listen_port": 11099}]
start_profile["route"]["rules"] = [r for r in start_profile["route"]["rules"] if r.get("action") != "hijack-dns"]
start_file = ROOT / "client-start.json"
start_file.write_text(json.dumps(start_profile, indent=2, ensure_ascii=False))
proc = subprocess.Popen([SING_BOX, "run", "-c", str(start_file)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    import time as _time
    _time.sleep(3)
    alive = proc.poll() is None
    check("client profile actually starts (no FATAL)", alive)
    if not alive:
        print(proc.stdout.read()[:400])
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

# --- 6. HTTP 层：/sub 与 /api/home ---
(ROOT / "rule-ui").mkdir(parents=True, exist_ok=True)
(ROOT / "rule-ui" / "token").write_text("test-token-123456\n")
from http.server import ThreadingHTTPServer
server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
port = server.server_address[1]
import threading
threading.Thread(target=server.serve_forever, daemon=True).start()

def http_get(path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

status, body = http_get(f"/sub/{device['sub_token']}")
check("/sub valid token -> 200 profile", status == 200 and "home-auto" in body, f"{status}")
status, _ = http_get("/sub/wrong-token")
check("/sub wrong token -> 404", status == 404, f"{status}")
status, _ = http_get("/api/home")
check("/api/home without token -> 401", status == 401, f"{status}")
status, body = http_get("/api/home", token="test-token-123456")
parsed = json.loads(body) if status == 200 else {}
check("/api/home with token -> 200, secrets masked",
      status == 200 and "private_key" not in body and device["uuid"] not in body, f"{status}")

def http_post(path, payload, token="test-token-123456"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

status, _ = http_post("/api/home/server/host", {"host": "home2.example.com"})
check("POST host valid -> 200", status == 200, f"{status}")
check("host persisted", app.load_home()["server"]["host"] == "home2.example.com")
status, _ = http_post("/api/home/server/host", {"host": "bad host!"})
check("POST host invalid -> 400", status == 400, f"{status}")
server.shutdown()

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
