"""横屏纯视频素材包的独立备料验收。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import validate_bundle as validator


class FootageBundleTests(unittest.TestCase):
    """纯视频检查仅约束选择该版面的素材包。"""

    def test_accepts_script_matched_video_with_full_capacity(self) -> None:
        """选段与口播匹配且长度足够时通过。"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            entry = {"file": "videos/clip.mp4", "kind": "video", "script_anchor": "机器人走进工厂"}
            report = validator.Report()
            with patch.object(validator, "probe_duration", return_value=20.0):
                validator.check_footage_bundle({"script": "机器人走进工厂"}, [(path, entry)], report, 10.0, None)
            self.assertEqual(report.errors, [])

    def test_rejects_portrait_video_for_each_landscape_layout(self) -> None:
        """三种横屏版面都拒绝竖屏视频。"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            resolved = [(path, {"file": "videos/clip.mp4", "kind": "video"})]
            for layout in (
                "landscape-full-v1",
                validator.FOOTAGE_LAYOUT,
                validator.EDITORIAL_LAYOUT,
            ):
                with self.subTest(layout=layout):
                    report = validator.Report()
                    with patch.object(validator, "probe_dimensions", return_value=(360, 640)):
                        validator.check_landscape_video_orientation(resolved, layout, report)
                    self.assertTrue(any("videos/clip.mp4" in item for item in report.errors))

    def test_accepts_landscape_video_for_each_landscape_layout(self) -> None:
        """三种横屏版面都接受横屏视频。"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            resolved = [(path, {"file": "videos/clip.mp4", "kind": "video"})]
            for layout in (
                validator.LANDSCAPE_LAYOUT,
                validator.FOOTAGE_LAYOUT,
                validator.EDITORIAL_LAYOUT,
            ):
                with self.subTest(layout=layout):
                    report = validator.Report()
                    with patch.object(validator, "probe_dimensions", return_value=(640, 360)):
                        validator.check_landscape_video_orientation(resolved, layout, report)
                    self.assertEqual(report.errors, [])

    def test_declared_default_landscape_runs_orientation_gate(self) -> None:
        """普通横屏包即使省略 --layout 也按备料标识拒绝竖屏视频。"""
        with TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "videos").mkdir()
            (bundle / "videos/clip.mp4").write_bytes(b"video")
            (bundle / "content.json").write_text(json.dumps({
                "schema_version": 1, "title": "测试", "script": "机器人", "sources": [],
                "prepared_for": validator.LANDSCAPE_LAYOUT,
            }), encoding="utf-8")
            (bundle / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "assets": [{"file": "videos/clip.mp4", "kind": "video", "summary": "机器人"}],
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.object(validator, "check_videos"), \
                    patch.object(validator, "probe_dimensions", return_value=(360, 640)), \
                    contextlib.redirect_stdout(output):
                result = validator.main(["validate_bundle.py", str(bundle)])
            self.assertEqual(result, 1)
            self.assertIn("videos/clip.mp4 显示尺寸 360×640", output.getvalue())

    def test_rejects_images_motion_metadata_and_insufficient_video(self) -> None:
        """图片、动效声明及无法覆盖口播的短片均被拒绝。"""
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "clip.mp4"
            image = folder / "still.png"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            resolved = [
                (video, {"file": "videos/clip.mp4", "kind": "video", "script_anchor": "无关内容"}),
                (image, {"file": "images/still.png", "kind": "image"}),
            ]
            report = validator.Report()
            with patch.object(validator, "probe_duration", return_value=3.0):
                validator.check_footage_bundle(
                    {"script": "机器人走进工厂", "editorial": {"prepared_for": validator.EDITORIAL_LAYOUT}},
                    resolved, report, 10.0, None,
                )
            self.assertTrue(any("不能包含图片" in error for error in report.errors))
            self.assertTrue(any("editorial" in error for error in report.errors))
            self.assertTrue(any("视频不足" in error for error in report.errors))
            self.assertTrue(any("script_anchor" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
