# singbox-ui-debian13

Debian 13 minimal 上的 sing-box 网关与 Rule UI，一键安装、systemd 管理、IPv4/IPv6 FakeIP + TProxy 分流。基于 [singbox-ui-alpine-1.14](https://github.com/hanigege/singbox-ui-alpine-1.14) 适配，保留节点、黑白灰名单、DDNS、备份导入导出和运行态维护功能。

## 一键安装

适用 Debian 13 minimal 的 VM 或裸机，支持 amd64 / arm64。建议 1–2 核、1 GB RAM、8 GB 磁盘；需要 root 和能访问 Debian、GitHub 的网络。

静态 IP 和 DHCP 均支持。安装器读取系统当前地址作为监听地址，不修改用户已经配置好的 IP、默认网关或网卡。

```bash
curl -fsSL https://raw.githubusercontent.com/hanigege/singbox-ui-debian13/main/scripts/quick-install.sh | bash
```

如果所在网络无法直连 GitHub，可显式选用下载代理（由第三方提供）：

```bash
curl -fsSL --connect-timeout 15 --max-time 60 https://gh-proxy.com/https://raw.githubusercontent.com/hanigege/singbox-ui-debian13/main/scripts/quick-install.sh | GITHUB_DOWNLOAD_PREFIX=https://gh-proxy.com/ bash
```

下载代理同时用于仓库压缩包和官方二进制，二进制仍校验固定 SHA256。已有 HTTP/SOCKS 代理也可以通过 curl 标准环境变量配置。

如果仓库需要代理、而官方 Release 可以直连，可额外设置 `SING_BOX_DOWNLOAD_PREFIX=''`，单独让二进制直连，避免慢速镜像拖慢安装。

最小系统若未安装 curl，先执行：

```bash
apt-get update && apt-get install -y curl ca-certificates
```

可选自定义 UI 密码（至少 6 位、不含空白）：

```bash
curl -fsSL https://raw.githubusercontent.com/hanigege/singbox-ui-debian13/main/scripts/quick-install.sh | RULE_UI_TOKEN='请替换成自己的长密码' bash
```

访问 `http://<网关的局域网 IPv4>:9091`，查看随机密码：

```bash
cat /etc/sing-box/rule-ui/token
```

初装包含示例节点，**需要在 UI 填入真实节点后才能代理上网**。也可以在首次安装时设置 `SING_BOX_INITIAL_NODES_FILE=/root/nodes.json`，内容为 `[ {"enabled":true,"outbound":{...}} ]`。示例凭据不能用于真实网络。

## 版本和安装行为

- 固定官方 sing-box **1.14.4 stable**，下载官方发布包并校验 `versions.json` 中的 SHA256。版本升级通过仓库审核后的 manifest 实施，安装器不追随 latest。
- 使用官方 Linux 二进制和 Debian glibc，无 Alpine 兼容层。不使用系统 apt 的同名 sing-box 包，避免两套服务相互覆盖。
- 重复安装保留 `nodes/groups/base/token`、自定义规则、timer 计划和 `/etc/default/singbox-gateway`。
- 覆盖前备份到 `/var/backups/singbox-ui/`；安装失败恢复已备份文件和之前 active 的服务。系统依赖包保留。新装失败会保留诊断数据，修正原因后可重试。
- 所有 UI 保存与导入都先生成临时配置，执行 `sing-box check`，通过后原子落盘；运行态应用失败回滚。
- nftables 只管理 `inet singbox_tproxy`，整表先校验再以单事务替换。不会 flush 其它防火墙规则。
- 不修改 `/etc/resolv.conf`，不自动停止其它 DNS。若 LAN 53 端口冲突，安装失败并显示占用进程。
- 默认不广播 IPv6 RA、不打开系统转发。需要让机器承担三层网关时，在 `/etc/default/singbox-gateway` 显式启用 forwarding，再重启 TProxy。
- 日志每小时检查大小、保留 6 份压缩文件；systemd 日志按系统 journal 策略管理。

## 网络使用方式

| 服务 | 默认监听 | 用途 |
|---|---|---|
| Rule UI | IPv4 `0.0.0.0:9091` | token 鉴权的管理页面 |
| Clash API | LAN IPv4 `:9090` | 运行态 API，独立随机 secret |
| DNS | LAN IPv4 / 本机 ULA IPv6 `:53` | 国内真实地址、代理域名 FakeIP |
| TProxy | `:9888` | 内核透明代理入口，不是 SOCKS 端口 |
| SOCKS | 默认关闭，可在 UI 开启 | 仅绑定 LAN IPv4 |

请在可信局域网使用管理端口。公网 VPS 应通过 SSH 隧道或已有防火墙限制管理入口。仓库不会改宿主机防火墙。

客户端 DNS 指向此机。FakeIP 默认 `28.0.0.0/8` 和 `2001:2::/64`，上游路由器需要把这两个网段路由到本机 IPv4/IPv6 地址；显式灰名单 IP/CIDR 与 Telegram IP 捕获同样需要流量经过本机。真实地址直连流量继续走现有上游网关。仅修改 DNS、没有匹配路由时，FakeIP 无法到达透明代理。

建议为部署机器设置 DHCP 静态租约或 cloud-init 固定地址。每两分钟检测 IP/IPv6 前缀变化，仅发生变化或服务异常时重新应用。

网关操作系统自身应使用返回真实 IP 的 DNS，例如 `223.5.5.5`；不要继承另一台网关的 FakeIP DNS，否则同一个 `28.x` 地址可能在两台机器上对应不同域名，导致 APT/HTTPS 失败。安装器检测到常见 FakeIP 地址会明确停止。PVE 模板默认设置真实上游 DNS，可通过 `DNS_SERVER` 修改。

## 日常维护

```bash
sing-box-gateway-info
sing-box check -c /etc/sing-box/config.json
systemctl status sing-box sing-box-tproxy singbox-rule-ui
journalctl -u singbox-rule-ui -u sing-box -e
tail -n 100 /var/log/sing-box-gateway/sing-box.log
systemctl list-timers '*sing-box*'
```

规则默认每周日 04:20 更新，随机延迟最多 15 分钟；systemd timer 的 `Persistent=true` 会补跑关机错过的任务。可在 UI 修改时间，保存在 timer drop-in 中。维护页能检查配置健康、规则文件、IPv6 前缀、节点状态。

计划使用系统时区；可用 `timedatectl set-timezone Asia/Shanghai` 设置北京时间。

```text
/etc/sing-box/config.json               生效配置
/etc/sing-box/manager/                  节点、分组、基线、备份
/etc/sing-box/custom-rules/             黑白灰名单与 DDNS
/etc/default/singbox-gateway            管理监听与可选 forwarding/RA
/opt/singbox-rule-ui/                   UI 程序
/var/log/sing-box-gateway/              sing-box 和规则更新日志
/var/backups/singbox-ui/                安装前完整备份
```

## 从 Alpine 迁移

先在 Alpine UI 导出备份，在 Debian 新 UI 导入。导入仅采用节点、分组和规则，新机器继续使用本机 IP、systemd 和 1.14 的配置渲染逻辑；不复制 OpenRC、网卡、路由器或宿主机配置。导入后校验节点延迟与真实上网，再调整路由器指向新机器。

原生产网关可继续运行，验证完成后再切换。备份包含节点密码，请妥善保管，勿提交到 GitHub。

## PVE 模板

使用官方 [Debian 13 cloud 镜像](https://cloud.debian.org/images/cloud/trixie/latest/) 的 `generic` 版本，无桌面。`scripts/pve-create-template.sh` 在 PVE 下载镜像、校验 SHA512、安装 Guest Agent、清除机器身份并创建 cloud-init 模板；默认仅创建干净 OS 模板，不包含节点和密码。

```bash
# 在 PVE 上执行；选择未占用的 VMID。
TEMPLATE_ID=923 STORAGE=local BRIDGE=vmbr0 bash scripts/pve-create-template.sh
```

克隆后在 PVE cloud-init 配置 SSH key、用户、IPv4、IPv6 和 DNS，再启动 VM 并执行上面的一键安装。已存在的 VMID 会被拒绝，脚本不会覆盖 VM。

## 卸载

```bash
sing-box-gateway-uninstall
```

停用并移除本项目 unit、TProxy 规则和 sysctl 文件；默认保留程序、配置、节点、密码和日志，便于重装。卸载前另存配置备份，系统依赖包不会被移除。

## 验证

```bash
python3 scripts/check_config_idempotency.py
python3 scripts/health_check.py  # 需要已安装的 Debian 13
python3 scripts/acceptance_test.py  # 在独立测试机执行，会保存/导入配置并重启服务
```

新版本必须在 Debian 13 VM 验证首次安装、复装、非法配置拒绝、备份导入导出、systemd timer、IPv4/IPv6 FakeIP、nftables、整机重启和保留数据卸载/重装。
