#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0 || { echo '请使用 root 执行。' >&2; exit 1; }
backup_dir=/var/backups/singbox-ui/uninstall-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup_dir"
if [ -d /etc/sing-box ]; then tar -C /etc -cpf "$backup_dir/config.tar" sing-box; fi
units=(monitor-sing-box-runtime.timer update-sing-box-rules.timer sing-box-logrotate.timer singbox-rule-ui.service sing-box.service sing-box-tproxy.service monitor-sing-box-runtime.service update-sing-box-rules.service sing-box-logrotate.service)
for unit in "${units[@]}"; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl stop "$unit"
    systemctl disable "$unit" 2>/dev/null
  fi
done
for unit in "${units[@]}"; do rm -f "/etc/systemd/system/$unit"; done
rm -f /etc/sysctl.d/99-sing-box-tproxy.conf /etc/logrotate.d/sing-box-gateway
systemctl daemon-reload
echo "已停用并移除本项目服务。配置、节点、token、程序及日志保留，方便重装；备份: $backup_dir"
