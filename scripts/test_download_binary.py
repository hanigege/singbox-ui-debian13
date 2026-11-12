#!/usr/bin/env python3
"""验证按架构下载、源码提交绑定及失败时不留下损坏归档。"""

import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bundled_assets import Architecture, ROOT
from download_binary import ensure_binary, source_commit
from test_bundled_assets import elf_header, make_asset


class DownloadBinaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="selective-binary-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.project = self.root / "project"
        self.project.mkdir()
        self.commit = "a" * 40
        self.assets = {}
        for arch, machine in (("amd64", 62), ("arm64", 183)):
            _, spec = make_asset(self.remote, arch, {"sing-box": elf_header(machine), "libcronet.so": elf_header(machine), "LICENSE": b"fixture license"})
            self.assets[arch] = spec
        (self.project / "versions.json").write_text(json.dumps({"version": "1.14.4", "assets": self.assets}))
        self.archive = self.root / "source.tar.gz"
        with tarfile.open(self.archive, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": self.commit}) as archive:
            directory = tarfile.TarInfo("project/")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)

    def transport(self, arch: Architecture):
        def download(args):
            expected = f"https://raw.githubusercontent.com/hanigege/singbox-ui-debian13/{self.commit}/{self.assets[arch]['path']}"
            self.assertEqual(args[-1], "https://mirror.example/" + expected)
            output = Path(args[args.index("--output") + 1])
            shutil.copyfile(self.remote / self.assets[arch]["path"], output)
            return subprocess.CompletedProcess(args, 0)
        return download

    def test_downloads_only_selected_architecture_at_source_commit(self):
        self.assertEqual(source_commit(self.archive), self.commit)
        for arch in ("amd64", "arm64"):
            project = self.project / arch
            project.mkdir()
            shutil.copyfile(self.project / "versions.json", project / "versions.json")
            with patch("download_binary.subprocess.run", side_effect=self.transport(arch)) as request:
                result = ensure_binary(project, arch, self.archive, "https://mirror.example/")
            request.assert_called_once()
            self.assertEqual(result.relative_to(project).as_posix(), self.assets[arch]["path"])
            self.assertEqual(list(project.glob("third_party/**/*.tar.gz")), [result])

    def test_full_git_checkout_uses_existing_asset_without_source_metadata(self):
        path = self.project / self.assets["amd64"]["path"]
        path.parent.mkdir(parents=True)
        shutil.copyfile(self.remote / self.assets["amd64"]["path"], path)
        with patch("download_binary.subprocess.run") as request:
            result = ensure_binary(self.project, "amd64", self.root / "absent.tar.gz", "")
        self.assertEqual(result, path)
        request.assert_not_called()

    def test_existing_corruption_is_rejected_without_network_repair(self):
        path = self.project / self.assets["amd64"]["path"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"corrupt")
        with patch("download_binary.subprocess.run") as request:
            with self.assertRaisesRegex(ValueError, "SHA256 不匹配"):
                ensure_binary(self.project, "amd64", self.archive, "")
        request.assert_not_called()

    def test_failed_transport_leaves_no_partial_asset(self):
        with patch("download_binary.subprocess.run", return_value=subprocess.CompletedProcess([], 28)):
            with self.assertRaisesRegex(RuntimeError, "exit=28"):
                ensure_binary(self.project, "amd64", self.archive, "")
        self.assertFalse((self.project / self.assets["amd64"]["path"]).exists())
        self.assertEqual(list(self.project.glob(".binary-download-*")), [])

    def test_wrong_download_is_not_published(self):
        def wrong_asset(args):
            output = Path(args[args.index("--output") + 1])
            shutil.copyfile(self.remote / self.assets["arm64"]["path"], output)
            return subprocess.CompletedProcess(args, 0)
        with patch("download_binary.subprocess.run", side_effect=wrong_asset):
            with self.assertRaisesRegex(ValueError, "SHA256 不匹配"):
                ensure_binary(self.project, "amd64", self.archive, "")
        self.assertFalse((self.project / self.assets["amd64"]["path"]).exists())
        self.assertEqual(list(self.project.glob(".binary-download-*")), [])

    def test_missing_commit_metadata_refuses_mutable_ref_download(self):
        with tarfile.open(self.archive, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.addfile(tarfile.TarInfo("file"))
        with patch("download_binary.subprocess.run") as request:
            with self.assertRaisesRegex(ValueError, "提交 ID"):
                ensure_binary(self.project, "amd64", self.archive, "")
        request.assert_not_called()


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "需要 Git 工作区验证源码归档属性")
class SourceArchiveTests(unittest.TestCase):
    def test_source_archive_excludes_both_large_assets_and_keeps_commit(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "source.tar.gz"
            subprocess.run(["git", "archive", "--format=tar.gz", "--worktree-attributes", "--output", str(path), "HEAD"], cwd=ROOT, check=True)
            self.assertLess(path.stat().st_size, 1024 * 1024)
            expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            self.assertEqual(source_commit(path), expected)
            with tarfile.open(path) as archive:
                self.assertFalse(any(member.name.startswith("third_party/sing-box/") for member in archive))


if __name__ == "__main__":
    unittest.main()
