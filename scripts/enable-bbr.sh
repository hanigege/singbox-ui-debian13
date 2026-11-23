#!/bin/sh
set -eu

if [ "$(id -u)" != 0 ]; then
  echo '请使用 root 执行。' >&2
  exit 1
fi

. /etc/os-release
if [ "$ID" != debian ] || [ "$VERSION_ID" != 13 ]; then
  echo '此脚本只支持 Debian 13。' >&2
  exit 1
fi

container=$(systemd-detect-virt --container 2>/dev/null || true)
case "$container" in
  ''|none) ;;
  *)
    echo "检测到容器环境 $container；BBR 由宿主机内核管理，请在宿主机配置。" >&2
    exit 1
    ;;
esac

available=$(sysctl -n net.ipv4.tcp_available_congestion_control)
case " $available " in
  *' bbr '*) ;;
  *)
    command -v modprobe >/dev/null 2>&1 || {
      echo '缺少 modprobe，请先安装 Debian 的 kmod 包。' >&2
      exit 1
    }
    modprobe tcp_bbr
    ;;
esac

available=$(sysctl -n net.ipv4.tcp_available_congestion_control)
case " $available " in
  *' bbr '*) ;;
  *)
    echo "当前内核不提供 BBR；可用算法: $available" >&2
    exit 1
    ;;
esac

bbr_tmp=$(mktemp -d)
trap 'rm -rf "$bbr_tmp"' EXIT HUP INT TERM
cat > "$bbr_tmp/99-singbox-gateway-bbr.conf" <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
printf '%s\n' tcp_bbr > "$bbr_tmp/singbox-gateway-bbr.conf"

# 先验证当前内核接受参数，再安装持久化文件，避免留下无法应用的启动配置。
sysctl -p "$bbr_tmp/99-singbox-gateway-bbr.conf"
test "$(sysctl -n net.core.default_qdisc)" = fq
test "$(sysctl -n net.ipv4.tcp_congestion_control)" = bbr

install -m 0644 "$bbr_tmp/99-singbox-gateway-bbr.conf" /etc/sysctl.d/99-singbox-gateway-bbr.conf
install -m 0644 "$bbr_tmp/singbox-gateway-bbr.conf" /etc/modules-load.d/singbox-gateway-bbr.conf

echo 'BBR 已启用，无需重启。'
sysctl net.core.default_qdisc net.ipv4.tcp_congestion_control
