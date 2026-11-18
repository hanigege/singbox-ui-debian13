#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit


CONFIG_PATH = Path("/etc/sing-box/config.json")
APP_DIR = Path("/opt/singbox-rule-ui")
SING_BOX_BIN = Path("/usr/local/bin/sing-box")


def default_lan_ip():
    out = subprocess.check_output(["ip", "-o", "-4", "route", "get", "1.1.1.1"], text=True)
    parts = out.split()
    if "src" not in parts:
        raise RuntimeError("cannot detect LAN IPv4 address")
    return parts[parts.index("src") + 1]


def assigned_ipv6_addresses():
    try:
        out = subprocess.check_output(["ip", "-o", "-6", "addr", "show", "scope", "global"], text=True)
    except Exception:
        return []
    addresses = []
    for line in out.splitlines():
        parts = line.split()
        if "inet6" not in parts:
            continue
        value = parts[parts.index("inet6") + 1].split("/", 1)[0]
        try:
            addresses.append(ipaddress.IPv6Address(value))
        except ValueError:
            continue
    return addresses


def preferred_ipv6_listener(lan_ip):
    addresses = assigned_ipv6_addresses()
    if not addresses:
        return ""
    expected_candidates = []
    try:
        last_octet = int(ipaddress.IPv4Address(lan_ip).packed[-1])
        suffix = str(last_octet)
        expected_candidates = [
            ipaddress.IPv6Address(f"fd88::{suffix}{suffix}"),
            ipaddress.IPv6Address(f"fd88::{suffix * 4}") if len(suffix) == 1 else None,
        ]
    except Exception:
        expected_candidates = []
    for expected in expected_candidates:
        if expected and expected in addresses:
            return str(expected)

    # 与 app.py 的同名函数保持一致：只在私有(ULA)地址上监听 53 端口 DNS。
    # GUA 可被公网直接路由，绑上去等于开放解析器；没有 ULA 时返回空串，
    # 上层会移除 dns-in-v6 入站，不提供 IPv6 DNS 也不裸奔。
    private_addresses = [address for address in addresses if address.is_private]
    if not private_addresses:
        return ""

    def score(address):
        text = str(address)
        if "ff:fe" not in text:
            return 0
        return 1

    return str(sorted(private_addresses, key=score)[0])


def remove_inbound_tag(config, tag):
    for section in ("dns", "route"):
        for rule in config.get(section, {}).get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            inbound = rule.get("inbound")
            if isinstance(inbound, list) and tag in inbound:
                inbound[:] = [item for item in inbound if item != tag]
                if len(inbound) == 1:
                    rule["inbound"] = inbound[0]
            elif inbound == tag:
                rule.pop("inbound", None)


def add_inbound_tag(config, tag):
    for section in ("dns", "route"):
        for rule in config.get(section, {}).get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            inbound = rule.get("inbound")
            if inbound is None:
                continue
            if isinstance(inbound, list):
                if tag not in inbound:
                    inbound.append(tag)
            elif inbound == "dns-in":
                rule["inbound"] = ["dns-in", tag]


def render_managed_config():
    sys.path.insert(0, str(APP_DIR))
    from app import RULE_DIR, load_groups, load_nodes, render_config

    return render_config(nodes=load_nodes(), groups=load_groups(), rule_dir=RULE_DIR)


