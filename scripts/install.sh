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
apt-get install -y --no-install-recommends ca-certificates curl python3 nftables iproute2 iputils-ping rsync util-linux logrotate procps
gateway_tmp=$(mktemp -d)
backup_dir=/var/backups/singbox-ui/$(date +%Y%m%d-%H%M%S)-$$
mkdir -p "$backup_dir"
services=(sing-box.service sing-box-tproxy.service singbox-rule-ui.service update-sing-box-rules.timer monitor-sing-box-runtime.timer sing-box-logrotate.timer)
for service in "${services[@]}"; do
  if systemctl is-active --quiet "$service"; then echo "$service" >> "$backup_dir/active-services"; fi
  if systemctl is-enabled --quiet "$service"; then echo "$service" >> "$backup_dir/enabled-services"; fi
done
paths=(etc/sing-box opt/singbox-rule-ui usr/local/bin/sing-box usr/local/lib/sing-box etc/default/singbox-gateway etc/sysctl.d/99-sing-box-tproxy.conf etc/logrotate.d/sing-box-gateway etc/systemd/system/update-sing-box-rules.timer.d)
for unit in "$PROJECT_DIR"/systemd/*; do paths+=("etc/systemd/system/$(basename "$unit")"); done
for helper in refresh-sing-box-runtime-config monitor-sing-box-runtime update-sing-box-rules-jsdelivr refresh-sing-box-tproxy-setup sing-box-tproxy-setup stop-sing-box-tproxy; do paths+=("usr/local/sbin/$helper"); done
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

# 固定官方资产和校验值，不跟随 latest 自动改变二进制。
arch=$(dpkg --print-architecture)
readarray -t asset < <(python3 - "$PROJECT_DIR/versions.json" "$arch" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
asset = data["assets"][sys.argv[2]]
print(data["version"])
print(asset["url"])
print(asset["sha256"])
PY
)
test "${#asset[@]}" = 3 || { echo "不支持架构: $arch" >&2; exit 1; }
curl -fL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 180 "${GITHUB_DOWNLOAD_PREFIX:-}${asset[1]}" -o "$gateway_tmp/sing-box.tar.gz"
echo "${asset[2]}  $gateway_tmp/sing-box.tar.gz" | sha256sum -c -
mkdir "$gateway_tmp/binary"
tar -xzf "$gateway_tmp/sing-box.tar.gz" -C "$gateway_tmp/binary" --strip-components=1
"$gateway_tmp/binary/sing-box" version
mkdir -p /usr/local/bin /usr/local/sbin /usr/local/lib/sing-box /opt/singbox-rule-ui /etc/sing-box/manager /etc/sing-box/custom-rules /var/log/sing-box-gateway
# 一个安装器只管理自己创建的网关；已有第三方配置需要显式导入。
if [ -f /etc/sing-box/config.json ] && [ ! -f /etc/sing-box/manager/base.json ]; then
  echo '检测到第三方 sing-box 配置，请先备份后通过 Rule UI 导入节点。' >&2
  exit 1
fi
install_mutated=1
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
install -m 0644 "$PROJECT_DIR"/systemd/* /etc/systemd/system/
if [ ! -f /etc/default/singbox-gateway ]; then
  install -m 0600 "$PROJECT_DIR/templates/gateway.env" /etc/default/singbox-gateway
fi
set -a
. /etc/default/singbox-gateway
set +a
python3 "$PROJECT_DIR/scripts/bootstrap_config.py"
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
systemctl restart sing-box-tproxy sing-box singbox-rule-ui
systemctl start update-sing-box-rules.timer monitor-sing-box-runtime.timer sing-box-logrotate.timer
python3 "$PROJECT_DIR/scripts/health_check.py"
cp "$PROJECT_DIR/versions.json" /etc/sing-box/manager/installed-version.json
echo "安装完成。备份: $backup_dir"
/usr/local/bin/sing-box-gateway-info
