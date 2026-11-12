#!/usr/bin/env python3
"""从真实直连 HTTPS 会话发现域名，补充日志无法反映的 TLS 故障学习。"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Literal, TypedDict, cast


APP_DIR = Path(os.environ.get("RULE_UI_APP_DIR", "/opt/singbox-rule-ui"))
LOCK_PATH = Path(os.environ.get("RULE_UI_AUTO_PROXY_LOCK", "/run/sing-box-auto-proxy-learning.lock"))
POLL_INTERVAL_SECONDS = 0.1
PROBE_TIMEOUT_MS = 5000
PROBE_COOLDOWN_SECONDS = 15 * 60
MAX_QUEUE_SIZE = 128
CN_ROUTE_MARKERS = (
    "geosite-cn", "geosite-geolocation-cn", "geosite-icloud@cn", "geosite-apple@cn", "geoip-cn",
)
IGNORED_ROUTE_MARKERS = ("geosite-speedtest", "custom-whitelist", "custom-ddns")

sys.path.insert(0, str(APP_DIR))
import app
from auto_proxy_learning import advance_state, normalize_domain, record_probe


class LogCursor(TypedDict):
    device: int
    inode: int
    offset: int


class Candidate(TypedDict):
    failures: list[int]
    lastProbeAt: int


class LearnedDomain(TypedDict):
    learnedAt: int
    expiresAt: int


class LearningState(TypedDict):
    version: int
    enabled: bool
    cursor: LogCursor | None
    candidates: dict[str, Candidate]
    learned: dict[str, LearnedDomain]


ProbeStatus = Literal["busy", "disabled", "already-learned", "cooldown", "manual", "direct-ok", "proxy-failed", "learned"]


class ProbeResult(TypedDict):
    status: ProbeStatus
    cooldown: int
    proxyDelayMs: int | None


def direct_cn_domain(connection: object) -> str | None:
    """只接收国内分类直连的 TCP/443 域名，排除明确的人工直连规则。"""
    if not isinstance(connection, dict):
        return None
    metadata = connection.get("metadata")
    chains = connection.get("chains")
    rule = connection.get("rule")
    if not isinstance(metadata, dict) or not isinstance(chains, list) or not isinstance(rule, str):
        return None
    if metadata.get("network") != "tcp" or str(metadata.get("destinationPort")) != "443":
        return None
    if "direct" not in chains or "route(direct)" not in rule:
        return None
    if any(marker in rule for marker in IGNORED_ROUTE_MARKERS):
        return None
    if not any(marker in rule for marker in CN_ROUTE_MARKERS):
        return None
    host = metadata.get("host")
    return normalize_domain(host) if isinstance(host, str) else None


def eligibility(state: LearningState, domain: str, now: int) -> ProbeResult | None:
    if not state["enabled"]:
        return ProbeResult(status="disabled", cooldown=60, proxyDelayMs=None)
    if domain in state["learned"]:
        return ProbeResult(status="already-learned", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=None)
    last_probe = state["candidates"].get(domain, {}).get("lastProbeAt", 0)
    remaining = PROBE_COOLDOWN_SECONDS - (now - last_probe)
    if remaining > 0:
        return ProbeResult(status="cooldown", cooldown=remaining, proxyDelayMs=None)
    return None


def manually_handled(domain: str) -> bool:
    lists = {name: app.read_entries(name) for name in app.LISTS}
    return app.is_manually_handled(domain, lists)


def failed_candidate_state(state: LearningState, domain: str, now: int) -> LearningState:
    updated, _ = advance_state(state, [domain], now)
    candidates = dict(updated["candidates"])
    candidates[domain] = {**candidates[domain], "lastProbeAt": now}
    return cast(LearningState, {**updated, "candidates": candidates})


def probe_domain(domain: str) -> ProbeResult:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ProbeResult(status="busy", cooldown=2, proxyDelayMs=None)

        state = cast(LearningState, app.load_auto_proxy_state())
        skipped = eligibility(state, domain, int(time.time()))
        if skipped:
            return skipped
        if manually_handled(domain):
            return ProbeResult(status="manual", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=None)

        url = f"https://{domain}/"
        direct_ok = app.test_node_delay("direct", url=url, timeout_ms=PROBE_TIMEOUT_MS)["ok"]
        # TCP 已连通后的 TLS EOF 不会成为 sing-box 建连错误；主动确认两次以减少瞬时误学。
        if not direct_ok:
            direct_ok = app.test_node_delay("direct", url=url, timeout_ms=PROBE_TIMEOUT_MS)["ok"]
        proxy = None if direct_ok else app.test_node_delay("Proxy", url=url, timeout_ms=PROBE_TIMEOUT_MS)

        # 探测期间 UI 可能暂停学习、删除规则或修改白名单；写入前重新读取，保留最新状态。
        state = cast(LearningState, app.load_auto_proxy_state())
        skipped = eligibility(state, domain, int(time.time()))
        if skipped:
            return skipped
        if manually_handled(domain):
            return ProbeResult(status="manual", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=None)
        if direct_ok:
            if domain in state["candidates"]:
                candidates = dict(state["candidates"])
                candidates.pop(domain)
                app.write_json(app.AUTO_PROXY_STATE_PATH, {**state, "candidates": candidates})
            return ProbeResult(status="direct-ok", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=None)

        now = int(time.time())
        updated = failed_candidate_state(state, domain, now)
        if not proxy or not proxy["ok"]:
            app.write_json(app.AUTO_PROXY_STATE_PATH, updated)
            return ProbeResult(status="proxy-failed", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=None)
        updated = record_probe(updated, domain, now, True)
        applied = app.apply_auto_proxy_state(updated)
        if not applied.get("ok"):
            raise RuntimeError(f"自动学习规则应用失败: {applied.get('error', 'unknown error')}")
        delay = proxy.get("delay")
        return ProbeResult(status="learned", cooldown=PROBE_COOLDOWN_SECONDS, proxyDelayMs=delay if isinstance(delay, int) else None)


def main() -> None:
    stopped = Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    queue: deque[str] = deque()
    queued: set[str] = set()
    in_flight: dict[Future[ProbeResult], str] = {}
    checked_until: dict[str, float] = {}
    last_api_warning = 0.0

    # 写学习状态共用一个 flock；单个探测线程继续允许主循环捕获短连接，不产生无效并发。
    with ThreadPoolExecutor(max_workers=1) as pool:
        while not stopped.is_set():
            now_mono = time.monotonic()
            checked_until = {domain: until for domain, until in checked_until.items() if until > now_mono}
            snapshot = app.clash_api_request("/connections", timeout=2)
            if snapshot.get("ok"):
                data = snapshot.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
                    raise ValueError("Clash connections 响应缺少 connections 数组")
                for connection in data["connections"]:
                    domain = direct_cn_domain(connection)
                    if not domain or domain in queued or domain in in_flight.values() or domain in checked_until:
                        continue
                    if len(queue) >= MAX_QUEUE_SIZE:
                        break
                    queue.append(domain)
                    queued.add(domain)
            elif now_mono - last_api_warning >= 60:
                print(json.dumps({"event": "connections-api-unavailable", "code": snapshot.get("code"), "error": snapshot.get("error")}, ensure_ascii=False), flush=True)
                last_api_warning = now_mono

            for future, domain in list(in_flight.items()):
                if not future.done():
                    continue
                del in_flight[future]
                try:
                    result = future.result()
                except (OSError, ValueError, RuntimeError) as exc:
                    checked_until[domain] = time.monotonic() + 60
                    print(json.dumps({"event": "probe-error", "domain": domain, "error": str(exc)}, ensure_ascii=False), flush=True)
                    continue
                checked_until[domain] = time.monotonic() + result["cooldown"]
                if result["status"] == "busy":
                    # 原日志学习器持锁时保留队列，不能因为短连接已关闭而永久丢掉候选。
                    queue.appendleft(domain)
                    queued.add(domain)
                elif result["status"] in {"learned", "proxy-failed"}:
                    print(json.dumps({"event": "domain-" + result["status"], "domain": domain, "proxyDelayMs": result["proxyDelayMs"]}, ensure_ascii=False), flush=True)

            if queue and not in_flight and checked_until.get(queue[0], 0) <= time.monotonic():
                domain = queue.popleft()
                queued.remove(domain)
                in_flight[pool.submit(probe_domain, domain)] = domain
                print(json.dumps({"event": "domain-check-started", "domain": domain}, ensure_ascii=False), flush=True)
            stopped.wait(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
