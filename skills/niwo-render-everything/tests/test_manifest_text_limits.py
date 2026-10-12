"""验证外部 Agent 能收到字段超限反馈并在修订后通过自校验。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import validate_bundle as validator


class ManifestTextLimitTests(unittest.TestCase):
    """核对完整校验入口的退出码、字段定位和修订后的结果。"""

    ASSET_FILE: ClassVar[str] = "images/asset.png"
    PNG_HEADER: ClassVar[bytes] = b"\x89PNG\r\n\x1a\n"
    LIMITS: ClassVar[dict[str, int]] = {"license": 128, "source_url": 2000, "script_anchor": 512}

    @classmethod
    def manifest(cls, field: str, value: str) -> dict[str, Any]:
        """构造只改变目标字段的素材清单。

        Args:
            field: 需要核对的素材字段。
            value: 该字段的测试值。

        Returns:
            dict[str, Any]: 包含单张图片的完整清单。
        """
        return {"schema_version": 1, "assets": [{
            "file": cls.ASSET_FILE, "kind": "image", "summary": "现场画面", "tags": ["现场"],
            field: value,
        }]}

    def test_field_boundaries(self) -> None:
        """长度边界通过，超限反馈包含字段、文件、实际长度和修订要求。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "images").mkdir()
            (bundle / self.ASSET_FILE).write_bytes(self.PNG_HEADER)
            for field, limit in self.LIMITS.items():
                for length in (limit - 1, limit):
                    with self.subTest(field=field, length=length):
                        report = validator.Report()
                        validator.validate_manifest(self.manifest(field, "字" * length), bundle, report)
                        self.assertFalse(report.errors, report.errors)
                with self.subTest(field=field, length=limit + 1):
                    report = validator.Report()
                    validator.validate_manifest(self.manifest(field, "字" * (limit + 1)), bundle, report)
                    errors = "\n".join(report.errors)
                    for expected in (f"manifest.assets[0].{field}", self.ASSET_FILE,
                                     str(limit + 1), str(limit), "修订"):
                        self.assertIn(expected, errors)

    def test_external_agent_revision_can_pass(self) -> None:
        """超限使入口失败且不改文件，外部修订后再次执行可通过。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "images").mkdir()
            (bundle / self.ASSET_FILE).write_bytes(self.PNG_HEADER)
            (bundle / "content.json").write_text(json.dumps({
                "schema_version": 1, "title": "自校验测试", "script": "现场画面。", "sources": [],
            }), encoding="utf-8")
            manifest_path = bundle / "manifest.json"
            for field, limit in self.LIMITS.items():
                with self.subTest(field=field):
                    original = json.dumps(self.manifest(field, "字" * (limit + 1)))
                    manifest_path.write_text(original, encoding="utf-8")
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result = validator.main(["validate_bundle.py", str(bundle)])
                    self.assertEqual(result, 1)
                    self.assertIn(f"manifest.assets[0].{field}", output.getvalue())
                    self.assertEqual(manifest_path.read_text(encoding="utf-8"), original)
                    manifest_path.write_text(json.dumps(self.manifest(field, "字" * limit)), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = validator.main(["validate_bundle.py", str(bundle)])
                    self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