def apply_runtime_listeners(config, lan_ip, ipv6_listen):
    changed = False
    inbounds = config.get("inbounds", []) or []
    kept_inbounds = []
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            kept_inbounds.append(inbound)
            continue
        if inbound.get("tag") == "dns-in" and inbound.get("listen") != lan_ip:
            inbound["listen"] = lan_ip
            changed = True
        if inbound.get("tag") == "dns-in-v6":
            if ipv6_listen:
                if inbound.get("listen") != ipv6_listen:
                    inbound["listen"] = ipv6_listen
                    changed = True
            else:
                remove_inbound_tag(config, "dns-in-v6")
                changed = True
                continue
        kept_inbounds.append(inbound)
    if kept_inbounds != inbounds:
        config["inbounds"] = kept_inbounds
    if ipv6_listen and not any(isinstance(item, dict) and item.get("tag") == "dns-in-v6" for item in kept_inbounds):
        config.setdefault("inbounds", []).append({"type": "direct", "tag": "dns-in-v6", "listen": ipv6_listen, "listen_port": 53})
        add_inbound_tag(config, "dns-in-v6")
        changed = True

    clash = config.setdefault("experimental", {}).setdefault("clash_api", {})
    controller = f"{lan_ip}:9090"
    if clash.get("external_controller") != controller:
        clash["external_controller"] = controller
        changed = True
    return changed


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    # 同目录写入并同步内容，进程中断时不会留下截断的正式文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def check_rendered_config(path, text):
    # 运行态刷新同样遵守“先 check 再落盘”：正式 config.json 只有在 sing-box 能解析时才会被替换。
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.check-", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        result = subprocess.run([str(SING_BOX_BIN), "check", "-c", str(temp_path)], text=True, capture_output=True, timeout=30)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "sing-box check failed").strip()
            raise RuntimeError(message)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def config_is_valid(path):
    if not path.is_file():
        return False
    result = subprocess.run([str(SING_BOX_BIN), "check", "-c", str(path)], text=True, capture_output=True, timeout=30)
    return result.returncode == 0


