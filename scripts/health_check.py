#!/usr/bin/env python3
"""安装后等待真实 API 就绪，并检查服务和配置。"""
import json
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def main() -> None:
    subprocess.run(["/usr/local/bin/sing-box", "check", "-c", "/etc/sing-box/config.json"], check=True)
    subprocess.run(["systemctl", "is-active", "sing-box", "sing-box-tproxy", "singbox-rule-ui", "sing-box-auto-proxy-watch"], check=True)
    token = Path("/etc/sing-box/rule-ui/token").read_text().strip()
    last_error: OSError | None = None
    for attempt in range(20):
        try:
            request = Request("http://127.0.0.1:9091/api/maintenance", headers={"Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=5) as response:
                health = json.load(response)["maintenance"]
            if not health["configHealth"]["ok"]:
                summary = json.dumps(health["configHealth"]["summary"], ensure_ascii=True)
                raise RuntimeError(f"Configuration health check failed: {summary}")
            print("Rule UI HTTP 200; configuration health check passed.")
            return
        except (URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
            print(json.dumps({"warning": "waiting for Rule UI", "attempt": attempt + 1, "error": str(exc)}, ensure_ascii=True))
            time.sleep(1)
    if last_error is not None:
        raise last_error


if __name__ == "__main__":
    main()
