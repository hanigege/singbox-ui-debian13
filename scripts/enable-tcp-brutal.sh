#!/bin/sh
# 可选启用 TCP Brutal 内核模块（DKMS），与 scripts/enable-bbr.sh 同为独立脚本，
# 不并入一键安装：模块需要按当前内核编译，受内核与虚拟化环境影响，
# 应在确认 sing-box 全套运行正常后单独执行。服务端与客户端（网关）两侧都要装，
# Brutal 只控制本机发送方向，只装一侧只有一侧受益。见 README「可选启用 TCP Brutal」。
set -eu

# 与 versions.json 的 tcp_brutal 段保持一致：固定上游版本与校验和，不追随 latest。
TCP_BRUTAL_VERSION=1.0.3
TCP_BRUTAL_URL="https://github.com/HyNetworks/tcp-brutal/releases/download/v${TCP_BRUTAL_VERSION}/tcp-brutal.dkms.tar.gz"
TCP_BRUTAL_SHA256=38526721f2e8a8c1907eb289d80526fc2bee9ca09b1f89d91362cea8ef1aad04

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
    echo "检测到容器环境 $container；内核模块由宿主机管理，请在宿主机安装。" >&2
    exit 1
    ;;
esac

brutal_available() {
  available=$(sysctl -n net.ipv4.tcp_available_congestion_control)
  case " $available " in
    *' brutal '*) return 0 ;;
    *) return 1 ;;
  esac
}

if brutal_available; then
  echo '当前内核已提供 brutal，无需重复安装。'
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # dkms 会带入编译工具链；内核头必须与正在运行的内核版本一致。
  apt-get install -y --no-install-recommends ca-certificates curl dkms "linux-headers-$(uname -r)"

  brutal_tmp=$(mktemp -d)
  trap 'rm -rf "$brutal_tmp"' EXIT HUP INT TERM
  curl --fail --location --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 \
    --output "$brutal_tmp/tcp-brutal.dkms.tar.gz" "${GITHUB_DOWNLOAD_PREFIX:-}$TCP_BRUTAL_URL"
  echo "$TCP_BRUTAL_SHA256  $brutal_tmp/tcp-brutal.dkms.tar.gz" | sha256sum -c -
  tar -xzf "$brutal_tmp/tcp-brutal.dkms.tar.gz" -C "$brutal_tmp"

  # DKMS 源码树固定放在 /usr/src/tcp-brutal-<版本>；复装时先清掉旧树再放新树。
  rm -rf "/usr/src/tcp-brutal-$TCP_BRUTAL_VERSION"
  cp -a "$brutal_tmp/dkms_source_tree" "/usr/src/tcp-brutal-$TCP_BRUTAL_VERSION"
  if ! dkms status "tcp-brutal/$TCP_BRUTAL_VERSION" 2>/dev/null | grep -q .; then
    dkms add "tcp-brutal/$TCP_BRUTAL_VERSION"
  fi
  if ! dkms status "tcp-brutal/$TCP_BRUTAL_VERSION" 2>/dev/null | grep -q installed; then
    dkms build "tcp-brutal/$TCP_BRUTAL_VERSION"
    dkms install "tcp-brutal/$TCP_BRUTAL_VERSION"
  fi
fi

# 开机自动加载；DKMS 会在内核升级后自动为新内核重编模块。
brutal_load_tmp=$(mktemp)
printf '%s\n' brutal > "$brutal_load_tmp"
install -m 0644 "$brutal_load_tmp" /etc/modules-load.d/brutal.conf
rm -f "$brutal_load_tmp"

command -v modprobe >/dev/null 2>&1 || {
  echo '缺少 modprobe，请先安装 Debian 的 kmod 包。' >&2
  exit 1
}
modprobe brutal

brutal_available || {
  echo "模块已安装但当前内核仍未提供 brutal；可用算法: $(sysctl -n net.ipv4.tcp_available_congestion_control)" >&2
  exit 1
}

echo 'TCP Brutal 已启用，无需重启。'
echo '提醒：Brutal 只控制本机发送方向，服务端与客户端（网关）两侧都要安装；'
echo 'Rule UI 里 VLESS 节点选 Brutal 并按本端线路实际带宽填上/下行后才会实际使用。'
sysctl net.ipv4.tcp_available_congestion_control
