#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path


APP_DIR = Path(os.environ.get("RULE_UI_APP_DIR", "/opt/singbox-rule-ui"))
LOCK_PATH = Path(os.environ.get("RULE_UI_AUTO_PROXY_LOCK", "/run/sing-box-auto-proxy-learning.lock"))
MAX_PROBES_PER_RUN = 10
PROBE_TIMEOUT_MS = 6000


def read_new_log_lines(path, cursor):
    stat = path.stat()
    identity = {"device": stat.st_dev, "inode": stat.st_ino}
    offset = 0
    if cursor and cursor.get("device") == stat.st_dev and cursor.get("inode") == stat.st_ino:
        offset = int(cursor.get("offset", 0) or 0)
        if stat.st_size < offset:
            offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
        boundary = data.rfind(b"\n")
        if boundary < 0:
            return [], {**identity, "offset": offset}
        complete = data[: boundary + 1]
        lines = complete.decode("utf-8", errors="replace").splitlines()
        return lines, {**identity, "offset": offset + len(complete)}


def log_path_from_config(app):
    output = str(app.load_json(app.CONFIG_PATH, {}).get("log", {}).get("output", "")).strip()
    if not output:
        return None
    path = Path(output)
    return path if path.is_absolute() else app.CONFIG_PATH.parent / path


def active_proxy_tag(app):
    state = app.get_proxy_state()
    if not state.get("ok"):
        return None
    data = state.get("data", {})
    selected = data.get("now")
    if selected == "Auto":
        selected = data.get("autoNow")
    return selected if selected in app.enabled_node_tags(app.load_nodes()) else None


def is_manually_handled(app, domain, lists):
    for name in ("whitelist", "blacklist", "greylist", "ddns"):
        if any(app.auto_domain_matches_entry(domain, entry) for entry in lists.get(name, [])):
            return True
    return False


def discard_candidate(state, domain):
    candidates = dict(state["candidates"])
    candidates.pop(domain, None)
    return {**state, "candidates": candidates}


def run_tick(dry_run=False):
    sys.path.insert(0, str(APP_DIR))
    import app
    from auto_proxy_learning import advance_state, parse_direct_network_failure, record_probe

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": True, "skipped": "another learning run is active"}

        state = app.load_auto_proxy_state()
        path = log_path_from_config(app)
        if path is None or not path.exists():
            return {"ok": True, "skipped": "sing-box log output is not available"}
        if state["cursor"] is None:
            stat = path.stat()
            state["cursor"] = {"device": stat.st_dev, "inode": stat.st_ino, "offset": stat.st_size}
            if not dry_run:
                app.write_json(app.AUTO_PROXY_STATE_PATH, state)
            return {"ok": True, "initialized": True, "learnedCount": len(state["learned"])}

        lines, cursor = read_new_log_lines(path, state["cursor"])
        now = int(time.time())
        failures = [domain for line in lines if (event := parse_direct_network_failure(line)) for domain, _ in [event]]
        next_state, ready = advance_state(state, failures, now)
        next_state["cursor"] = cursor
        lists = {name: app.read_entries(name) for name in app.LISTS}
        tag = active_proxy_tag(app) if next_state["enabled"] and ready else None
        probed = []
        if tag:
            for domain in ready[:MAX_PROBES_PER_RUN]:
                if is_manually_handled(app, domain, lists):
                    next_state = discard_candidate(next_state, domain)
                    continue
                result = app.test_node_delay(tag, url=f"https://{domain}/", timeout_ms=PROBE_TIMEOUT_MS)
                next_state = record_probe(next_state, domain, now, bool(result["ok"]))
                probed.append({"domain": domain, "ok": bool(result["ok"]), "delayMs": result["delay"]})

        previous_domains = set(state["learned"])
        next_domains = set(next_state["learned"])
        route_changed = previous_domains != next_domains
        apply_result = {"ok": True, "changed": False}
        if dry_run:
            return {
                "ok": True,
                "dryRun": True,
                "newLogLines": len(lines),
                "directFailures": failures,
                "probes": probed,
                "wouldAdd": sorted(next_domains - previous_domains),
                "wouldRemove": sorted(previous_domains - next_domains),
            }
        if route_changed:
            apply_result = app.apply_auto_proxy_state(next_state)
            if not apply_result.get("ok"):
                # 配置失败时不承认学习结果；游标和探测冷却仍保留，稍后可重试，不会反复扫旧日志。
                fallback_state = {**next_state, "learned": state["learned"]}
                app.apply_auto_proxy_state(fallback_state)
        else:
            app.write_json(app.AUTO_PROXY_STATE_PATH, next_state)
        return {
            "ok": bool(apply_result.get("ok")),
            "newLogLines": len(lines),
            "directFailures": len(failures),
            "probes": probed,
            "learnedCount": len(next_state["learned"]),
            "apply": apply_result,
        }


def main():
    parser = argparse.ArgumentParser(description="学习多次直连失败且经当前代理验证可达的域名")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true", help="启用自动学习")
    group.add_argument("--disable", action="store_true", help="暂停学习，已有临时规则保留至过期")
    group.add_argument("--clear", action="store_true", help="清空已学习规则并停止候选统计")
    group.add_argument("--status", action="store_true", help="显示开关和临时代理域名")
    parser.add_argument("--dry-run", action="store_true", help="只分析新日志并探测，不保存状态或改配置")
    args = parser.parse_args()
    if args.enable or args.disable or args.clear or args.status:
        sys.path.insert(0, str(APP_DIR))
        import app

        if args.status:
            state = app.load_auto_proxy_state()
            print(json.dumps({
                "enabled": state["enabled"],
                "learnedDomains": sorted(state["learned"]),
                "candidateCount": len(state["candidates"]),
            }, ensure_ascii=False))
            return 0
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print(json.dumps({"ok": False, "error": "another learning run is active"}))
                return 1
            state = app.load_auto_proxy_state()
            if args.enable:
                state["enabled"] = True
            if args.disable:
                state["enabled"] = False
            if args.clear:
                state["candidates"] = {}
                state["learned"] = {}
            result = app.apply_auto_proxy_state(state)
    else:
        result = run_tick(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
