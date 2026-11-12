#!/usr/bin/env python3
"""校验仓库内的官方发布归档，并为安装器准备当前架构的运行文件。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import tarfile
from pathlib import Path
from typing import Literal, TypedDict, cast


Architecture = Literal["amd64", "arm64"]
ARCHITECTURES: tuple[Architecture, ...] = ("amd64", "arm64")
ELF_MACHINES = {"amd64": 62, "arm64": 183}
ROOT = Path(__file__).resolve().parents[1]


class AssetSpec(TypedDict):
    url: str
    sha256: str
    path: str


class Manifest(TypedDict):
    version: str
    assets: dict[Architecture, AssetSpec]


def load_manifest(path: Path) -> Manifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("version"), str):
        raise ValueError(f"版本清单缺少 version: {path}")
    version = raw["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"版本号格式无效: {version}")
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, dict) or set(raw_assets) != set(ARCHITECTURES):
        raise ValueError("版本清单必须同时包含 amd64 和 arm64")
    assets: dict[Architecture, AssetSpec] = {}
    for arch in ARCHITECTURES:
        item = raw_assets[arch]
        filename = f"sing-box-{version}-linux-{arch}.tar.gz"
        expected_path = f"third_party/sing-box/v{version}/{filename}"
        expected_url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{filename}"
        if not isinstance(item, dict) or item.get("path") != expected_path or item.get("url") != expected_url:
            raise ValueError(f"{arch} 的归档路径或官方来源 URL 与版本号不一致")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{arch} 的 SHA256 格式无效")
        assets[arch] = AssetSpec(path=expected_path, url=expected_url, sha256=digest)
    return Manifest(version=version, assets=assets)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asset(root: Path, version: str, arch: Architecture, spec: AssetSpec) -> Path:
    path = root / spec["path"]
    actual = sha256_file(path)
    if actual != spec["sha256"]:
        raise ValueError(f"官方归档 SHA256 不匹配: path={path}, expected={spec['sha256']}, actual={actual}")
    prefix = f"sing-box-{version}-linux-{arch}"
    with tarfile.open(path, "r:gz") as archive:
        for filename in ("sing-box", "libcronet.so", "LICENSE"):
            member = archive.getmember(f"{prefix}/{filename}")
            if not member.isfile() or member.size == 0:
                raise ValueError(f"官方归档缺少有效运行文件: {path}: {filename}")
            if filename == "LICENSE":
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"无法读取运行文件: {path}: {filename}")
            with handle:
                header = handle.read(20)
            if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01":
                raise ValueError(f"运行文件不是 ELF64 little-endian: {path}: {filename}")
            machine = struct.unpack("<H", header[18:20])[0]
            if machine != ELF_MACHINES[arch]:
                raise ValueError(f"运行文件架构错误: {path}: {filename}, machine={machine}, expected={arch}")
    return path


def extract_asset(path: Path, version: str, arch: Architecture, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    prefix = f"sing-box-{version}-linux-{arch}"
    with tarfile.open(path, "r:gz") as archive:
        # 只提取经过校验的三个固定文件，不执行归档里的路径、链接或其它成员。
        for filename in ("sing-box", "libcronet.so", "LICENSE"):
            member = archive.getmember(f"{prefix}/{filename}")
            target = destination / filename
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"无法提取运行文件: {path}: {filename}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if filename == "sing-box" else 0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description="校验内置官方归档；安装时按架构提取运行文件")
    parser.add_argument("--arch", choices=ARCHITECTURES, help="仅检查指定架构")
    parser.add_argument("--destination", type=Path, help="将指定架构提取到尚不存在的目录")
    args = parser.parse_args()
    if args.destination is not None and args.arch is None:
        parser.error("--destination 必须与 --arch 一起使用")
    manifest = load_manifest(ROOT / "versions.json")
    architectures = (cast(Architecture, args.arch),) if args.arch else ARCHITECTURES
    for arch in architectures:
        spec = manifest["assets"][arch]
        path = verify_asset(ROOT, manifest["version"], arch, spec)
        if args.destination is not None:
            extract_asset(path, manifest["version"], arch, args.destination)
        print(json.dumps({"source": "repository", "version": manifest["version"], "arch": arch, "path": spec["path"], "bytes": path.stat().st_size, "sha256": spec["sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
