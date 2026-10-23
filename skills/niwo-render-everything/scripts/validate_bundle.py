#!/usr/bin/env python3
"""素材包打包前的本地自校验，只用标准库，可在任意目录下运行。

检查字段协议和本地备料质量。新版横屏备料另检查段落关联、派生前景和有效视频容量；
这些备料要求不代表其他版面的服务端准入规则。媒体验收需要 ffprobe 和 ffmpeg。

用法:
    python3 validate_bundle.py <素材包目录> [--layout landscape-full-v1] [--target-seconds 300]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from protocol_schema import LIMITS, SCHEMA, validate as validate_protocol

_CONTENT = SCHEMA["$defs"]["BundleContent"]["properties"]
_ASSET = SCHEMA["$defs"]["BundleManifestAsset"]["properties"]
_MANIFEST = SCHEMA["$defs"]["BundleManifest"]["properties"]

CONTENT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

PINYIN_PATTERN = re.compile(r"^[a-z]+[1-5]$")
HEADLINE_HIGHLIGHT_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")

MAX_TITLE_CHARS = _CONTENT["title"]["maxLength"]
MAX_SCRIPT_CHARS = _CONTENT["script"]["maxLength"]
MAX_NOTES_CHARS = _CONTENT["notes"]["maxLength"]
MAX_HEADLINE_LINES = 3
# 标题带的容量是宽度而非字数：中日韩字符占满一个字宽，英文数字只占 0.56。
NARROW_CHAR_WIDTH = 0.56
MAX_HEADLINE_LINE_WIDTH = 14.0
WARN_HEADLINE_LINE_WIDTH = 12.0
MAX_HIGHLIGHTS_PER_LINE = 2
# 主标题下方那行英文副标题，只有卡片式信息版会排它。它排在主标题四分之一的字号上，
# 一行放得下大半句英文；再长就会挤掉主标题的字号。
MAX_SUBHEADLINE_CHARS = 72
MAX_SOURCE_ENTRY_WIDTH = 24.0
MAX_SUMMARY_CHARS = 240
MAX_LICENSE_CHARS = _ASSET["license"]["maxLength"]
MAX_SOURCE_URL_CHARS = _ASSET["source_url"]["maxLength"]
MAX_SCRIPT_ANCHOR_CHARS = _ASSET["script_anchor"]["maxLength"]
MAX_TAG_CHARS = _ASSET["tags"]["items"]["maxLength"]
MAX_TAGS = _ASSET["tags"]["maxItems"]
MAX_ENTRIES = _MANIFEST["assets"]["maxItems"]
MAX_IMAGE_BYTES = LIMITS["image_bytes_max"]
MAX_VIDEO_BYTES = LIMITS["video_bytes_max"]
EDITORIAL_LAYOUT = "landscape-editorial-motion-v1"
FOOTAGE_LAYOUT = "landscape-footage-v1"
LANDSCAPE_LAYOUT = "landscape-full-v1"
LANDSCAPE_LAYOUTS = frozenset({LANDSCAPE_LAYOUT, FOOTAGE_LAYOUT, EDITORIAL_LAYOUT})
EDITORIAL_MEDIA_FILES_MAX = LIMITS["media_files_max"]
# Niwo 接收的 ZIP 上限是 200 MiB；打包前给压缩格式、文件名和目录项留 5 MiB 余量。
EDITORIAL_SOURCE_BYTES_MAX = 195 * 1024 * 1024
EDITORIAL_ROLES = frozenset({"hook", "title", "origin", "explain", "evidence", "contrast", "conclusion"})
VIDEO_COVERAGE_RATIO = 0.9
# 纯视频没有文字卡补位，备料按一倍半并向上取整到十秒留余量。
FOOTAGE_RESERVE_RATIO = 1.5
FOOTAGE_CAPACITY_ROUND_SECONDS = 10
FOOTAGE_HASH_CHUNK_BYTES = 1024 * 1024
FOOTAGE_SENTENCE_PATTERN = re.compile(r".+?(?:[。！？!?；;\n]|\.(?=\s|$)|$)", re.DOTALL)
PROBE_TIMEOUT_SECONDS = 30
EDITORIAL_ID_CHARS = 128
EDITORIAL_TEXT_CHARS = 2000
EDITORIAL_PATH_CHARS = 260
EDITORIAL_MAX_SEGMENTS = 128

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})

CONTENT_KEYS = frozenset(
    {
        "schema_version",
        "title",
        "script",
        "hook_headline",
        "sources",
        "pronunciations",
        "notes",
        "editorial",
        "prepared_for",
    }
)
# 常见的误写：这些都是用户在 Niwo 里渲染前才决定的参数，属于 render.json 而不是内容协议。
RENDER_PARAM_KEYS = frozenset(
    {
        "video_format",
        # 成片形态更早还叫 aspect_ratio，取值写成画面比例。
        "aspect_ratio",
        "task_id",
        "user_id",
        "generation",
        "theme",
        "tts",
        "bgm",
        "bgm_mood",
        "sfx",
        "assets",
        "output",
        "model",
        "vision_model",
        "reasoning_effort",
        "asset_cache",
        "voice_id",
        "speed",
        "show_subtitles",
        "show_chapter_progress",
        "show_sources",
        "disclaimer",
        "character",
        "shared_template",
        "enable_tikhub",
        "enable_visual_review",
        "enable_asset_search",
        "brand_bar",
    }
)
ENTRY_KEYS = frozenset(
    {
        "file",
        "kind",
        "summary",
        "tags",
        "source_url",
        "license",
        "script_anchor",
        "clip_start_seconds",
        "clip_end_seconds",
        "relevance_score",
        "editorial",
    }
)

MAGIC_BYTES = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".mkv": (b"\x1a\x45\xdf\xa3",),
    ".webm": (b"\x1a\x45\xdf\xa3",),
}


class Report:
    """收集字段和备料错误，以及需人工确认的质量告警。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_json(path: Path, report: Report) -> dict[str, Any] | None:
    """读取并解析一个必需的 JSON 文件。"""
    if not path.is_file():
        report.error(f"缺少 {path.name}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        report.error(f"{path.name} 无法解析: {error}")
        return None
    if not isinstance(payload, dict):
        report.error(f"{path.name} 顶层必须是 JSON 对象")
        return None
    return payload


def check_pronunciations(value: Any, report: Report) -> None:
    """校验多音字注音与词条逐字对应。"""
    if not isinstance(value, dict):
        report.error("pronunciations 必须是对象")
        return
    for word, pinyin in value.items():
        cleaned = word.strip()
        if not cleaned:
            report.error("pronunciations 的词条不能为空")
            continue
        if not isinstance(pinyin, str):
            report.error(f"pronunciations[{word!r}] 的值必须是字符串")
            continue
        tokens = pinyin.split()
        if len(tokens) != len(cleaned):
            report.error(
                f"pronunciations[{cleaned!r}] 需要 {len(cleaned)} 个音节，"
                f"实际给出 {len(tokens)} 个: {pinyin!r}"
            )
            continue
        for token in tokens:
            if token != "-" and not PINYIN_PATTERN.match(token.lower()):
                report.error(
                    f"pronunciations[{cleaned!r}] 的拼音 {token!r} 非法，"
                    "需为带声调数字的拼音（如 diao4、lv4、yi5），ü 写作 v"
                )
        if all(token == "-" for token in tokens):
            report.error(
                f"pronunciations[{cleaned!r}] 全是 -，没有任何需要修正的读音，"
                "请删掉这个词条"
            )


def _headline_display_width(line: str) -> float:
    """按去掉 [[ ]] 标记后的可见文字估算标题行宽度，单位为一个中文字宽。"""
    visible = HEADLINE_HIGHLIGHT_PATTERN.sub(r"\1", line)
    return sum(1.0 if ord(char) > 0x2E80 else NARROW_CHAR_WIDTH for char in visible)


def _count_headline_highlights(line: str) -> int:
    """统计一行里合法 [[...]] 高亮标记的数量。"""
    return len(HEADLINE_HIGHLIGHT_PATTERN.findall(line))


def _display_width(text: str) -> float:
    """按可见文字估算宽度，单位为一个中文字宽。"""
    return sum(1.0 if ord(char) > 0x2E80 else NARROW_CHAR_WIDTH for char in text)


def check_sources(value: Any, report: Report) -> None:
    """校验资料来源列表的换行与单条宽度。"""
    if not isinstance(value, list):
        report.error("sources 必须是数组")
        return
    if not value:
        report.warn(
            "sources 是空数组，引用过公开资料就默认写上来源名称；"
            "确实一条都没引用时可以忽略这条"
        )
        return
    seen: set[str] = set()
    for index, entry in enumerate(value):
        label = f"sources[{index}]"
        if not isinstance(entry, str):
            report.error(f"{label} 必须是字符串")
            continue
        collapsed = " ".join(entry.split()).strip()
        if not collapsed:
            report.warn(f"{label} 为空，已忽略")
            continue
        if collapsed in seen:
            report.warn(f"{label} 与前面的条目重复，已忽略")
            continue
        if "\n" in entry:
            report.error(f"{label} 不能包含换行符")
            continue
        width = _display_width(collapsed)
        if width > MAX_SOURCE_ENTRY_WIDTH:
            report.error(
                f"{label} {collapsed!r} 折合 {width:.1f} 个中文字宽，"
                f"超过上限 {MAX_SOURCE_ENTRY_WIDTH:g}"
            )
            continue
        seen.add(collapsed)


def check_headline_subtitle(value: Any, report: Report) -> None:
    """校验主标题下方那行英文副标题。缺省是常态，只有写了才校验。"""
    if value is None:
        return
    if not isinstance(value, str):
        report.error("hook_headline.subtitle 必须是字符串")
        return
    if "\n" in value:
        report.error("hook_headline.subtitle 不能包含换行符")
        return
    if len(value) > MAX_SUBHEADLINE_CHARS:
        report.error(
            f"hook_headline.subtitle 有 {len(value)} 字符，"
            f"超过上限 {MAX_SUBHEADLINE_CHARS}"
        )


def check_hook_headline(value: Any, report: Report) -> None:
    """校验顶部钩子大标题的行数、高亮标记与单行长度。"""
    if not isinstance(value, dict):
        report.error("hook_headline 必须是对象")
        return
    for key in sorted(set(value) - {"lines", "subtitle"}):
        report.error(f"hook_headline 出现未知字段 {key!r}")
    check_headline_subtitle(value.get("subtitle"), report)
    lines = value.get("lines")
    if not isinstance(lines, list):
        report.error("hook_headline.lines 必须是数组")
        return
    if not lines:
        report.error("hook_headline.lines 不能为空，至少需要 1 行")
        return
    if len(lines) > MAX_HEADLINE_LINES:
        report.error(
            f"hook_headline.lines 有 {len(lines)} 行，超过上限 {MAX_HEADLINE_LINES}"
        )
        return

    total_highlights = 0
    for index, line in enumerate(lines):
        label = f"hook_headline.lines[{index}]"
        if not isinstance(line, str):
            report.error(f"{label} 必须是字符串")
            continue
        if not line.strip():
            report.error(f"{label} 不能为空")
            continue
        if "\n" in line:
            report.error(f"{label} 不能包含换行符")
            continue

        highlights = _count_headline_highlights(line)
        remaining = HEADLINE_HIGHLIGHT_PATTERN.sub("", line)
        if "[[" in remaining or "]]" in remaining:
            report.error(
                f"{label} 的高亮标记不成对、嵌套或内容为空: {line!r}"
            )
            continue
        if highlights > MAX_HIGHLIGHTS_PER_LINE:
            report.error(
                f"{label} 有 {highlights} 处高亮，超过每行上限 {MAX_HIGHLIGHTS_PER_LINE}"
            )
        total_highlights += highlights

        width = _headline_display_width(line)
        if width > MAX_HEADLINE_LINE_WIDTH:
            report.error(
                f"{label} 折合 {width:.1f} 个中文字宽，超过上限 "
                f"{MAX_HEADLINE_LINE_WIDTH:g}（英文与数字按半个字算）"
            )
        elif width > WARN_HEADLINE_LINE_WIDTH:
            report.warn(
                f"{label} 折合 {width:.1f} 个中文字宽，超过建议的 "
                f"{WARN_HEADLINE_LINE_WIDTH:g} 字，顶部标题带可能放不下"
            )

    if total_highlights == 0:
        report.warn(
            "hook_headline 没有任何 [[ ]] 高亮，会退化成普通标题"
        )


def validate_content(content: dict[str, Any], report: Report) -> None:
    """校验 content.json 的字段取值。"""
    for key in sorted(set(content) - CONTENT_KEYS):
        if key in ("video_format", "aspect_ratio"):
            report.error(
                f"content.json 不要写 {key!r}，成片形态由用户在 Niwo 渲染界面上选。"
                "你只按确认过的取向收素材，并在 notes 里提醒用户选一致的形态"
            )
        elif key in RENDER_PARAM_KEYS:
            report.error(
                f"content.json 不要写 {key!r}，它是渲染参数，"
                "由用户在 Niwo 里渲染前自己选"
            )
        else:
            report.error(
                f"content.json 出现未知字段 {key!r}，协议不接受未列出的字段"
            )

    prepared_for = content.get("prepared_for")
    if prepared_for is not None and prepared_for not in (LANDSCAPE_LAYOUT, FOOTAGE_LAYOUT):
        report.error("content.prepared_for 只能是 landscape-full-v1 或 landscape-footage-v1")
    if prepared_for is not None and content.get("editorial") is not None:
        report.error("content.prepared_for 不能与 editorial 备料声明同时使用")

    if content.get("schema_version") != CONTENT_SCHEMA_VERSION:
        report.error(
            f"content.json 的 schema_version 必须是 {CONTENT_SCHEMA_VERSION}，"
            f"实际为 {content.get('schema_version')!r}"
        )

    title = content.get("title")
    if not isinstance(title, str) or not title.strip():
        report.error("title 不能为空，用户在 Niwo 里靠它认出这个素材包")
    elif len(title) > MAX_TITLE_CHARS:
        report.error(f"title 有 {len(title)} 字，超过上限 {MAX_TITLE_CHARS}")

    script = content.get("script")
    if not isinstance(script, str) or not script.strip():
        report.error("script 不能为空")
    else:
        if len(script) > MAX_SCRIPT_CHARS:
            report.error(f"script 长度 {len(script)} 超过上限 {MAX_SCRIPT_CHARS}")
        if "\n" in script:
            report.warn("script 含换行符，应该是一整段连续正文")
        if re.search(r"[\U0001f300-\U0001faff\u2600-\u27bf]", script):
            report.warn("script 含 emoji，配音会把它读出来或跳过，建议删掉")

    if "pronunciations" in content:
        check_pronunciations(content["pronunciations"], report)

    if content.get("hook_headline") is not None:
        check_hook_headline(content["hook_headline"], report)

    if "sources" in content:
        check_sources(content["sources"], report)
    else:
        report.warn(
            "content.json 没写 sources，引用过公开资料就默认带上来源名称；"
            "确实一条都没引用时可以忽略这条"
        )

    notes = content.get("notes")
    if isinstance(notes, str) and len(notes) > MAX_NOTES_CHARS:
        report.error(f"notes 有 {len(notes)} 字，超过上限 {MAX_NOTES_CHARS}")


def validate_manifest(
    manifest: dict[str, Any], bundle_dir: Path, report: Report
) -> list[tuple[Path, dict[str, Any]]]:
    """校验 manifest.json 并返回落在磁盘上的素材条目。"""
    for key in sorted(
        set(manifest) - {"schema_version", "generated_by", "notes", "assets"}
    ):
        report.error(f"manifest.json 出现未知字段 {key!r}")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        report.error(
            f"manifest.json 的 schema_version 必须是 {MANIFEST_SCHEMA_VERSION}，"
            f"实际为 {manifest.get('schema_version')!r}"
        )

    entries = manifest.get("assets")
    if not isinstance(entries, list):
        report.error("manifest.assets 必须是数组")
        return []
    if not entries:
        report.error("manifest.assets 是空的，至少要声明一个素材")
        return []
    if len(entries) > MAX_ENTRIES:
        report.error(f"manifest.assets 有 {len(entries)} 条，超过上限 {MAX_ENTRIES}")

    resolved: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"manifest.assets[{index}]"
        if not isinstance(entry, dict):
            report.error(f"{label} 必须是对象")
            continue
        for key in sorted(set(entry) - ENTRY_KEYS):
            report.error(f"{label} 出现未知字段 {key!r}")

        raw_file = entry.get("file")
        if not isinstance(raw_file, str) or not raw_file.strip():
            report.error(f"{label} 缺少 file")
            continue
        cleaned = raw_file.strip().replace("\\", "/")
        if "://" in cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
            report.error(f"{label} 的 file {raw_file!r} 必须是素材包内的相对路径")
            continue
        if cleaned in seen:
            report.error(f"manifest 重复声明素材: {cleaned}")
            continue
        seen.add(cleaned)

        if not cleaned.startswith(("images/", "videos/")):
            report.error(f"{label}.file 必须位于 images/ 或 videos/ 下")
            continue
        path = bundle_dir / cleaned
        if path.is_symlink() or bundle_dir.resolve() not in path.resolve().parents:
            report.error(f"{label}.file 不接受软链接或包外文件")
            continue
        kind = _check_entry_kind(entry, cleaned, label, report)
        if not path.is_file():
            report.error(f"{label} 声明的文件不存在: {cleaned}")
            continue
        _check_entry_text(entry, label, report)
        _check_entry_clip(entry, kind, label, report)
        _check_file_bytes(path, cleaned, kind, report)
        resolved.append((path, {**entry, "file": cleaned, "kind": kind}))
    return resolved


def _check_entry_kind(
    entry: dict[str, Any], cleaned: str, label: str, report: Report
) -> str | None:
    kind = entry.get("kind")
    if kind not in ("image", "video"):
        report.error(f"{label} 的 kind 必须是 image 或 video，实际为 {kind!r}")
        return None
    suffix = Path(cleaned).suffix.lower()
    allowed = IMAGE_EXTENSIONS if kind == "image" else VIDEO_EXTENSIONS
    if suffix not in allowed:
        report.error(
            f"{label} 的扩展名 {suffix or '(无)'} 与 kind={kind} 不匹配，"
            f"允许: {', '.join(sorted(allowed))}"
        )
        return None
    return kind


def _check_entry_text(entry: dict[str, Any], label: str, report: Report) -> None:
    """检查素材文字字段，将超限信息交给调用 Skill 的外部 Agent 修订。

    Args:
        entry: 当前素材条目。
        label: 当前条目在素材清单中的位置。
        report: 收集错误与质量告警的报告。

    Returns:
        None: 检查结果写入报告，不修改素材清单。
    """
    # 这些字段不能机械截断；由外部 Agent 保留有效授权、网址和原文后重新校验。
    for field, limit in (
        ("license", MAX_LICENSE_CHARS),
        ("source_url", MAX_SOURCE_URL_CHARS),
        ("script_anchor", MAX_SCRIPT_ANCHOR_CHARS),
    ):
        value = entry.get(field, "")
        location = f"{label}.{field}（素材 {entry.get('file', '')}）"
        if not isinstance(value, str):
            report.error(f"{location} 必须是字符串，请外部 Agent 修订 manifest.json 后重新校验")
        elif len(value) > limit:
            report.error(
                f"{location} 有 {len(value)} 字符，超过上限 {limit}；"
                "请外部 Agent 修订 manifest.json 后重新校验，通过前禁止打包 ZIP"
            )

    summary = entry.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        report.warn(f"{label} 没写 summary，Niwo 会用视觉模型自己给它补描述")
    else:
        if "\n" in summary:
            report.warn(f"{label} 的 summary 含换行，应该是单行画面描述")
        if len(summary) > MAX_SUMMARY_CHARS:
            report.warn(
                f"{label} 的 summary 有 {len(summary)} 字，"
                f"超过 {MAX_SUMMARY_CHARS} 的部分会被截断"
            )

    tags = entry.get("tags", [])
    if not isinstance(tags, list):
        report.error(f"{label} 的 tags 必须是数组")
        return
    if not tags:
        report.warn(f"{label} 没写 tags，summary 要配合 tags 才能替代视觉打标")
    if len(tags) > MAX_TAGS:
        report.error(f"{label} 有 {len(tags)} 个 tags，最多 {MAX_TAGS} 个；Agent 应保留最相关标签后复检")
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            report.error(f"{label} 的 tags 含空标签")
        elif len(tag) > MAX_TAG_CHARS:
            report.error(f"{label} 的标签 {tag!r} 超过 {MAX_TAG_CHARS} 字；Agent 应精简标签后复检")


def _check_entry_clip(
    entry: dict[str, Any], kind: str | None, label: str, report: Report
) -> None:
    start = entry.get("clip_start_seconds")
    end = entry.get("clip_end_seconds")
    if kind == "image" and (start is not None or end is not None):
        report.error(f"{label} 是图片，不能声明 clip_start_seconds / clip_end_seconds")
        return
    for name, value in (("clip_start_seconds", start), ("clip_end_seconds", end)):
        if value is not None and not isinstance(value, (int, float)):
            report.error(f"{label} 的 {name} 必须是数字")
            return
    if start is not None and start < 0:
        report.error(f"{label} 的 clip_start_seconds 不能是负数")
    if start is not None and end is not None and end <= start:
        report.error(f"{label} 的选段终点必须大于起点")


def _check_file_bytes(
    path: Path, cleaned: str, kind: str | None, report: Report
) -> None:
    """检查文件非空、体积在上限内，并用文件头识破下载到的错误页。"""
    size = path.stat().st_size
    if size == 0:
        report.error(f"{cleaned} 是空文件，下载可能失败了")
        return
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if size > limit:
        report.error(
            f"{cleaned} 有 {size / 1024 / 1024:.1f} MB，"
            f"超过上限 {limit // 1024 // 1024} MB"
        )

    with path.open("rb") as handle:
        head = handle.read(16)
    if head[:1] == b"<" or head[:5].lower() == b"<!doc":
        report.error(f"{cleaned} 内容是 HTML，说明下载到的是网页而不是素材文件")
        return
    suffix = Path(cleaned).suffix.lower()
    if suffix == ".webp":
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            report.error(f"{cleaned} 的文件头不是 webp，扩展名和真实格式不一致")
        return
    if suffix in (".mp4", ".mov"):
        if head[4:8] != b"ftyp":
            report.error(f"{cleaned} 的文件头不是 mp4/mov，扩展名和真实格式不一致")
        return
    expected = MAGIC_BYTES.get(suffix)
    if expected and not any(head.startswith(prefix) for prefix in expected):
        report.error(
            f"{cleaned} 的文件头与扩展名 {suffix} 不一致，可能下载到了别的格式"
        )


def probe_duration(path: Path) -> float | None:
    """用 ffprobe 读时长，读不出来返回 None。"""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    """读取视频显示方向的宽高，包含旋转元数据。

    Args:
        path: 待验收的视频文件。

    Returns:
        tuple[int, int] | None: 显示宽高；读取失败时为空。
    """
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:stream_tags=rotate:stream_side_data=rotation", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
        if completed.returncode != 0:
            return None
        streams = json.loads(completed.stdout).get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        width, height = stream.get("width"), stream.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            return None
        side_data = stream.get("side_data_list") or []
        rotation = next((item.get("rotation") for item in side_data if item.get("rotation") is not None), None)
        if rotation is None:
            rotation = (stream.get("tags") or {}).get("rotate", 0)
        if round(float(rotation)) % 180 == 90:
            width, height = height, width
        return width, height
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError):
        return None


