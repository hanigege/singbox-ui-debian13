"""直连失败域名的临时代理学习规则。"""

from __future__ import annotations

import ipaddress
import re


FAILURE_WINDOW_SECONDS = 10 * 60
FAILURE_THRESHOLD = 3
PROBE_INTERVAL_SECONDS = 15 * 60
LEARNED_TTL_SECONDS = 24 * 60 * 60
MAX_CANDIDATES = 500
MAX_LEARNED_DOMAINS = 500

DIRECT_FAILURE_RE = re.compile(
    r"\bconnection: open connection to (?P<target>\[[^]]+\]|[^\s]+) "
    r"using outbound/direct\[direct\]: (?P<error>.+)$"
)
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
RETRYABLE_ERRORS = (
    "i/o timeout",
    "connection timed out",
    "connection refused",
    "connection reset by peer",
    "network is unreachable",
    "no route to host",
)
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".test", ".invalid", ".example")


def normalize_domain(value: str) -> str | None:
    domain = value.strip().rstrip(".").lower()
    if not domain or len(domain) > 253 or any(domain.endswith(suffix) for suffix in LOCAL_SUFFIXES):
        return None
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in domain or not DOMAIN_RE.fullmatch(domain):
        return None
    return domain


def parse_direct_network_failure(line: str) -> tuple[str, int] | None:
    if " ERROR " not in line:
        return None
    match = DIRECT_FAILURE_RE.search(line)
    if not match:
        return None
    error = match.group("error").lower()
    if not any(marker in error for marker in RETRYABLE_ERRORS):
        return None
    target = match.group("target")
    if target.startswith("["):
        return None
    host, separator, raw_port = target.rpartition(":")
    if not separator or not raw_port.isdigit() or int(raw_port) != 443:
        return None
    domain = normalize_domain(host)
    return (domain, int(raw_port)) if domain else None


def empty_state() -> dict:
    return {"version": 1, "enabled": True, "cursor": None, "candidates": {}, "learned": {}}


def normalize_state(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("自动代理学习状态文件格式无效")
    candidates = raw.get("candidates", {})
    learned = raw.get("learned", {})
    cursor = raw.get("cursor")
    if not isinstance(candidates, dict) or not isinstance(learned, dict):
        raise ValueError("自动代理学习状态中的候选或规则列表无效")
    if cursor is not None and not isinstance(cursor, dict):
        raise ValueError("自动代理学习日志游标无效")
    normalized_candidates = {}
    for raw_domain, item in candidates.items():
        domain = normalize_domain(str(raw_domain))
        if not domain or not isinstance(item, dict):
            continue
        failures = item.get("failures", [])
        if not isinstance(failures, list):
            continue
        timestamps = [int(value) for value in failures if isinstance(value, int) and value >= 0]
        normalized_candidates[domain] = {
            "failures": timestamps[-FAILURE_THRESHOLD:],
            "lastProbeAt": max(0, int(item.get("lastProbeAt", 0) or 0)),
        }
    normalized_learned = {}
    for raw_domain, item in learned.items():
        domain = normalize_domain(str(raw_domain))
        if not domain or not isinstance(item, dict):
            continue
        learned_at = max(0, int(item.get("learnedAt", 0) or 0))
        expires_at = max(0, int(item.get("expiresAt", 0) or 0))
        if learned_at and expires_at > learned_at:
            normalized_learned[domain] = {"learnedAt": learned_at, "expiresAt": expires_at}
    return {
        "version": 1,
        "enabled": raw.get("enabled") is not False,
        "cursor": cursor,
        "candidates": normalized_candidates,
        "learned": normalized_learned,
    }


def advance_state(raw_state: dict, failures: list[str], now: int) -> tuple[dict, list[str]]:
    state = normalize_state(raw_state)
    cutoff = now - FAILURE_WINDOW_SECONDS
    candidates = {
        domain: {
            "failures": [stamp for stamp in item["failures"] if stamp >= cutoff],
            "lastProbeAt": item["lastProbeAt"],
        }
        for domain, item in state["candidates"].items()
    }
    candidates = {domain: item for domain, item in candidates.items() if item["failures"]}
    for domain in failures:
        normalized = normalize_domain(domain)
        if not normalized:
            continue
        item = candidates.setdefault(normalized, {"failures": [], "lastProbeAt": 0})
        item["failures"] = [stamp for stamp in item["failures"] if stamp >= cutoff]
        item["failures"].append(now)
        item["failures"] = item["failures"][-FAILURE_THRESHOLD:]
    if len(candidates) > MAX_CANDIDATES:
        candidates = dict(sorted(candidates.items(), key=lambda pair: pair[1]["failures"][-1], reverse=True)[:MAX_CANDIDATES])
    learned = {
        domain: item
        for domain, item in state["learned"].items()
        if item["expiresAt"] > now
    }
    next_state = {**state, "candidates": candidates, "learned": learned}
    ready = []
    if state["enabled"]:
        for domain, item in candidates.items():
            if domain in learned or len(item["failures"]) < FAILURE_THRESHOLD:
                continue
            if now - item["lastProbeAt"] >= PROBE_INTERVAL_SECONDS:
                ready.append(domain)
    return next_state, sorted(ready)


def record_probe(raw_state: dict, domain: str, now: int, succeeded: bool) -> dict:
    state = normalize_state(raw_state)
    normalized = normalize_domain(domain)
    if not normalized:
        raise ValueError("自动代理学习的探测域名无效")
    candidates = dict(state["candidates"])
    item = dict(candidates.get(normalized, {"failures": [], "lastProbeAt": 0}))
    item["lastProbeAt"] = now
    candidates[normalized] = item
    learned = dict(state["learned"])
    if succeeded:
        learned[normalized] = {"learnedAt": now, "expiresAt": now + LEARNED_TTL_SECONDS}
        learned = dict(sorted(learned.items(), key=lambda pair: pair[1]["learnedAt"], reverse=True)[:MAX_LEARNED_DOMAINS])
    return {**state, "candidates": candidates, "learned": learned}
