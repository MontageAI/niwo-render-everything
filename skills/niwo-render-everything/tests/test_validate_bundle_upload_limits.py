"""验证横屏信息动效素材包的本地上传容量门槛。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import validate_bundle as validator


class EditorialUploadLimitTests(unittest.TestCase):
    """核对新版面容量边界与普通版面隔离。"""

    def test_media_count_boundary(self) -> None:
        """媒体文件第 101 个才触发新版面数量错误。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            media_dir = bundle / "videos"
            media_dir.mkdir()
            for index in range(100):
                (media_dir / f"{index:03}.mp4").write_bytes(b"x")
            report = validator.Report()
            validator.check_editorial_upload_limits(bundle, report)
            self.assertFalse(report.errors)

            (media_dir / "100.mp4").write_bytes(b"x")
            report = validator.Report()
            validator.check_editorial_upload_limits(bundle, report)
            self.assertTrue(any("101 个" in error and "100 个" in error for error in report.errors))

    def test_source_size_boundary_includes_protocol_files(self) -> None:
        """把两个 JSON 算入容量，并给 ZIP 开销预留空间。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            media_dir = bundle / "videos"
            media_dir.mkdir()
            (bundle / "content.json").write_bytes(b"{}")
            (bundle / "manifest.json").write_bytes(b"{}")
            mib = 1024 * 1024
            for index in range(8):
                with (media_dir / f"{index:03}.mp4").open("wb") as handle:
                    handle.truncate(24 * mib)
            last = media_dir / "008.mp4"
            with last.open("wb") as handle:
                handle.truncate(3 * mib - 4)

            report = validator.Report()
            validator.check_editorial_upload_limits(bundle, report)
            self.assertFalse(report.errors)

            with last.open("r+b") as handle:
                handle.truncate(3 * mib - 3)
            report = validator.Report()
            validator.check_editorial_upload_limits(bundle, report)
            self.assertTrue(any("195 MiB" in error for error in report.errors))

    def test_source_size_includes_other_files_in_zip(self) -> None:
        """本地配乐也会打进 ZIP，不能只合计 images/videos。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            media_dir = bundle / "videos"
            music_dir = bundle / "bgm"
            media_dir.mkdir()
            music_dir.mkdir()
            with (media_dir / "clip.mp4").open("wb") as handle:
                handle.truncate(194 * 1024 * 1024)
            with (music_dir / "track.mp3").open("wb") as handle:
                handle.truncate(2 * 1024 * 1024)

            report = validator.Report()
            validator.check_editorial_upload_limits(bundle, report)
            self.assertTrue(any("195 MiB" in error for error in report.errors))

    def test_editorial_orphans_include_nested_media(self) -> None:
        """子目录里漏写的素材也应在外部 Agent 校验阶段报错。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            nested = bundle / "videos" / "展台"
            nested.mkdir(parents=True)
            (nested / "clip.mp4").write_bytes(b"x")

            report = validator.Report()
            validator.check_orphans(bundle, [], report, editorial_mode=True)
            self.assertTrue(any("videos/展台/clip.mp4" in error for error in report.errors))

    def test_main_applies_upload_limit_to_all_layouts(self) -> None:
        """普通包与信息动效包均遵守 Niwo 的 100 媒体限制。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            media_dir = bundle / "images"
            media_dir.mkdir()
            entries = []
            for index in range(101):
                name = f"images/{index:03}.png"
                (bundle / name).write_bytes(b"\x89PNG\r\n\x1a\n")
                entries.append({"file": name, "kind": "image", "summary": "素材", "tags": ["图"]})
            (bundle / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "assets": entries}), encoding="utf-8"
            )
            content = {"schema_version": 1, "title": "测试", "script": "测试", "sources": []}
            content_path = bundle / "content.json"
            content_path.write_text(json.dumps(content), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                legacy_result = validator.main(["validate_bundle.py", str(bundle)])
            self.assertEqual(legacy_result, 1)

            content["editorial"] = {"prepared_for": validator.EDITORIAL_LAYOUT,
                "title_text": "测试", "title_anchor": "测试",
                "segments": [{"id": "s1", "script_anchor": "测试", "role": "hook", "visual_goal": "展示测试"}]}
            content_path.write_text(json.dumps(content), encoding="utf-8")
            output = io.StringIO()
            with patch.object(validator, "check_editorial_bundle"), contextlib.redirect_stdout(output):
                editorial_result = validator.main(["validate_bundle.py", str(bundle)])
            self.assertEqual(editorial_result, 1)
            self.assertIn("101 个", output.getvalue())


if __name__ == "__main__":
    unittest.main()