def check_landscape_video_orientation(
    resolved: list[tuple[Path, dict[str, Any]]], layout: str, report: Report,
) -> None:
    """横屏版面逐条拒绝竖屏或无法确认方向的视频。

    Args:
        resolved: 已找到的素材文件及其声明。
        layout: 用户选择的横屏版面。
        report: 收集校验结果的报告。

    Returns:
        None: 方向问题直接写入报告。
    """
    for path, entry in resolved:
        if entry["kind"] != "video":
            continue
        name = entry["file"]
        dimensions = probe_dimensions(path)
        if dimensions is None:
            report.error(f"{name} 无法读取显示宽高，{layout} 的视频方向未验收")
        elif dimensions[0] <= dimensions[1]:
            report.error(f"{name} 显示尺寸 {dimensions[0]}×{dimensions[1]}，{layout} 必须使用横屏视频")


def check_videos(resolved: list[tuple[Path, dict[str, Any]]], report: Report) -> None:
    """核对视频可解码，且选段窗口落在真实时长内。"""
    videos = [(path, entry) for path, entry in resolved if entry["kind"] == "video"]
    if not videos:
        return
    if shutil.which("ffprobe") is None:
        report.error("环境里没有 ffprobe；Agent 应补齐运行环境后重新检查视频，不跳过验收")
        return
    for path, entry in videos:
        name = entry["file"]
        duration = probe_duration(path)
        if duration is None:
            report.error(f"{name} 无法被 ffprobe 解码，文件可能损坏或下载不完整")
            continue
        if duration < 1.0:
            report.error(f"{name} 只有 {duration:.2f} 秒，太短了，至少要几秒")
        end = entry.get("clip_end_seconds")
        start = entry.get("clip_start_seconds")
        if end is not None and end > duration + 0.1:
            report.error(
                f"{name} 的 clip_end_seconds={end} 超过实际时长 {duration:.2f} 秒"
            )
        elif start is not None and start >= duration:
            report.error(
                f"{name} 的 clip_start_seconds={start} 已经超过实际时长 "
                f"{duration:.2f} 秒"
            )
        if start is None and end is None and duration > 20.0:
            report.warn(
                f"{name} 长 {duration:.1f} 秒却没标选段，Niwo 会从第 0 秒开始取，"
                "建议裁成几秒的短镜头或补上 clip_start_seconds / clip_end_seconds"
            )


