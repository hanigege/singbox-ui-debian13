#!/bin/bash
set -euo pipefail
template_id=${TEMPLATE_ID:-923}
storage=${STORAGE:-local}
bridge=${BRIDGE:-vmbr0}
test "$(id -u)" = 0
command -v qm >/dev/null
command -v virt-customize >/dev/null || { echo '请在 PVE 安装 libguestfs-tools。' >&2; exit 1; }
[[ "$template_id" =~ ^[1-9][0-9]+$ ]] || { echo 'TEMPLATE_ID 必须是有效 VMID。' >&2; exit 1; }
test ! -e "/etc/pve/qemu-server/$template_id.conf"
test ! -e "/etc/pve/lxc/$template_id.conf"
gateway_tmp=$(mktemp -d)
trap 'rm -rf "$gateway_tmp"' EXIT
image=debian-13-generic-amd64.qcow2
base=https://cloud.debian.org/images/cloud/trixie/latest
curl -fL --retry 3 --retry-all-errors "$base/$image" -o "$gateway_tmp/$image"
curl -fL --retry 3 --retry-all-errors "$base/SHA512SUMS" -o "$gateway_tmp/SHA512SUMS"
(cd "$gateway_tmp" && sha512sum --check --ignore-missing SHA512SUMS)
# template 中不保存 SSH host key、machine-id 或 cloud-init 实例身份。
virt-customize -a "$gateway_tmp/$image" --install qemu-guest-agent --run-command 'systemctl enable qemu-guest-agent; cloud-init clean --logs --machine-id; rm -f /etc/ssh/ssh_host_*'
qm create "$template_id" --name debian13-minimal-cloud-template --ostype l26 --cores 2 --memory 1024 --cpu host --net0 "virtio,bridge=$bridge" --scsihw virtio-scsi-single --serial0 socket --vga serial0 --agent enabled=1
qm set "$template_id" --scsi0 "$storage:0,import-from=$gateway_tmp/$image,discard=on,iothread=1" --ide2 "$storage:cloudinit" --boot order=scsi0 --ipconfig0 ip=dhcp,ip6=auto --ciupgrade 0
qm resize "$template_id" scsi0 8G
qm template "$template_id"
echo "模板 $template_id 创建完成；克隆后请设置 cloud-init 用户、SSH key 和固定网络地址。"
