#!/usr/bin/env python3
"""检查这份 skill 有没有新版，必要时把本地这份就地更新到最新。只用标准库。

版本清单是公开仓库里的 skills/niwo-render-everything/version.json，本地同名文件是
安装时那一版。网络不通就静默放弃，这一步永远不该挡住正在进行的任务。

用法:
    python3 skill_update.py           # 只检查，不动任何文件
    python3 skill_update.py --apply   # 下载最新版覆盖本地 skill 目录

退出码:
    0   已是最新（--apply 时表示更新成功）
    10  有新版，可选更新
    20  本地版本低于 min_compatible，协议已不兼容，必须更新
    3   没检查成功（离线、超时、被挡），当作最新继续
    2   本地文件或用法有问题
    1   --apply 失败
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = "MontageAI/niwo-render-everything"
BRANCH = "main"
SKILL_NAME = "niwo-render-everything"
SKILL_PATH = f"skills/{SKILL_NAME}"

# 版本清单的镜像，按顺序试，第一个成功的说了算。raw 排前面是为了拿到刚推上去的版本，
# jsDelivr 有最长 12 小时缓存但国内连得上，正好互补。urllib 会自动走 HTTPS_PROXY。
VERSION_URLS = (
    f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SKILL_PATH}/version.json",
    f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{SKILL_PATH}/version.json",
)
TARBALL_URLS = (
    f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}",
    f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.tar.gz",
)
HTTP_TIMEOUT = 5
TARBALL_TIMEOUT = 60
UNKNOWN_VERSION = (0, 0, 0)


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_version(value: Any) -> tuple[int, ...] | None:
    """把 1.2.3 解析成可比较的元组，非数字段一律当作无法比较。"""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if not 2 <= len(parts) <= 4 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def load_local() -> dict[str, Any]:
    path = skill_dir() / "version.json"
    if not path.is_file():
        # 带版本号之前装的老包没有这个文件，按最旧处理，好把用户推去更新。
        return {"version": "0.0.0"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"本地 version.json 读不了: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def fetch(url: str, timeout: int) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": f"{SKILL_NAME}-updater"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def fetch_remote() -> dict[str, Any] | None:
    for url in VERSION_URLS:
        raw = fetch(url, HTTP_TIMEOUT)
        if raw is None:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and parse_version(data.get("version")):
            return data
    return None


def print_highlights(remote: dict[str, Any]) -> None:
    highlights = remote.get("highlights")
    if isinstance(highlights, list):
        for item in highlights[:5]:
            if isinstance(item, str) and item.strip():
                print(f"  - {item.strip()}")
    changelog = remote.get("changelog")
    if isinstance(changelog, str) and changelog.strip():
        print(f"完整改动: {changelog.strip()}")


def print_commands() -> None:
    print(f"更新命令: npx skills update {SKILL_NAME} -y")
    print(f"     或者: python3 {Path(__file__).resolve()} --apply")


def check() -> int:
    local = load_local()
    local_version = parse_version(local.get("version")) or UNKNOWN_VERSION
    remote = fetch_remote()
    if remote is None:
        print("没能连上版本清单，跳过更新检查，按当前这版继续。")
        return 3

    remote_version = parse_version(remote.get("version")) or UNKNOWN_VERSION
    label = f"{SKILL_NAME} 本地 {local.get('version', '未知')} / 最新 {remote['version']}"

    if local_version >= remote_version:
        print(f"{label}，已是最新。")
        return 0

    floor = parse_version(remote.get("min_compatible"))
    if floor is not None and local_version < floor:
        print(f"{label}，本地版本已低于最低兼容版本 {remote['min_compatible']}，必须先更新。")
        print_highlights(remote)
        print_commands()
        return 20

    print(f"{label}，有新版可以更新。")
    print_highlights(remote)
    print_commands()
    return 10


def download_tarball() -> bytes | None:
    for url in TARBALL_URLS:
        raw = fetch(url, TARBALL_TIMEOUT)
        if raw:
            return raw
    return None


def extract_skill(archive: bytes, staging: Path) -> bool:
    """把压缩包里 skill 目录那部分解到 staging，路径不安全的成员直接跳过。"""
    found = False
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            # 归档顶层是 <repo>-<branch>/，剥掉它再匹配 skills/<name>/
            if len(parts) < 4 or parts[1:3] != ("skills", SKILL_NAME):
                continue
            relative = Path(*parts[3:])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            found = True
    return found


def sync_into(staging: Path, destination: Path) -> None:
    """用新版覆盖旧版，并删掉新版里已经不存在的文件。"""
    incoming = {path.relative_to(staging) for path in staging.rglob("*") if path.is_file()}
    for relative in sorted(incoming):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / relative, target)
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(destination) not in incoming:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def apply() -> int:
    destination = skill_dir()
    if not (destination / "SKILL.md").is_file():
        print(f"{destination} 里没有 SKILL.md，不像 skill 目录，不敢往里写。")
        return 2
    # 源仓库里这份是发布的上游，拿发布版覆盖它等于把还没同步的改动冲掉。
    if (destination.parent.parent / ".github/workflows/sync-skill.yml").is_file():
        print(f"{destination} 是 skill 的源仓库，不能用发布版覆盖，改动请直接在这里提交。")
        return 2

    archive = download_tarball()
    if archive is None:
        print("下载最新版失败。挂上代理重试，或者手动跑 npx skills update。")
        return 1

    staging = destination.parent / f".{SKILL_NAME}-update"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        if not extract_skill(archive, staging) or not (staging / "SKILL.md").is_file():
            print("压缩包里没找到完整的 skill 目录，本地文件没动。")
            return 1
        # 解压后立刻同步，中途失败会留下半新半旧的目录，所以让用户重跑而不是自己回滚。
        sync_into(staging, destination)
    except OSError as exc:
        print(f"写入失败: {exc}。本地目录可能不完整，重跑一次 --apply。")
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    version = load_local().get("version", "未知")
    print(f"已更新到 {version}，重新读一遍 {destination / 'SKILL.md'} 再继续。")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        return check()
    if len(argv) == 2 and argv[1] == "--apply":
        return apply()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
