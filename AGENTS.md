# 维护规范

本仓库适配 Debian 13 minimal + systemd，基于 singbox-ui-alpine-1.14 复用 Rule UI。

- 默认使用中文说明、注释和 Docstring。
- 所有影响配置、节点、规则及 TProxy 的修改，必须先 staged check，再原子落盘；失败恢复上一份可启动状态。
- 只管理本项目的 nftables 表，不 flush 全局 ruleset，不修改上游路由器和宿主机网络。
- 使用官方 sing-box 发布资产，在 versions.json 固定版本与 SHA256。升级时一起验证配置迁移。
- 复装保留 nodes/groups/base/token、规则和定时计划；卸载默认保留数据。
- systemd unit 避免用 Requires/PartOf 把 UI 请求与 sing-box 重启绑在一起。
- 修改后检查 Python/Shell 语法和渲染幂等性；安装流程必须在干净 Debian 13 VM 实测安装、复装、重启、备份导入导出和卸载。
- 凭据、真实节点、API token、测试机 IP 和 SSH key 不得提交到仓库。

## 三仓同步维护

| 仓库 | 环境与版本线 |
|------|--------------|
| `hanigege/singbox-ui-alpine` | Alpine/OpenRC，sing-box 1.13 系列 |
| `hanigege/singbox-ui-alpine-1.14` | Alpine/OpenRC，sing-box 1.14 系列 |
| `hanigege/singbox-ui-debian13` | Debian 13/systemd，sing-box 1.14 系列 |

三个仓库均保留在线并持续维护。修改 Rule UI、名单模板或共用业务逻辑时，检查并同步适用变更；某仓不适用时在交付说明中写明原因。二进制、安装器、依赖、服务管理和平台相关代码分别适配、分别验证，不直接整文件覆盖。
