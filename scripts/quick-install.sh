#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ]; then
  echo '请使用 root 执行。' >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  apt-get -o APT::Update::Error-Mode=any update
  apt-get install -y --no-install-recommends curl ca-certificates
fi
gateway_tmp=$(mktemp -d)
trap 'rm -rf "$gateway_tmp"' EXIT HUP INT TERM
gateway_ref=${SINGBOX_UI_REF:-main}
gateway_prefix=${GITHUB_DOWNLOAD_PREFIX:-}
curl -fL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 600 "${gateway_prefix}https://github.com/hanigege/singbox-ui-debian13/archive/$gateway_ref.tar.gz" -o "$gateway_tmp/source.tar.gz"
tar -xzf "$gateway_tmp/source.tar.gz" -C "$gateway_tmp" --strip-components=1
bash "$gateway_tmp/scripts/install.sh"
