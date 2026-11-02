#!/usr/bin/env python3
"""真实节点配置完成后，验证双栈 DNS 和 FakeIP 透明代理。"""
import ipaddress
import json
import secrets
import socket
import struct
import subprocess
from pathlib import Path


def skip_name(data: bytes, offset: int) -> int:
    while data[offset]:
        if data[offset] & 0xC0 == 0xC0:
            return offset + 2
        offset += data[offset] + 1
    return offset + 1


def query(server: str, name: str, record_type: int) -> list[str]:
    txid = secrets.randbelow(65536)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\0"
    packet = struct.pack("!6H", txid, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!2H", record_type, 1)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    response_id, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if response_id != txid or flags & 15:
        raise RuntimeError(f"DNS 响应异常: server={server}, flags={flags}")
    offset = 12
    for _ in range(questions):
        offset = skip_name(data, offset) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_name(data, offset)
        kind, _, _, length = struct.unpack("!2HIH", data[offset:offset + 10])
        offset += 10
        if kind in (1, 28):
            addresses.append(str(ipaddress.ip_address(data[offset:offset + length])))
        offset += length
    return addresses


def main() -> None:
    config = json.loads(Path("/etc/sing-box/config.json").read_text())
    ipv4 = next(item["listen"] for item in config["inbounds"] if item["tag"] == "dns-in")
    domain = "www.gstatic.com"
    for version, kind, network in ((4, 1, "28.0.0.0/8"), (6, 28, "2001:2::/64")):
        answers = query(ipv4, domain, kind)
        fakeip = next(address for address in answers if ipaddress.ip_address(address) in ipaddress.ip_network(network))
        print(f"DNS AAAA/A -> FakeIP IPv{version}: {fakeip}")
        resolve_address = f"[{fakeip}]" if version == 6 else fakeip
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "25", "--noproxy", "*", "--resolve", f"{domain}:443:{resolve_address}", f"https://{domain}/generate_204", "-o", "/dev/null", "-w", f"FakeIP IPv{version} HTTPS: %{{http_code}}\n"], check=True)
    for inbound in config["inbounds"]:
        if inbound["tag"] == "dns-in-v6":
            assert query(inbound["listen"], domain, 28)
            print("IPv6 ULA DNS: PASS")
    direct = query(ipv4, "www.baidu.com", 1)
    assert direct and all(ipaddress.ip_address(item) not in ipaddress.ip_network("28.0.0.0/8") for item in direct)
    print("国内 DNS 真实地址: PASS")


if __name__ == "__main__":
    main()
