#!/usr/bin/env python3
"""素材包打包前的本地自校验，只用标准库，可在任意目录下运行。

校验规则与 Niwo 服务端的严格协议对齐：报错的项服务端一定会拒收，告警的项服务端会
容忍但成片质量可能受影响。视频时长与文件真实格式的检查需要 ffprobe，缺失时跳过。

用法:
    python3 validate_bundle.py <素材包目录>
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CONTENT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

PINYIN_PATTERN = re.compile(r"^[a-z]+[1-5]$")
HEADLINE_HIGHLIGHT_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")

MAX_TITLE_CHARS = 120
MAX_SCRIPT_CHARS = 6000
MAX_NOTES_CHARS = 2000
MAX_HEADLINE_LINES = 3
# 标题带的容量是宽度而非字数：中日韩字符占满一个字宽，英文数字只占 0.56。
NARROW_CHAR_WIDTH = 0.56
MAX_HEADLINE_LINE_WIDTH = 14.0
WARN_HEADLINE_LINE_WIDTH = 12.0
MAX_HIGHLIGHTS_PER_LINE = 2
MAX_SOURCE_ENTRY_WIDTH = 24.0
MAX_SUMMARY_CHARS = 240
MAX_TAG_CHARS = 32
MAX_TAGS = 8
MAX_ENTRIES = 200
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024

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
        "tts",
        "bgm",
        "bgm_mood",
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
    """收集校验结果，区分服务端会拒收的错误与仅影响质量的告警。"""

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


def check_hook_headline(value: Any, report: Report) -> None:
    """校验顶部钩子大标题的行数、高亮标记与单行长度。"""
    if not isinstance(value, dict):
        report.error("hook_headline 必须是对象")
        return
    for key in sorted(set(value) - {"lines"}):
        report.error(f"hook_headline 出现未知字段 {key!r}")
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

    if "hook_headline" in content:
        check_hook_headline(content["hook_headline"], report)

    if "sources" in content:
        check_sources(content["sources"], report)

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

        path = bundle_dir / cleaned
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
        report.warn(f"{label} 有 {len(tags)} 个 tags，超过 {MAX_TAGS} 的会被丢弃")
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            report.error(f"{label} 的 tags 含空标签")
        elif len(tag) > MAX_TAG_CHARS:
            report.warn(f"{label} 的标签 {tag!r} 超过 {MAX_TAG_CHARS} 字，会被截断")


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


def check_videos(resolved: list[tuple[Path, dict[str, Any]]], report: Report) -> None:
    """核对视频可解码，且选段窗口落在真实时长内。"""
    videos = [(path, entry) for path, entry in resolved if entry["kind"] == "video"]
    if not videos:
        return
    if shutil.which("ffprobe") is None:
        report.warn("环境里没有 ffprobe，跳过视频时长与选段窗口检查")
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
    bundle_dir: Path, resolved: list[tuple[Path, dict[str, Any]]], report: Report
) -> None:
    """列出磁盘上没写进 manifest 的素材，它们仍然可用但会走视觉打标。"""
    declared = {path.resolve() for path, _ in resolved}
    for subdir in ("images", "videos"):
        directory = bundle_dir / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                report.warn(f"{subdir}/{path.name} 不是支持的素材格式，会被忽略")
            elif path.resolve() not in declared:
                report.warn(
                    f"{subdir}/{path.name} 没写进 manifest，"
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


def main(argv: list[str]) -> int:
    """校验入口，有错误时返回非零退出码。"""
    if len(argv) != 2:
        print(__doc__)
        return 2
    bundle_dir = Path(argv[1]).expanduser().resolve()
    if not bundle_dir.is_dir():
        print(f"素材包目录不存在: {bundle_dir}")
        return 2

    report = Report()
    content = load_json(bundle_dir / "content.json", report)
    if content is not None:
        validate_content(content, report)

    resolved: list[tuple[Path, dict[str, Any]]] = []
    manifest = load_json(bundle_dir / "manifest.json", report)
    if manifest is not None:
        resolved = validate_manifest(manifest, bundle_dir, report)
        check_videos(resolved, report)
    check_orphans(bundle_dir, resolved, report)
    check_foreign_declarations(bundle_dir, report)

    images, videos = count_kinds(resolved)
    print(f"素材包: {bundle_dir}")
    if content is not None and isinstance(content.get("title"), str):
        print(f"标题: {content['title']}")
    print(f"已声明素材: {images} 张图片, {videos} 段视频")
    if content is not None and isinstance(content.get("script"), str):
        script = content["script"]
        print(f"口播文案: {len(script)} 字, 预计成片约 {len(script) / 6:.0f} 秒")

    for message in report.warnings:
        print(f"[告警] {message}")
    for message in report.errors:
        print(f"[错误] {message}")

    if report.errors:
        print(f"\n校验未通过: {len(report.errors)} 个错误，改完再打包。")
        return 1
    if report.warnings:
        print(f"\n校验通过，有 {len(report.warnings)} 条告警可以自行判断。")
    else:
        print("\n校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
