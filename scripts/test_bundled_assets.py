#!/usr/bin/env python3
"""用真实临时归档验证完整性、架构和受控提取的失败路径。"""

import io
import json
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path

from bundled_assets import Architecture, AssetSpec, extract_asset, load_manifest, sha256_file, verify_asset


def elf_header(machine: int) -> bytes:
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, machine)
    return bytes(header)


def make_asset(root: Path, arch: Architecture, contents: dict[str, bytes]) -> tuple[Path, AssetSpec]:
    prefix = f"sing-box-1.14.4-linux-{arch}"
    relative = f"third_party/sing-box/v1.14.4/{prefix}.tar.gz"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for filename, data in contents.items():
            member = tarfile.TarInfo(f"{prefix}/{filename}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    spec = AssetSpec(path=relative, sha256=sha256_file(path), url=f"https://github.com/SagerNet/sing-box/releases/download/v1.14.4/{prefix}.tar.gz")
    return path, spec


class BundledAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bundled-assets-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_both_architectures_extract_program_library_and_license(self):
        for arch, machine in (("amd64", 62), ("arm64", 183)):
            with self.subTest(arch=arch):
                contents = {"sing-box": elf_header(machine), "libcronet.so": elf_header(machine), "LICENSE": b"fixture license\n"}
                path, spec = make_asset(self.root, arch, contents)
                self.assertEqual(verify_asset(self.root, "1.14.4", arch, spec), path)
                destination = self.root / arch
                extract_asset(path, "1.14.4", arch, destination)
                self.assertEqual({item.name for item in destination.iterdir()}, set(contents))
                for name, data in contents.items():
                    self.assertEqual((destination / name).read_bytes(), data)
                self.assertEqual((destination / "sing-box").stat().st_mode & 0o777, 0o755)

    def test_modified_archive_is_rejected_before_extraction(self):
        path, spec = make_asset(self.root, "amd64", {"sing-box": elf_header(62)})
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA256 不匹配"):
            verify_asset(self.root, "1.14.4", "amd64", spec)

    def test_wrong_library_architecture_is_rejected(self):
        _, spec = make_asset(self.root, "amd64", {"sing-box": elf_header(62), "libcronet.so": elf_header(183), "LICENSE": b"license"})
        with self.assertRaisesRegex(ValueError, "架构错误"):
            verify_asset(self.root, "1.14.4", "amd64", spec)

    def test_missing_runtime_library_is_rejected(self):
        _, spec = make_asset(self.root, "amd64", {"sing-box": elf_header(62), "LICENSE": b"license"})
        with self.assertRaises(KeyError):
            verify_asset(self.root, "1.14.4", "amd64", spec)

    def test_extra_paths_in_archive_are_never_extracted(self):
        contents = {"sing-box": elf_header(62), "libcronet.so": elf_header(62), "LICENSE": b"license", "../../outside": b"unsafe"}
        path, spec = make_asset(self.root, "amd64", contents)
        verify_asset(self.root, "1.14.4", "amd64", spec)
        destination = self.root / "extracted"
        extract_asset(path, "1.14.4", "amd64", destination)
        self.assertEqual({item.name for item in destination.iterdir()}, {"sing-box", "libcronet.so", "LICENSE"})
        self.assertFalse((self.root / "outside").exists())

    def test_existing_destination_is_preserved(self):
        path, _ = make_asset(self.root, "amd64", {"sing-box": elf_header(62)})
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "keep").write_text("unchanged")
        with self.assertRaises(FileExistsError):
            extract_asset(path, "1.14.4", "amd64", destination)
        self.assertEqual((destination / "keep").read_text(), "unchanged")

    def test_manifest_requires_both_architectures_and_fixed_paths(self):
        manifest = {"version": "1.14.4", "assets": {}}
        path = self.root / "versions.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "同时包含"):
            load_manifest(path)
        for arch in ("amd64", "arm64"):
            _, spec = make_asset(self.root, arch, {"LICENSE": b"license"})
            manifest["assets"][arch] = spec
        path.write_text(json.dumps(manifest))
        self.assertEqual(set(load_manifest(path)["assets"]), {"amd64", "arm64"})
        manifest["assets"]["arm64"]["path"] = "../../outside.tar.gz"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "路径或官方来源"):
            load_manifest(path)


if __name__ == "__main__":
    unittest.main()
