"""复现纯视频素材总量有余、局部镜头却不足的备料问题。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import validate_bundle as validator


def _check_capacity(
    tmp_path: Path,
    script: str,
    clips: list[tuple[str, float, bytes]],
    target: float = 90.0,
    narration: float | None = None,
) -> validator.Report:
    """用独立文件和指定探测时长检查备料容量。

    Args:
        tmp_path: 临时素材目录。
        script: 定稿口播。
        clips: 每段视频的原文锚点、时长与文件内容。
        target: 用户目标秒数。
        narration: 可选的实测口播秒数。

    Returns:
        validator.Report: 容量与逐句覆盖的校验结果。
    """
    resolved = []
    durations = {}
    for index, (anchor, seconds, payload) in enumerate(clips):
        path = tmp_path / f"{index}.mp4"
        path.write_bytes(payload)
        durations[path] = seconds
        resolved.append((path, {
            "file": f"videos/{index}.mp4", "kind": "video", "script_anchor": anchor,
        }))
    report = validator.Report()
    with patch.object(validator, "probe_duration", side_effect=durations.__getitem__):
        validator.check_footage_bundle({"script": script}, resolved, report, target, narration)
    return report


@pytest.mark.parametrize("seconds,accepted", [(100.0, False), (135.0, False), (140.0, True)])
def test_ninety_second_target_requires_rounded_reserve(
    tmp_path: Path, seconds: float, accepted: bool,
) -> None:
    """九十秒目标需要一百四十秒有效素材，而非只覆盖口播。

    Args:
        tmp_path: 临时素材目录。
        seconds: 实际素材时长。
        accepted: 是否满足含冗余的容量门槛。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。", [("机器人进入工厂。", seconds, b"a")])
    assert (not report.errors) is accepted
    if not accepted:
        assert any("140.0" in error for error in report.errors)


def test_total_reserve_cannot_hide_an_underprepared_sentence(tmp_path: Path) -> None:
    """总量达到一百四十秒时仍拒绝只给第二句六秒画面的包。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。监管方检查数据。", [
        ("机器人进入工厂。", 134.0, b"a"), ("监管方检查数据。", 6.0, b"b"),
    ])
    assert any("监管方检查数据" in error for error in report.errors)


def test_one_clip_cannot_be_counted_for_two_sentences(tmp_path: Path) -> None:
    """同一长片不能同时作为两个句子的独立镜头。

    Args:
        tmp_path: 临时素材目录。
    """
    script = "机器人进入工厂。监管方检查数据。"
    report = _check_capacity(tmp_path, script, [(script, 140.0, b"a")])
    assert report.errors


def test_duplicate_files_do_not_inflate_capacity(tmp_path: Path) -> None:
    """同内容换文件名仍只计一次，不可伪造冗余。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。", [
        ("机器人进入工厂。", 70.0, b"same"), ("机器人进入工厂。", 70.0, b"same"),
    ])
    assert any("视频不足" in error for error in report.errors)


def test_partial_anchor_must_be_completed_before_packaging(tmp_path: Path) -> None:
    """只有半句话的锚点无法证明后半句对应的素材已经备齐。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。", [("机器人", 140.0, b"a")])
    assert any("完整" in error for error in report.errors)


def test_actual_narration_longer_than_target_increases_reserve(tmp_path: Path) -> None:
    """实测口播变长后必须重新计算余量。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(
        tmp_path, "机器人进入工厂。", [("机器人进入工厂。", 140.0, b"a")], narration=100.0,
    )
    assert any("150.0" in error for error in report.errors)


def test_balanced_distinct_clips_cover_each_sentence(tmp_path: Path) -> None:
    """总量和逐句独立余量都达标的包可以通过。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。监管方检查数据。", [
        ("机器人进入工厂。", 70.0, b"a"), ("监管方检查数据。", 70.0, b"b"),
    ])
    assert not report.errors


def test_several_short_clips_cannot_replace_one_full_length_primary(tmp_path: Path) -> None:
    """逐句总量有余时仍要求主镜头足够长，避免只凑秒数。

    Args:
        tmp_path: 临时素材目录。
    """
    report = _check_capacity(tmp_path, "机器人进入工厂。", [
        ("机器人进入工厂。", 70.0, b"a"), ("机器人进入工厂。", 70.0, b"b"),
    ])
    assert any("主镜头" in error for error in report.errors)


def test_same_source_disjoint_windows_count_separately(tmp_path: Path) -> None:
    """同一原片的互不重叠区间可分别覆盖两句，不按文件数误去重。

    Args:
        tmp_path: 临时素材目录。
    """
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    sentences = ["机器人进入工厂。", "监管方检查数据。"]
    resolved = [(path, {
        "file": f"videos/{index}.mp4", "kind": "video", "script_anchor": sentence,
        "clip_start_seconds": index * 72.0, "clip_end_seconds": index * 72.0 + 70.0,
    }) for index, sentence in enumerate(sentences)]
    report = validator.Report()
    with patch.object(validator, "probe_duration", return_value=142.0):
        validator.check_footage_bundle({"script": "".join(sentences)}, resolved, report, 90.0, None)
    assert not report.errors


def test_same_source_overlap_is_not_added_twice(tmp_path: Path) -> None:
    """七十秒与重叠七十秒的区间只算一百秒有效素材。

    Args:
        tmp_path: 临时素材目录。
    """
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    sentence = "机器人进入工厂。"
    resolved = [(path, {
        "file": f"videos/{index}.mp4", "kind": "video", "script_anchor": sentence,
        "clip_start_seconds": start, "clip_end_seconds": start + 70.0,
    }) for index, start in enumerate([0.0, 30.0])]
    report = validator.Report()
    with patch.object(validator, "probe_duration", return_value=100.0):
        validator.check_footage_bundle({"script": sentence}, resolved, report, 90.0, None)
    assert any("可用 100.0 秒" in error for error in report.errors)
    assert any("重叠" in error for error in report.errors)
