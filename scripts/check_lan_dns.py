#!/usr/bin/env python3
"""从独立 LAN 客户端检查网关双栈 UDP/TCP DNS，不修改路由或网关配置。"""
import argparse
import ipaddress
import json
import secrets
import socket
import struct


def query_packet(domain: str) -> bytes:
    name = b"".join(bytes([len(part)]) + part.encode("ascii") for part in domain.split(".")) + b"\0"
    return struct.pack("!6H", secrets.randbelow(65536), 256, 1, 0, 0, 0) + name + struct.pack("!2H", 28, 1)


def validate_response(packet: bytes, response: bytes) -> int:
    if len(response) < 12 or response[:2] != packet[:2]:
        raise ValueError("DNS 响应长度或 transaction ID 异常")
    _, flags, _, answers, _, _ = struct.unpack("!6H", response[:12])
    if not flags & 0x8000 or flags & 15 or answers == 0:
        raise ValueError(f"DNS 查询失败: rcode={flags & 15}, answers={answers}")
    return answers


def udp_query(server: str, packet: bytes) -> int:
    family = socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        sock.sendto(packet, (server, 53))
        response, _ = sock.recvfrom(65535)
    return validate_response(packet, response)


def read_exact(sock: socket.socket, count: int) -> bytes:
    response = b""
    while len(response) < count:
        chunk = sock.recv(count - len(response))
        if not chunk:
            raise EOFError("DNS TCP 响应提前关闭")
        response += chunk
    return response


def tcp_query(server: str, packet: bytes) -> int:
    with socket.create_connection((server, 53), timeout=5) as sock:
        sock.sendall(len(packet).to_bytes(2, "big") + packet)
        length = int.from_bytes(read_exact(sock, 2), "big")
        response = read_exact(sock, length)
    return validate_response(packet, response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ipv4", type=ipaddress.IPv4Address)
    parser.add_argument("ipv6", type=ipaddress.IPv6Address)
    parser.add_argument("--domain", default="www.gstatic.com")
    args = parser.parse_args()
    for server in (str(args.ipv4), str(args.ipv6)):
        for query in (udp_query, tcp_query):
            answers = query(server, query_packet(args.domain))
            print(json.dumps({"server": server, "transport": query.__name__, "answers": answers, "ok": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
