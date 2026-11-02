#!/usr/bin/env python3
"""检查正式监听地址的端口冲突，允许本项目运行中的 sing-box。"""
import json
import subprocess
from pathlib import Path


def main() -> None:
    config = json.loads(Path("/etc/sing-box/config.json").read_text())
    targets = {str(item["listen"]) for item in config["inbounds"] if item.get("listen_port") == 53}
    result = subprocess.run(["ss", "-H", "-ltnup", "sport = :53"], check=True, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        address = parts[4].rsplit(":", 1)[0].strip("[]")
        if "sing-box" not in line and address in targets | {"0.0.0.0", "::", "*"}:
            raise RuntimeError(f"DNS 53 端口被占用，请调整服务监听地址: {line}")


if __name__ == "__main__":
    main()