def check_orphans(
    bundle_dir: Path, resolved: list[tuple[Path, dict[str, Any]]], report: Report,
    *, editorial_mode: bool = False,
) -> None:
    """核对未声明素材；新横屏版拒绝，普通版面保留原提示。"""
    declared = {path.resolve() for path, _ in resolved}
    for subdir in ("images", "videos"):
        directory = bundle_dir / subdir
        if not directory.is_dir():
            continue
        # 新横屏的 manifest 必须覆盖子目录素材；普通版面维持原有顶层提示范围。
        paths = directory.rglob("*")
        for path in sorted(paths):
            if not path.is_file() or path.name.startswith("."):
                continue
            label = path.relative_to(bundle_dir).as_posix()
            if path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                report.error(f"{label} 不是支持的素材格式，请移出媒体目录后复检")
            elif path.resolve() not in declared:
                if editorial_mode:
                    report.error(f"{label} 没写进 manifest，新横屏版必须声明素材用途")
                else:
                    report.warn(
                        f"{label} 没写进 manifest，"
                        "仍然可用但 Niwo 会用视觉模型自己补描述"
                    )


def check_foreign_declarations(bundle_dir: Path, report: Report) -> None:
    """提醒删掉不该由外部产出的声明文件。"""
    if (bundle_dir / "task.json").is_file():
        report.warn(
            "目录里还有 task.json，它是旧协议。内容请写进 content.json，"
            "确认迁移完成后删掉 task.json，别打进 zip"
        )
    if (bundle_dir / "render.json").is_file():
        report.warn(
            "目录里有 render.json，那是音色、配乐、模型这类渲染参数，"
            "由用户在 Niwo 里渲染前自己选，不该由你产出，别打进 zip"
        )


