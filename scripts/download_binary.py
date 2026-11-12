#!/usr/bin/env python3
"""按系统架构从本仓库的固定提交获取官方归档，并在落盘前校验。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import cast

from bundled_assets import ARCHITECTURES, ROOT, Architecture, load_manifest, verify_asset


def source_commit(source_archive: Path) -> str:
    # git archive 的全局 PAX 头携带提交 ID，不再查询可变的 main，也不依赖 API 配额。
    if not source_archive.is_file():
        raise FileNotFoundError(f"当前架构归档缺失，且没有源码归档提交信息: {source_archive}；请使用一键安装入口或完整 Git 克隆")
    with tarfile.open(source_archive, "r:gz") as archive:
        commit = archive.pax_headers.get("comment", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"源码归档缺少有效提交 ID: {source_archive}；请使用一键安装入口或完整 Git 克隆")
    return commit


def ensure_binary(root: Path, arch: Architecture, source_archive: Path, prefix: str) -> Path:
    manifest = load_manifest(root / "versions.json")
    spec = manifest["assets"][arch]
    destination = root / spec["path"]
    if destination.exists():
        # 完整 Git 克隆已有归档；损坏时直接拒绝，不能用重新下载掩盖校验失败。
        return verify_asset(root, manifest["version"], arch, spec)
    commit = source_commit(source_archive)
    url = f"https://raw.githubusercontent.com/hanigege/singbox-ui-debian13/{commit}/{spec['path']}"
    print(json.dumps({"event": "download-binary", "arch": arch, "commit": commit, "path": spec["path"]}, ensure_ascii=False), flush=True)
    with tempfile.TemporaryDirectory(prefix=".binary-download-", dir=root) as name:
        staging = Path(name)
        temporary_asset = staging / spec["path"]
        temporary_asset.parent.mkdir(parents=True)
        result = subprocess.run([
            "curl", "--fail", "--location", "--retry", "3", "--retry-all-errors",
            "--connect-timeout", "15", "--max-time", "600", "--output", str(temporary_asset), prefix + url,
        ])
        if result.returncode != 0:
            raise RuntimeError(f"本仓库二进制下载失败: arch={arch}, commit={commit}, exit={result.returncode}")
        verify_asset(staging, manifest["version"], arch, spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_asset.replace(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="只获取当前架构的本仓库官方二进制归档")
    parser.add_argument("--arch", choices=ARCHITECTURES, required=True)
    parser.add_argument("--source-archive", type=Path, default=ROOT / "source.tar.gz")
    parser.add_argument("--prefix", default=os.environ.get("GITHUB_DOWNLOAD_PREFIX", ""))
    args = parser.parse_args()
    path = ensure_binary(ROOT, cast(Architecture, args.arch), args.source_archive, args.prefix)
    print(json.dumps({"event": "binary-ready", "arch": args.arch, "path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
