#!/bin/bash
set -euo pipefail
umask 077
PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test "$(id -u)" = 0 || { echo '请使用 root 执行。' >&2; exit 1; }
. /etc/os-release
if [ "$ID" != debian ] || [ "$VERSION_ID" != 13 ] || [ ! -d /run/systemd/system ]; then
  echo '只支持运行 systemd 的 Debian 13；建议使用 VM 或裸机。' >&2
  exit 1
fi
exec 8>/run/singbox-ui-install.lock
flock -n 8 || { echo '已有安装正在运行。' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
if getent ahostsv4 github.com | awk '{print $1}' | grep -Eq '^(28\.|198\.(18|19)\.)'; then
  echo '系统 DNS 返回了其它网关的 FakeIP，无法供本机 TProxy 使用。请将系统上游 DNS 改为真实解析器（如 223.5.5.5），再重新安装。' >&2
  exit 1
fi
apt-get -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends ca-certificates curl python3 python3-qrcode nftables iproute2 iputils-ping rsync util-linux logrotate procps unattended-upgrades dnsmasq
gateway_tmp=$(mktemp -d)
backup_dir=/var/backups/singbox-ui/$(date +%Y%m%d-%H%M%S)-$$
mkdir -p "$backup_dir"
services=(sing-box.service sing-box-tproxy.service sing-box-dns-forwarder.service singbox-rule-ui.service update-sing-box-rules.timer monitor-sing-box-runtime.timer sing-box-logrotate.timer sing-box-config-backup.timer)
if [ -x /usr/sbin/ifup ] && systemctl cat ifupdown-wait-online.service >/dev/null 2>&1; then
  services+=(ifupdown-wait-online.service)
fi
for service in "${services[@]}"; do
  if systemctl is-active --quiet "$service"; then echo "$service" >> "$backup_dir/active-services"; fi
  if systemctl is-enabled --quiet "$service"; then echo "$service" >> "$backup_dir/enabled-services"; fi
done
paths=(etc/sing-box opt/singbox-rule-ui usr/local/bin/sing-box usr/local/lib/sing-box etc/default/singbox-gateway etc/sysctl.d/99-sing-box-tproxy.conf etc/sysctl.d/98-quic-udp-buffers.conf etc/apt/apt.conf.d/20auto-upgrades etc/apt/apt.conf.d/52unattended-upgrades-local etc/logrotate.d/sing-box-gateway etc/systemd/system/update-sing-box-rules.timer.d)
for unit in "$PROJECT_DIR"/systemd/*; do paths+=("etc/systemd/system/$(basename "$unit")"); done
for helper in refresh-sing-box-runtime-config monitor-sing-box-runtime update-sing-box-rules-jsdelivr refresh-sing-box-tproxy-setup sing-box-tproxy-setup stop-sing-box-tproxy sing-box-config-snapshot; do paths+=("usr/local/sbin/$helper"); done
for helper in sing-box-gateway-info sing-box-gateway-uninstall; do paths+=("usr/local/bin/$helper"); done
for path in "${paths[@]}"; do
  if [ -e "/$path" ]; then echo "$path" >> "$backup_dir/paths"; fi
done
touch "$backup_dir/paths"
tar -C / -cpf "$backup_dir/before.tar" -T "$backup_dir/paths"
install_mutated=0
rollback() {
  result=$?
  trap - EXIT
  if [ "$result" != 0 ] && [ "$install_mutated" = 1 ]; then
    echo "安装失败，恢复旧文件；备份: $backup_dir" >&2
    systemctl stop "${services[@]}" || true
    for service in "${services[@]}"; do
      if ! grep -qxF "$service" "$backup_dir/enabled-services" 2>/dev/null; then systemctl disable "$service" 2>/dev/null || true; fi
    done
    tar -C / -xpf "$backup_dir/before.tar"
    systemctl daemon-reload
    if [ -f "$backup_dir/active-services" ]; then
      while read -r service; do systemctl restart "$service" || echo "恢复服务失败: $service" >&2; done < "$backup_dir/active-services"
    fi
  fi
  rm -rf "$gateway_tmp"
  exit "$result"
}
trap rollback EXIT

# 按当前架构准备本仓库归档；下载或校验失败时在改动正式程序前停止。
arch=$(dpkg --print-architecture)
python3 "$PROJECT_DIR/scripts/download_binary.py" --arch "$arch" \
  --source-archive "${SINGBOX_UI_SOURCE_ARCHIVE:-$PROJECT_DIR/source.tar.gz}" --prefix "${GITHUB_DOWNLOAD_PREFIX:-}"
python3 "$PROJECT_DIR/scripts/bundled_assets.py" --arch "$arch" --destination "$gateway_tmp/binary"
"$gateway_tmp/binary/sing-box" version
mkdir -p /usr/local/bin /usr/local/sbin /usr/local/lib/sing-box /opt/singbox-rule-ui /etc/sing-box/manager /etc/sing-box/custom-rules /var/log/sing-box-gateway
# 一个安装器只管理自己创建的网关；已有第三方配置需要显式导入。
if [ -f /etc/sing-box/config.json ] && [ ! -f /etc/sing-box/manager/base.json ]; then
  echo '检测到第三方 sing-box 配置，请先备份后通过 Rule UI 导入节点。' >&2
  exit 1
fi
install_mutated=1
# 暂停旧监控器，避免复装迁移与主动学习同时写状态。
# 自动代理学习已下线：升级安装时停用并移除旧组件（单元、助手脚本、状态文件），渲染层会把其规则集引用按旧版规则集清理掉。
for old_unit in sing-box-auto-proxy-watch.service sing-box-auto-proxy-learning.timer sing-box-auto-proxy-learning.service; do
  if systemctl list-unit-files "$old_unit" >/dev/null 2>&1 || [ -f "/etc/systemd/system/$old_unit" ]; then systemctl disable --now "$old_unit" >/dev/null 2>&1 || true; fi
  rm -f "/etc/systemd/system/$old_unit"
done
rm -f /usr/local/sbin/sing-box-auto-proxy-learn /usr/local/sbin/sing-box-auto-proxy-watch
rm -f /etc/sing-box/manager/auto-proxy-learning.json /etc/sing-box/manager/auto-proxy-learned.json
install -m 0755 "$gateway_tmp/binary/sing-box" /usr/local/bin/sing-box.new
mv /usr/local/bin/sing-box.new /usr/local/bin/sing-box
if [ -f "$gateway_tmp/binary/libcronet.so" ]; then
  install -m 0644 "$gateway_tmp/binary/libcronet.so" /usr/local/lib/sing-box/libcronet.so
fi
rsync -a --exclude __pycache__ "$PROJECT_DIR/singbox-rule-ui/" /opt/singbox-rule-ui/
install -m 0755 "$PROJECT_DIR/scripts/refresh_runtime_config.py" /usr/local/sbin/refresh-sing-box-runtime-config
install -m 0755 "$PROJECT_DIR/scripts/monitor_runtime.py" /usr/local/sbin/monitor-sing-box-runtime
install -m 0755 "$PROJECT_DIR/scripts/sync_tproxy_setup.py" /usr/local/sbin/refresh-sing-box-tproxy-setup
install -m 0755 "$PROJECT_DIR/scripts/update-sing-box-rules-jsdelivr" /usr/local/sbin/update-sing-box-rules-jsdelivr
install -m 0755 "$PROJECT_DIR/scripts/stop-sing-box-tproxy" /usr/local/sbin/stop-sing-box-tproxy
install -m 0755 "$PROJECT_DIR/scripts/sing-box-gateway-info" /usr/local/bin/sing-box-gateway-info
install -m 0755 "$PROJECT_DIR/scripts/uninstall.sh" /usr/local/bin/sing-box-gateway-uninstall
install -m 0644 "$PROJECT_DIR/templates/logrotate.conf" /etc/logrotate.d/sing-box-gateway
install -m 0755 "$PROJECT_DIR/scripts/sing-box-config-snapshot" /usr/local/sbin/sing-box-config-snapshot
install -m 0644 "$PROJECT_DIR/templates/sysctl-quic-buffers.conf" /etc/sysctl.d/98-quic-udp-buffers.conf
# 立即生效一次；LXC 等环境可能不允许写 net.core，失败不阻断安装，重启后由 systemd-sysctl 再应用。
sysctl -p /etc/sysctl.d/98-quic-udp-buffers.conf || true
# 自动安全/稳定更新：管理员已有同名配置时保留，不覆盖。
if [ ! -f /etc/apt/apt.conf.d/20auto-upgrades ]; then
  install -m 0644 "$PROJECT_DIR/templates/apt/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
fi
if [ ! -f /etc/apt/apt.conf.d/52unattended-upgrades-local ]; then
  install -m 0644 "$PROJECT_DIR/templates/apt/52unattended-upgrades-local" /etc/apt/apt.conf.d/52unattended-upgrades-local
fi
install -m 0644 "$PROJECT_DIR"/systemd/* /etc/systemd/system/
# 国内 DNS 本机转发层（dnsmasq）主配置：托管文件，每次安装都对齐仓库版本；
# 真实上游在 manager/dns-upstreams.conf，由平台维护，安装器不覆盖已有值。
install -m 0644 "$PROJECT_DIR/templates/dnsmasq-forwarder.conf" /etc/sing-box/dnsmasq-forwarder.conf
# Debian 的 dnsmasq 包自带同名服务会占 53 端口并自启，本机转发层用独立单元，关掉它。
systemctl disable --now dnsmasq.service 2>/dev/null || true
systemctl reset-failed dnsmasq.service 2>/dev/null || true
if [ ! -f /etc/default/singbox-gateway ]; then
  install -m 0600 "$PROJECT_DIR/templates/gateway.env" /etc/default/singbox-gateway
fi
set -a
. /etc/default/singbox-gateway
set +a
python3 "$PROJECT_DIR/scripts/bootstrap_config.py"
# 转发层上游初始值：按当前 groups 的生效选择生成（新装=默认预设，升级=用户现有选择），
# 保证 sing-box 起来之前转发层就有正确上游；之后由平台热维护。生成失败不挡安装，
# rule-ui 启动时还会再同步一次。
python3 - <<'PYEOF' || true
import json
from pathlib import Path

groups = {}
try:
    groups = json.loads(Path("/etc/sing-box/manager/groups.json").read_text(encoding="utf-8"))
except Exception:
    pass
dns = groups.get("dns", {}) if isinstance(groups, dict) else {}
presets = {"dnspod": ("119.29.29.29", 53), "alidns": ("223.5.5.5", 53), "114dns": ("114.114.114.114", 53)}
choice = dns.get("local", "dnspod")
upstreams = []
if choice == "custom_dns":
    raw = dns.get("local_custom_servers")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("server"):
                upstreams.append((str(item["server"]), int(item.get("port", 53))))
    if not upstreams and dns.get("local_custom_server"):
        upstreams.append((str(dns["local_custom_server"]), int(dns.get("local_custom_port", 53))))
    if not upstreams:
        upstreams = [("223.5.5.5", 53)]
else:
    upstreams = [presets.get(choice, presets["dnspod"])]
lines = [f"server={server}" + (f"#{port}" if port != 53 else "") for server, port in upstreams[:5]]
Path("/etc/sing-box/manager/dns-upstreams.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
PYEOF
RULE_UPDATE_RESTART=0 RULE_UPDATE_LOCK_WAIT=300 /usr/local/sbin/update-sing-box-rules-jsdelivr
/usr/local/sbin/refresh-sing-box-runtime-config
/usr/local/bin/sing-box check -c /etc/sing-box/config.json
# 检查 LAN 53 端口冲突，不停止其它 DNS 或改写系统 resolver。
python3 "$PROJECT_DIR/scripts/check_ports.py"
chmod -R go-rwx /etc/sing-box/manager /etc/sing-box/rule-ui
chmod 0600 /etc/sing-box/config.json
systemctl daemon-reload
/usr/local/sbin/refresh-sing-box-tproxy-setup
systemd-analyze verify "$PROJECT_DIR"/systemd/*
systemctl enable "${services[@]}"
systemctl restart sing-box-dns-forwarder sing-box-tproxy sing-box singbox-rule-ui
systemctl restart update-sing-box-rules.timer monitor-sing-box-runtime.timer sing-box-logrotate.timer sing-box-config-backup.timer
python3 "$PROJECT_DIR/scripts/health_check.py"
cp "$PROJECT_DIR/versions.json" /etc/sing-box/manager/installed-version.json
echo "安装完成。备份: $backup_dir"
/usr/local/bin/sing-box-gateway-info