def recover_last_good_config(error):
    if config_is_valid(CONFIG_PATH):
        print(
            json.dumps(
                {"level": "warning", "event": "runtime_config_refresh_rejected", "fallback": "current_config", "reason": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return True

    backup_dir = CONFIG_PATH.parent / "manager" / "backups"
    backups = sorted(backup_dir.glob("config.json.bak-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    for backup in backups:
        if not config_is_valid(backup):
            continue
        atomic_write_text(CONFIG_PATH, backup.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "level": "warning",
                    "event": "runtime_config_restored",
                    "fallback": str(backup),
                    "reason": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return True

    print(
        json.dumps(
            {"level": "error", "event": "runtime_config_refresh_failed", "fallback": "unavailable", "reason": str(error)},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return False


def main():
    try:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(f"sing-box config does not exist: {CONFIG_PATH}")
        lan_ip = default_lan_ip()
        ipv6_listen = preferred_ipv6_listener(lan_ip)
        previous = CONFIG_PATH.read_text(encoding="utf-8")
        config = render_managed_config()
        listener_changed = apply_runtime_listeners(config, lan_ip, ipv6_listen)
        rendered = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        if rendered != previous:
            check_rendered_config(CONFIG_PATH, rendered)
            atomic_write_text(CONFIG_PATH, rendered)
            print(f"Rendered sing-box config and updated listeners for {lan_ip}.")
        elif listener_changed:
            print(f"Updated sing-box listeners for {lan_ip}.")
        else:
            print(f"sing-box config already rendered and listeners match {lan_ip}.")
        return 0
    except Exception as exc:
        if recover_last_good_config(exc):
            return 2
        raise RuntimeError(f"Runtime config refresh failed without a valid fallback: {exc}") from exc



def log_recovery(level: str, event: str, **details: str | int | float) -> None:
    print(json.dumps({"level": level, "event": event, **details}, ensure_ascii=False), file=sys.stderr)


def private_recovery_dir() -> Path:
    root = CONFIG_PATH.parent / "recovery"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
        raise PermissionError(f"Recovery directory must be owned by the service user and private: {root}")
    return root


def managed_rule_path(raw_path: str) -> Path:
    base = CONFIG_PATH.parent
    absolute = Path(os.path.abspath(base / raw_path))
    relative = absolute.relative_to(base)
    if not relative.parts or relative.parts[0] == "recovery" or absolute == CONFIG_PATH:
        raise ValueError(f"Invalid managed rule-set path: {absolute}")
    if not absolute.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Rule-set path resolves outside the config directory: {absolute}")
    return relative


def snapshot_manifest(content: bytes, tree: Path) -> tuple[bytes, set[Path]]:
    # 从同一份原始配置重写校验路径，并展开 1.14 的规则标签占位符。
    config = json.loads(content)
    files = {Path(CONFIG_PATH.name)}
    for rule_set in config.get("route", {}).get("rule_set", []):
        if rule_set.get("type") != "local":
            continue
        raw_path = rule_set["path"]
        tags = rule_set["tag"]
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(raw_path, str) or not tags or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("Invalid local rule-set path or tag")
        for tag in tags:
            if Path(tag).name != tag or tag in ("", ".", ".."):
                raise ValueError("Invalid local rule-set tag")
            files.add(managed_rule_path(raw_path.replace("{tag}", tag)))
        rule_set["path"] = str(tree / managed_rule_path(raw_path))
    return json.dumps(config, ensure_ascii=False).encode("utf-8"), files


def startup_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SING_BOX_BIN), "check", "-c", str(path)],
        cwd=CONFIG_PATH.parent,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )


def check_snapshot(generation: Path) -> set[Path]:
    tree = generation / "files"
    content = (tree / CONFIG_PATH.name).read_bytes()
    checked_config, files = snapshot_manifest(content, tree)
    expected = json.loads((generation / "sha256.json").read_text(encoding="utf-8"))
    actual = {str(path): hashlib.sha256((tree / path).read_bytes()).hexdigest() for path in sorted(files)}
    if actual != expected:
        raise ValueError(f"Known-good snapshot checksum mismatch: {generation}")
    with tempfile.TemporaryDirectory(prefix=".check-", dir=generation.parent) as directory:
        candidate = Path(directory) / "config.json"
        atomic_write_bytes(candidate, checked_config)
        result = startup_check(candidate)
    if result.returncode:
        raise ValueError(f"Known-good snapshot rejected by sing-box check: {generation}")
    return files


def selected_generation(root: Path) -> Path:
    pointer = root / "known-good"
    name = pointer.read_text(encoding="utf-8").strip()
    if Path(name).name != name or not name.startswith("known-good-"):
        raise ValueError(f"Invalid known-good snapshot pointer: {pointer}")
    generation = root / name
    if generation.is_symlink() or not generation.is_dir():
        raise FileNotFoundError(f"Known-good snapshot directory missing: {generation}")
    return generation


def capture_known_good(root: Path) -> None:
    # 仅支持由管理员在数据面验收后显式提升快照，不从启动检查自动晋升。
    content = CONFIG_PATH.read_bytes()
    generation = Path(tempfile.mkdtemp(prefix="known-good-", dir=root))
    tree = generation / "files"
    try:
        _, files = snapshot_manifest(content, tree)
        files_data = {path: (CONFIG_PATH.parent / path).read_bytes() for path in files}
        if files_data[Path(CONFIG_PATH.name)] != content:
            raise RuntimeError("Live config changed during capture, retry after runtime verification")
        for path, data in files_data.items():
            atomic_write_bytes(tree / path, data)
        checksums = {str(path): hashlib.sha256(data).hexdigest() for path, data in sorted(files_data.items())}
        atomic_write_text(generation / "sha256.json", json.dumps(checksums, indent=2) + "\n")
        check_snapshot(generation)
        if any((CONFIG_PATH.parent / path).read_bytes() != data for path, data in files_data.items()):
            raise RuntimeError("Config or local rules changed during capture, retry after runtime verification")
        atomic_write_text(root / "known-good", generation.name + "\n")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired):
        shutil.rmtree(generation)
        raise
    log_recovery("info", "known_good_snapshot_saved", snapshot=str(generation), files=len(files))


def recover_startup(root: Path, check_code: int, diagnostic: str) -> None:
    generation = selected_generation(root)
    files = check_snapshot(generation)
    tree = generation / "files"
    incident = Path(tempfile.mkdtemp(prefix="incident-", dir=root))
    changes: dict[Path, bytes] = {}
    for relative in sorted(files):
        live_path = CONFIG_PATH.parent / relative
        if not live_path.resolve().is_relative_to(CONFIG_PATH.parent.resolve()):
            raise ValueError(f"Refusing to replace path outside config directory: {live_path}")
        candidate = (tree / relative).read_bytes()
        if live_path.is_file():
            bad_data = live_path.read_bytes()
            if bad_data == candidate:
                continue
            atomic_write_bytes(incident / "files" / relative, bad_data)
        changes[relative] = candidate
    diagnosis = {
        "snapshot": generation.name,
        "checkReturnCode": check_code,
        "diagnostic": diagnostic,
        "replacedFiles": [str(path) for path in changes],
    }
    atomic_write_text(incident / "diagnosis.json", json.dumps(diagnosis, ensure_ascii=False, indent=2) + "\n")
    # 先恢复规则，最后发布引用它们的配置。完整快照始终保持独立可用。
    for path in sorted(changes, key=lambda item: item.name == CONFIG_PATH.name and len(item.parts) == 1):
        atomic_write_bytes(CONFIG_PATH.parent / path, changes[path])
    if startup_check(CONFIG_PATH).returncode:
        raise RuntimeError(f"Restored live configuration failed verification. Incident: {incident}")
    atomic_write_text(root / "last-recovery", incident.name + "\n")
    log_recovery("warning", "known_good_snapshot_restored", snapshot=str(generation), incident=str(incident), files=len(changes))


def read_digest(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return ""


def config_digest() -> str:
    return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()


def runtime_dir() -> Path:
    paths = os.environ.get("RUNTIME_DIRECTORY", "/run/sing-box-startup").split(os.pathsep)
    directories = [Path(path) for path in paths]
    try:
        directory = directories[0] if len(directories) == 1 else next(
            path for path in directories if path.name == "sing-box-startup"
        )
    except StopIteration as exc:
        raise FileNotFoundError("systemd startup runtime directory not listed in RUNTIME_DIRECTORY") from exc
    if not directory.is_dir() or directory.is_symlink():
        raise FileNotFoundError(f"systemd startup runtime directory unavailable: {directory}")
    return directory


def startup() -> int:
    runtime = runtime_dir()
    pending = runtime / "pending"
    skip_render = runtime / "skip-render"
    pending.unlink(missing_ok=True)

    result = startup_check(CONFIG_PATH)
    if result.returncode:
        log_recovery("error", "startup_config_rejected", config=str(CONFIG_PATH), code=result.returncode)
        root = private_recovery_dir()
        with (root / "lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            recover_startup(root, result.returncode, (result.stderr or result.stdout).strip())
            atomic_write_text(skip_render, config_digest() + "\n")
    elif read_digest(skip_render) == config_digest():
        log_recovery("info", "startup_reuses_restored_config", config=str(CONFIG_PATH))
    else:
        # 配置内容已变动时，允许管理器正常渲染和启动。
        skip_render.unlink(missing_ok=True)
        main()

    digest = config_digest()
    result = startup_check(CONFIG_PATH)
    if result.returncode or config_digest() != digest:
        log_recovery("error", "startup_config_rejected_after_refresh", config=str(CONFIG_PATH), code=result.returncode)
        return 1
    atomic_write_text(pending, digest + "\n")
    return 0


def clash_readiness_endpoint() -> tuple[str, int, str]:
    clash = json.loads(CONFIG_PATH.read_bytes()).get("experimental", {}).get("clash_api", {})
    address = clash.get("external_controller", "")
    controller = urlsplit("//" + address)
    host, port = controller.hostname, controller.port
    if not host or not port or controller.username or controller.path or controller.query or controller.fragment:
        raise ValueError("Clash API external_controller must be a local listen address with a port")
    if host == "localhost" or host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    ipaddress.ip_address(host)
    return host, port, clash.get("secret", "")


def clash_ready(host: str, port: int, secret: str, timeout: float) -> bool:
    # HTTPConnection 只访问配置中的本地监听地址，不经过代理或跟随跳转。
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        connection.request("GET", "/version", headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read(4096))
        return isinstance(payload, dict) and isinstance(payload.get("version"), str) and bool(payload["version"])
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        connection.close()


def confirm_startup(timeout: float) -> int:
    pending = runtime_dir() / "pending"
    digest = read_digest(pending)
    pid = int(os.environ.get("MAINPID", "0"))
    if not digest or pid <= 1:
        raise RuntimeError("Startup readiness requires the prepared configuration and systemd MainPID")
    host, port, secret = clash_readiness_endpoint()
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        os.kill(pid, 0)
        if clash_ready(host, port, secret, min(remaining, 0.5)):
            if config_digest() != digest:
                raise RuntimeError("Configuration changed before the Clash API became ready")
            os.kill(pid, 0)
            pending.unlink()
            log_recovery("info", "startup_clash_ready", pid=pid)
            return 0
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    log_recovery("error", "startup_clash_readiness_timeout", timeoutSeconds=timeout, port=port)
    return 1


def recover_unready_startup() -> int:
    try:
        runtime = runtime_dir()
    except FileNotFoundError:
        # systemd 在创建 RuntimeDirectory 前失败时，没有可恢复的启动尝试。
        return 0
    pending = runtime / "pending"
    digest = read_digest(pending)
    pending.unlink(missing_ok=True)
    result = os.environ.get("SERVICE_RESULT", "")
    if not digest or result not in {"exit-code", "signal", "core-dump", "timeout", "oom-kill", "protocol", "resources"}:
        return 0

    root = private_recovery_dir()
    with (root / "lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        skip_render = runtime / "skip-render"
        attempted = runtime / "recovery-attempt"
        if digest != config_digest():
            log_recovery("warning", "startup_recovery_skipped_config_changed", serviceResult=result)
            return 0
        if digest in {read_digest(skip_render), read_digest(attempted)}:
            log_recovery("warning", "startup_recovery_already_attempted", serviceResult=result)
            return 0
        atomic_write_text(attempted, digest + "\n")
        diagnostic = json.dumps({
            "serviceResult": result,
            "exitCode": os.environ.get("EXIT_CODE", ""),
            "exitStatus": os.environ.get("EXIT_STATUS", ""),
        })
        recover_startup(root, 0, diagnostic)
        atomic_write_text(skip_render, config_digest() + "\n")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description="刷新 sing-box 配置，维护经过验证的冷启动快照")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--startup", action="store_true", help="启动前检查并在需要时恢复快照")
    mode.add_argument("--startup-ready", action="store_true", help="等待 Clash API 就绪")
    mode.add_argument("--startup-stopped", action="store_true", help="处理未就绪的启动失败")
    mode.add_argument("--capture-known-good", action="store_true", help="管理员完成运行验收后保存当前配置和规则")
    mode.add_argument("--verify-known-good", action="store_true", help="校验快照的摘要和 sing-box 配置")
    args = parser.parse_args()
    if not (args.startup or args.startup_ready or args.startup_stopped or args.capture_known_good or args.verify_known_good):
        return main()
    try:
        if args.startup:
            return startup()
        if args.startup_ready:
            return confirm_startup(8.0)
        if args.startup_stopped:
            return recover_unready_startup()
        root = private_recovery_dir()
        with (root / "lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.capture_known_good:
                capture_known_good(root)
            else:
                generation = selected_generation(root)
                files = check_snapshot(generation)
                log_recovery("info", "known_good_snapshot_verified", snapshot=str(generation), files=len(files))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        log_recovery("error", "known_good_operation_failed", reason=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