def count_kinds(resolved: list[tuple[Path, dict[str, Any]]]) -> tuple[int, int]:
    images = sum(1 for _, entry in resolved if entry["kind"] == "image")
    videos = sum(1 for _, entry in resolved if entry["kind"] == "video")
    return images, videos


def check_editorial_upload_limits(bundle_dir: Path, report: Report) -> None:
    """保守按包目录内全部文件检查新横屏素材包的 Niwo 上传门槛。

    Args:
        bundle_dir: 包含协议文件与媒体目录的素材包目录。
        report: 结果收集器。

    Returns:
        None: 超过媒体数量或预留 ZIP 开销后的体积门槛时记录错误。
    """
    media_files = [
        path for dirname in ("images", "videos")
        for path in (bundle_dir / dirname).rglob("*") if path.is_file()
    ]
    if len(media_files) > EDITORIAL_MEDIA_FILES_MAX:
        report.error(
            f"素材包媒体文件有 {len(media_files)} 个，超过 Niwo 上传上限 "
            f"{EDITORIAL_MEDIA_FILES_MAX} 个；请合并或删去低价值镜头"
        )
    # 配乐、说明等附带文件也会进入 ZIP；只合计媒体会让上传时才发现超限。
    package_files = [path for path in bundle_dir.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in package_files)
    if total_bytes > EDITORIAL_SOURCE_BYTES_MAX:
        report.error(
            f"素材包待打包文件共 {total_bytes / 1024 / 1024:.1f} MiB，"
            f"超过打包前上限 {EDITORIAL_SOURCE_BYTES_MAX // 1024 // 1024} MiB；"
            "Niwo ZIP 上限为 200 MiB，请压缩视频并为 ZIP 开销留余量"
        )


