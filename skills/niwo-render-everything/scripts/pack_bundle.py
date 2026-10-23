#!/usr/bin/env python3
"""Validate an immutable staging copy, package it, revalidate the ZIP, then publish.

Exit 1 means the external Agent must repair the input and rerun this command.
This program does not invent missing provenance, rewrite narration or call an LLM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from protocol_schema import LIMITS


def check(directory: Path, options: list[str]) -> dict:
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("validate_bundle.py")),
                             str(directory), "--json", *options], capture_output=True, text=True)
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError(f"校验程序未正常运行：{result.stderr or result.stdout}；修复运行环境后重试") from exc
    if result.returncode != 0 or report.get("status") != "passed":
        report["status"] = "repair_required"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--layout")
    parser.add_argument("--target-seconds", type=float)
    parser.add_argument("--narration-seconds", type=float)
    args = parser.parse_args(argv)
    source, output = args.bundle_dir.resolve(), args.output.resolve()
    options = []
    for name in ("layout", "target_seconds", "narration_seconds"):
        if getattr(args, name) is not None:
            options.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    report = {"status": "repair_required", "errors": [], "warnings": [], "deliverable": None}
    temporary_zip = None
    try:
        if not source.is_dir():
            raise ValueError("素材包目录不存在")
        if output == source or source in output.parents or output.suffix.lower() != ".zip":
            raise ValueError("输出必须是素材包目录之外的 .zip 文件")
        with tempfile.TemporaryDirectory(prefix="niwo-pack-") as temp:
            staging = Path(temp) / "staging"
            staging.mkdir()
            # Copy only the documented package structure. Reject symlinks before
            # following them, including symlinked media directories.
            for relative in ("content.json", "manifest.json", "images", "videos"):
                entry = source / relative
                if entry.is_symlink():
                    raise ValueError(f"{relative} 不接受软链接")
                if not entry.exists():
                    continue  # The validator reports required missing JSON files.
                if entry.is_dir():
                    for path in entry.rglob("*"):
                        if path.is_symlink():
                            raise ValueError(f"{path.relative_to(source)} 不接受软链接")
                    shutil.copytree(entry, staging / relative)
                else:
                    shutil.copy2(entry, staging / relative)
            report = check(staging, options)
            report["deliverable"] = None
            if report["status"] != "passed":
                print(json.dumps(report, ensure_ascii=False))
                return 1
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_zip = tempfile.mkstemp(prefix=".niwo-", suffix=".zip", dir=output.parent)
            os.close(fd)
            with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(staging.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(staging).as_posix())
            if Path(temporary_zip).stat().st_size > LIMITS["zip_bytes_max"]:
                raise ValueError("最终 ZIP 超过 200 MiB；Agent 应压缩素材后重新打包")
            with zipfile.ZipFile(temporary_zip) as archive:
                if archive.testzip() is not None:
                    raise ValueError("最终 ZIP 完整性检查失败，请重新打包")
                # This archive was just constructed from our own regular files.
                extracted = Path(temp) / "extracted"
                archive.extractall(extracted)
            report = check(extracted, options)
            report["deliverable"] = None
            if report["status"] != "passed":
                print(json.dumps(report, ensure_ascii=False))
                return 1
            digest = hashlib.sha256(Path(temporary_zip).read_bytes()).hexdigest()
            os.replace(temporary_zip, output)
            temporary_zip = None
            report.update(deliverable=str(output), sha256=digest,
                          next_action="仅交付此路径及 SHA-256 对应的已验收 ZIP；改动后重新执行本命令")
            print(json.dumps(report, ensure_ascii=False))
            return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        report.update(status="repair_required", deliverable=None,
                      errors=[{"code": "pack_failed", "message": str(exc)}],
                      next_action="外部 Agent 自主修复文件、字段或运行环境，再执行相同打包命令；不要交付旧 ZIP")
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        if temporary_zip is not None:
            Path(temporary_zip).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