def _finite_number(value: Any) -> bool:
    """判断真实有限数值，避免布尔值和非有限值混入坐标与时间。

    Args:
        value: 待检查的字段值。
    Returns:
        bool: 值是否为有限数字。
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _editorial_object(value: Any, keys: set[str], label: str, report: Report) -> dict[str, Any]:
    """检查专属元数据对象及未知键。

    Args:
        value: 待检查的对象。
        keys: 允许的键。
        label: 字段位置。
        report: 结果收集器。

    Returns:
        dict[str, Any]: 输入对象，类型错误时返回空对象。
    """
    if not isinstance(value, dict):
        report.error(f"{label} 必须是对象")
        return {}
    for key in sorted(set(value) - keys):
        report.error(f"{label} 出现未知字段 {key!r}")
    return value


def _editorial_text(value: Any, label: str, report: Report, limit: int = EDITORIAL_TEXT_CHARS) -> bool:
    """检查必需的非空文本，不改写用户原文。

    Args:
        value: 文本值。
        label: 字段位置。
        report: 结果收集器。
        limit: 字符上限。

    Returns:
        bool: 是否为非空字符串。
    """
    valid = isinstance(value, str) and bool(value.strip())
    if not valid:
        report.error(f"{label} 必须是非空字符串")
    elif len(value) > limit:
        report.error(f"{label} 超过 {limit} 字符")
        return False
    return valid


def check_editorial_content(content: dict[str, Any], report: Report) -> dict[str, dict[str, Any]]:
    """校验备料意图和原文锚点，不据此设置最终渲染版面。

    Args:
        content: 内容声明。
        report: 结果收集器。

    Returns:
        dict[str, dict[str, Any]]: 以唯一编号索引的段落。
    """
    if content.get("editorial") is None:
        return {}
    editorial = _editorial_object(content["editorial"], {"prepared_for", "title_text", "title_anchor", "segments"}, "content.editorial", report)
    if editorial.get("prepared_for") != EDITORIAL_LAYOUT:
        report.error(f"content.editorial.prepared_for 必须是 {EDITORIAL_LAYOUT}")
    _editorial_text(editorial.get("title_text"), "content.editorial.title_text", report)
    script = content.get("script") if isinstance(content.get("script"), str) else ""
    anchor = editorial.get("title_anchor")
    if _editorial_text(anchor, "content.editorial.title_anchor", report) and anchor not in script:
        report.error("content.editorial.title_anchor 必须从定稿 script 原样摘取")
    segments = editorial.get("segments")
    if not isinstance(segments, list) or not segments:
        report.error("content.editorial.segments 必须是非空数组")
        return {}
    if len(segments) > EDITORIAL_MAX_SEGMENTS:
        report.error(f"content.editorial.segments 超过 {EDITORIAL_MAX_SEGMENTS} 项")
    indexed: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(segments):
        label = f"content.editorial.segments[{index}]"
        segment = _editorial_object(value, {"id", "script_anchor", "role", "visual_goal"}, label, report)
        identifier = segment.get("id")
        if _editorial_text(identifier, f"{label}.id", report, EDITORIAL_ID_CHARS):
            if identifier in indexed:
                report.error(f"{label}.id 重复: {identifier}")
            indexed[identifier] = segment
        role = segment.get("role")
        if not isinstance(role, str) or role not in EDITORIAL_ROLES:
            report.error(f"{label}.role 必须是约定的叙事角色")
        _editorial_text(segment.get("visual_goal"), f"{label}.visual_goal", report)
        anchor = segment.get("script_anchor")
        if _editorial_text(anchor, f"{label}.script_anchor", report) and anchor not in script:
            report.error(f"{label}.script_anchor 必须从定稿 script 原样摘取")
    return indexed


def check_foreground_alpha(path: Path, report: Report) -> None:
    """用真实解码的透明通道排除不透明截图与全空前景。

    Args:
        path: PNG 路径。
        report: 结果收集器。

    Returns:
        None: 将不合格或未能验收的结果加入错误列表。
    """
    if shutil.which("ffmpeg") is None:
        report.error(f"{path.name} 前景透明度未验收：缺少 ffmpeg")
        return
    try:
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
            "alphaextract,signalstats,metadata=print:file=-", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        report.error(f"{path.name} 前景透明度未验收: {error}")
        return
    minimum = re.search(r"lavfi\.signalstats\.YMIN=([\d.]+)", result.stdout)
    maximum = re.search(r"lavfi\.signalstats\.YMAX=([\d.]+)", result.stdout)
    if result.returncode or not minimum or not maximum:
        report.error(f"{path.name} 无法解码透明通道，不能把无透明度的截图当作前景")
    elif float(minimum.group(1)) >= 255:
        report.error(f"{path.name} 完全不透明，必须保留主体以外的透明区域")
    elif float(maximum.group(1)) <= 0:
        report.error(f"{path.name} 完全透明，没有可见主体")


def check_editorial_bundle(content: dict[str, Any], resolved: list[tuple[Path, dict[str, Any]]],
    report: Report, target_seconds: float | None = None, narration_seconds: float | None = None) -> None:
    """检查新版横屏的素材关联、派生来源、实测容量和纯视频底片要求。

    Args:
        content: 内容声明。
        resolved: 已解析素材。
        report: 结果收集器。
        target_seconds: 用户确认的目标时长，仅作备料估算。
        narration_seconds: 已生成口播的实测时长，优先于目标时长。

    Returns:
        None: 报告错误和容量估算，不修改文件或进行慢放补齐。
    """
    segments = check_editorial_content(content, report)
    active = isinstance(content.get("editorial"), dict) and content["editorial"].get("prepared_for") == EDITORIAL_LAYOUT
    declared = {entry["file"]: (path, entry) for path, entry in resolved}
    durations: dict[str, float | None] = {}
    matched: set[str] = set()
    capacity = 0.0
    foreground_count = 0
    for path, entry in resolved:
        name = entry["file"]
        if entry.get("editorial") is None:
            if active:
                report.error(f"{name} 缺少 editorial.role，请按真实素材填写 footage、foreground 或 reference_image，并关联原文段落")
            continue
        if content.get("editorial") is None:
            report.error(f"{name} 声明了 editorial，但 content.editorial 缺失")
        metadata = _editorial_object(entry["editorial"], {"role", "subject", "segment_ids", "title_candidate", "derived_from", "focus"}, f"{name}.editorial", report)
        role = metadata.get("role")
        if role not in ("footage", "foreground", "reference_image"):
            report.error(f"{name}.editorial.role 必须是 footage、foreground 或 reference_image")
        if metadata.get("subject") is not None and (not isinstance(metadata["subject"], str) or len(metadata["subject"]) > EDITORIAL_TEXT_CHARS):
            report.error(f"{name}.editorial.subject 必须是最多 {EDITORIAL_TEXT_CHARS} 字符的字符串")
        if "title_candidate" in metadata and not isinstance(metadata["title_candidate"], bool):
            report.error(f"{name}.editorial.title_candidate 必须是布尔值")
        identifiers = metadata.get("segment_ids", [])
        if not isinstance(identifiers, list) or any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
            report.error(f"{name}.editorial.segment_ids 必须是非空字符串组成的数组")
            identifiers = []
        if len(set(identifiers)) != len(identifiers):
            report.error(f"{name}.editorial.segment_ids 不能重复")
        if len(identifiers) > EDITORIAL_MAX_SEGMENTS:
            report.error(f"{name}.editorial.segment_ids 超过 {EDITORIAL_MAX_SEGMENTS} 项")
        for identifier in identifiers:
            if identifier not in segments:
                report.error(f"{name} 关联了不存在的段落 {identifier!r}")
        anchor = entry.get("script_anchor")
        if active and anchor and (not isinstance(anchor, str) or anchor not in content.get("script", "")):
            report.error(f"{name}.script_anchor 必须从定稿 script 原样摘取")
        if metadata.get("focus") is not None:
            focus = _editorial_object(metadata["focus"], {"x", "y", "width", "height"}, f"{name}.editorial.focus", report)
            if any(not _finite_number(focus.get(key)) or not 0 <= focus[key] <= 1 for key in ("x", "y", "width", "height")):
                report.error(f"{name}.editorial.focus 四项必须是 0 到 1 的有限数字")
            elif focus["width"] <= 0 or focus["height"] <= 0 or focus["x"] + focus["width"] > 1 or focus["y"] + focus["height"] > 1:
                report.error(f"{name}.editorial.focus 必须有正面积且位于画面内")
        if role == "footage":
            if entry["kind"] != "video":
                report.error(f"{name} 的 footage 必须是视频，不能用图片作叙事底片")
                continue
            durations[name] = probe_duration(path)
            duration = durations[name]
            if duration is None or not _finite_number(duration):
                report.error(f"{name} 有效时长未验收：ffprobe 不可用或视频无法读取")
                continue
            start = entry.get("clip_start_seconds") or 0
            end = entry.get("clip_end_seconds") if entry.get("clip_end_seconds") is not None else duration
            if not _finite_number(start) or not _finite_number(end) or not 0 <= start < end <= duration + 0.1:
                report.error(f"{name} 的视频选段必须落在实际时长 {duration:.2f} 秒内")
                continue
            capacity += min(end, duration) - start
            matched.update(identifiers)
        if role == "reference_image":
            if entry["kind"] != "image":
                report.error(f"{name} 的 reference_image 必须是图片")
            if metadata.get("derived_from") is not None:
                report.error(f"{name} 的 reference_image 不声明视频派生关系")
        if role == "foreground":
            if entry["kind"] != "image" or path.suffix.lower() != ".png":
                report.error(f"{name} 的 foreground 必须是带透明通道的 PNG")
                continue
            foreground_count += 1
            check_foreground_alpha(path, report)
        if role == "foreground" or metadata.get("derived_from") is not None:
            derived = _editorial_object(metadata.get("derived_from"), {"file", "at_seconds"}, f"{name}.editorial.derived_from", report)
            source_name, second = derived.get("file"), derived.get("at_seconds")
            if isinstance(source_name, str) and (len(source_name) > EDITORIAL_PATH_CHARS or source_name != source_name.strip()
                or "\\" in source_name or ":" in source_name or "\x00" in source_name or ".." in PurePosixPath(source_name).parts
                or PurePosixPath(source_name).is_absolute() or PurePosixPath(source_name).as_posix() != source_name or source_name == "."):
                report.error(f"{name}.derived_from.file 必须是安全的原样 POSIX 相对路径")
            source = declared.get(source_name) if isinstance(source_name, str) else None
            if source is None or source[1]["kind"] != "video":
                report.error(f"{name}.derived_from.file 必须精确指向包内已声明的视频")
            elif not _finite_number(second) or second < 0:
                report.error(f"{name}.derived_from.at_seconds 必须是非负有限秒数")
            else:
                if source_name not in durations:
                    durations[source_name] = probe_duration(source[0])
                duration = durations[source_name]
                if duration is None or not _finite_number(duration):
                    report.error(f"{name} 派生来源时间未验收：无法读取源视频时长")
                elif second >= duration:
                    report.error(f"{name} 抽帧时间 {second:g} 秒超出源视频 {duration:.2f} 秒")
    if not active:
        return
    if foreground_count == 0:
        report.error("新版横屏至少需要一张从包内视频抽帧制作的透明 PNG 前景")
    for identifier, segment in segments.items():
        if segment.get("role") != "title" and identifier not in matched:
            report.error(f"段落 {identifier!r} 没有关联有效视频，请按该段 visual_goal 补素材")
    expected = narration_seconds if narration_seconds is not None else target_seconds
    if expected is None:
        report.error(f"新版横屏可用视频合计 {capacity:.1f} 秒；缺少目标或实测口播时长，90% 视频覆盖未验收。请加 --target-seconds 或 --narration-seconds")
    else:
        required = expected * VIDEO_COVERAGE_RATIO
        basis = "实测口播" if narration_seconds is not None else "用户目标（非真实口播时长）"
        if capacity < required:
            report.error(f"新版横屏视频不足：{basis} {expected:.1f} 秒需至少 {required:.1f} 秒视频，可用 {capacity:.1f} 秒，缺 {required - capacity:.1f} 秒；请补素材，不得靠慢放或冻结补齐")
        else:
            report.warn(f"容量估算：{basis} {expected:.1f} 秒，可用视频 {capacity:.1f} 秒，达到 90% 总量；仍需人工核对逐段余量、重复镜头及前景轮廓，实际成片占比由编排后验收")


def _footage_source_hash(path: Path) -> str:
    """按文件内容识别换名复制的原视频。

    Args:
        path: 包内已解析的视频路径。

    Returns:
        str: 原视频的 SHA-256 摘要。

    Raises:
        OSError: 文件无法读取时。
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(FOOTAGE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _interval_capacity(windows: list[tuple[float, float]]) -> float:
    """合并同源选段，仅计算不重复的时间区间。

    Args:
        windows: 同一源文件的选段起止秒数。

    Returns:
        float: 时间区间并集的秒数。
    """
    capacity = end = 0.0
    for start, next_end in sorted(windows):
        capacity += max(0.0, next_end - max(start, end))
        end = max(end, next_end)
    return capacity


def check_footage_bundle(
    content: dict[str, Any],
    resolved: list[tuple[Path, dict[str, Any]]],
    report: Report,
    target_seconds: float | None,
    narration_seconds: float | None,
) -> None:
    """核对纯视频备料的一倍半总余量及按字数估算的逐句覆盖。

    Args:
        content: 已确认的口播内容。
        resolved: 已声明且能在包内找到的素材。
        report: 校验结果。
        target_seconds: 用户确认的目标时长。
        narration_seconds: 实测口播时长，优先于目标值。

    Returns:
        None: 错误直接加入报告。
    """
    if content.get("editorial") is not None:
        report.error("横屏纯视频版不能声明动效版的 editorial 备料")
    if content.get("hook_headline") is not None:
        report.error("横屏纯视频版没有钩子标题带，hook_headline 必须为空")
    script = content.get("script") if isinstance(content.get("script"), str) else ""
    sentences: dict[str, int] = {}
    for match in FOOTAGE_SENTENCE_PATTERN.finditer(script):
        sentence = match.group().strip()
        if sentence:
            sentences[sentence] = sentences.get(sentence, 0) + 1
    sentence_clips: dict[str, list[float]] = {sentence: [] for sentence in sentences}
    source_windows: dict[str, list[tuple[float, float]]] = {}
    videos = 0
    for path, entry in resolved:
        name = entry["file"]
        if entry["kind"] != "video":
            report.error(f"横屏纯视频版不能包含图片: {name}")
            continue
        videos += 1
        if entry.get("editorial") is not None:
            report.error(f"{name} 不能带动效版前景或图解声明")
        anchor = entry.get("script_anchor")
        if not isinstance(anchor, str) or not anchor.strip() or anchor not in script:
            report.error(f"{name}.script_anchor 必须从定稿口播原样摘取")
            continue
        if anchor.strip() not in sentences:
            report.error(f"{name}.script_anchor 必须对应完整一句口播，不能截断半句或把多句合为一个锚点")
            continue
        duration = probe_duration(path)
        if duration is None or not _finite_number(duration):
            report.error(f"{name} 无法验收有效视频时长")
            continue
        start = entry.get("clip_start_seconds")
        end = entry.get("clip_end_seconds")
        start = 0.0 if start is None else start
        end = duration if end is None else end
        if not _finite_number(start) or not _finite_number(end) or not 0 <= start < end <= duration + 0.1:
            report.error(f"{name} 的选段必须落在实际时长 {duration:.2f} 秒内")
            continue
        end = min(end, duration)
        try:
            source_hash = _footage_source_hash(path)
        except OSError as error:
            report.error(f"{name} 无法核对重复来源: {error}")
            continue
        windows = source_windows.setdefault(source_hash, [])
        before = _interval_capacity(windows)
        windows.append((start, end))
        unique_seconds = _interval_capacity(windows) - before
        if not math.isclose(unique_seconds, end - start):
            report.error(f"{name} 与同源视频选段重叠，重复部分不计入有效容量，请更换或重新裁剪")
        sentence_clips[anchor.strip()].append(unique_seconds)
    if not videos:
        report.error("横屏纯视频版至少需要一段真实视频")
    expected = narration_seconds if narration_seconds is not None else target_seconds
    if expected is None:
        report.error("横屏纯视频版必须提供 --target-seconds 或 --narration-seconds 才能验收视频容量")
        return
    capacity = sum(_interval_capacity(windows) for windows in source_windows.values())
    required = math.ceil(expected * FOOTAGE_RESERVE_RATIO / FOOTAGE_CAPACITY_ROUND_SECONDS) * FOOTAGE_CAPACITY_ROUND_SECONDS
    basis = "实测口播" if narration_seconds is not None else "目标口播"
    if capacity < required:
        report.error(f"横屏纯视频版视频不足：{basis} {expected:.1f} 秒，按 1.5 倍向上取整到十秒需 {required:.1f} 秒，可用 {capacity:.1f} 秒，缺 {required - capacity:.1f} 秒；请补充真实镜头")
    else:
        report.warn(f"纯视频有效备料合计 {capacity:.1f} 秒，达到含冗余的 {required:.1f} 秒门槛；仍需人工检查画面内容、重复镜头与实际配音后每镜时长")
    # 总时长只作估算依据；每句单独分配素材，不能把同一长片反复计给多个句子。
    total_chars = sum(len(sentence) * count for sentence, count in sentences.items())
    for sentence, count in sentences.items():
        estimated = expected * len(sentence) / total_chars
        clips = sentence_clips[sentence]
        sentence_required = estimated * count * FOOTAGE_RESERVE_RATIO
        if sum(clips) < sentence_required:
            report.error(f"口播 {sentence!r} 对应视频不足：按字数估算共 {estimated * count:.2f} 秒，含 50% 余量需 {sentence_required:.2f} 秒，可用 {sum(clips):.2f} 秒")
        if sum(seconds >= estimated for seconds in clips) < count:
            report.error(f"口播 {sentence!r} 缺少独立主镜头：每次出现至少一段完整视频能覆盖估算的 {estimated:.2f} 秒；不能仅靠多个短片的总和放行")


def main(argv: list[str]) -> int:
    """校验入口，有错误时返回非零退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir")
    parser.add_argument("--target-seconds", type=float, help="用户确认的目标秒数，仅用于备料估算")
    parser.add_argument("--narration-seconds", type=float, help="已生成口播的实测秒数")
    parser.add_argument("--layout", choices=sorted(LANDSCAPE_LAYOUTS), help="指定横屏版面并验收视频方向")
    parser.add_argument("--json", action="store_true", help="输出供外部 Agent 修复与复检的结构化报告")
    arguments = parser.parse_args(argv[1:])
    for value in (arguments.target_seconds, arguments.narration_seconds):
        if value is not None and (not _finite_number(value) or value <= 0):
            parser.error("时长必须是正的有限数字")
    bundle_dir = Path(arguments.bundle_dir).expanduser().resolve()
    if not bundle_dir.is_dir():
        print(f"素材包目录不存在: {bundle_dir}")
        return 2

    report = Report()
    content = load_json(bundle_dir / "content.json", report)
    manifest = load_json(bundle_dir / "manifest.json", report)
    if content is not None and manifest is not None:
        for error in validate_protocol({"content": content, "manifest": manifest}):
            match = re.search(r"manifest\.assets\[(\d+)\]", error)
            if match and isinstance(manifest.get("assets"), list):
                entry = manifest["assets"][int(match.group(1))]
                if isinstance(entry, dict):
                    error += f"（素材 {entry.get('file', '未知')}）"
            report.error(error)
    # 类型不合法时不要继续执行面向正确类型的媒体与业务检查。
    if report.errors:
        return print_report(report, arguments.json)
    if content is not None:
        validate_content(content, report)

    declared_layout = None
    if content is not None:
        declared_layout = content.get("prepared_for")
        if isinstance(content.get("editorial"), dict):
            declared_layout = content["editorial"].get("prepared_for")
    if arguments.layout and declared_layout and arguments.layout != declared_layout:
        report.error(f"--layout={arguments.layout} 与 content.json 的备料目标 {declared_layout} 不一致")
    selected_layout = arguments.layout or (declared_layout if isinstance(declared_layout, str) else None)

    resolved: list[tuple[Path, dict[str, Any]]] = []
    if manifest is not None:
        resolved = validate_manifest(manifest, bundle_dir, report)
        check_videos(resolved, report)
        if selected_layout in LANDSCAPE_LAYOUTS:
            check_landscape_video_orientation(resolved, selected_layout, report)
    if content is not None:
        check_editorial_bundle(content, resolved, report, arguments.target_seconds, arguments.narration_seconds)
        if selected_layout == FOOTAGE_LAYOUT:
            check_footage_bundle(content, resolved, report, arguments.target_seconds, arguments.narration_seconds)
    check_editorial_upload_limits(bundle_dir, report)
    check_orphans(bundle_dir, resolved, report,
                  editorial_mode=bool(selected_layout == FOOTAGE_LAYOUT or (content and content.get("editorial") is not None)))
    check_foreign_declarations(bundle_dir, report)

    if arguments.json:
        return print_report(report, True)
    images, videos = count_kinds(resolved)
    print(f"素材包: {bundle_dir}")
    if content is not None and isinstance(content.get("title"), str):
        print(f"标题: {content['title']}")
    print(f"已声明素材: {images} 张图片, {videos} 段视频")
    if content is not None and isinstance(content.get("script"), str):
        script = content["script"]
        print(f"口播文案: {len(script)} 字；字数不等于真实口播时长")

    return print_report(report, False)


def print_report(report: Report, as_json: bool) -> int:
    """Failure means repair and retry by the calling Agent, never deliver this pack."""
    if as_json:
        print(json.dumps({
            "status": "repair_required" if report.errors else "passed",
            "errors": [{"code": "bundle_validation_error", "message": error} for error in report.errors],
            "warnings": report.warnings,
            "next_action": "外部 Agent 自主修复上述文件/字段，使用相同版面和时长重新校验；通过前不得交付" if report.errors else "使用 pack_bundle.py 生成并复验最终 ZIP",
        }, ensure_ascii=False))
    else:
        for message in report.warnings:
            print(f"[告警] {message}")
        for message in report.errors:
            print(f"[错误] {message}")
        print("\n校验未通过：外部 Agent 应自主修复并复检，不交付错误包。" if report.errors else "\n校验通过。")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
