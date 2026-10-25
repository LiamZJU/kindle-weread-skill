#!/usr/bin/env python3
"""Kindle mount detection and package installer for kindle-weread-skill.

Standard library, with optional paramiko for Wi-Fi uploads when OpenSSH is absent.
Python 3.9+. Logs go to stderr. Use --json for the machine-readable result on stdout.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import inspect
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    import paramiko
except ImportError:
    paramiko = None


USER_AGENT = "kindle-weread-skill/1.0"
HF_CUTOFF = "5.16.3"
ADBREAK_MIN = "5.18.1"
LANGUAGEBREAK_MAX = "5.16.2.1.1"
WINTERBREAK2_MAX = "5.16.4"
REFERENCE_FIRMWARE = "5.16.2.1.1"
SSH_PORT = 2222
SSH_USER = "root"
REMOTE_PLUGIN_PARENT = "/mnt/us/koreader/plugins"
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Shown after a wireless step. Install never writes SSH_autostart = true.
SSH_OFF_NOTE = "用完后请回到顶部菜单 → 网络 → SSH 服务器，取消勾选 SSH 服务器。"
JAILBREAK_ACK_FLAG = "--i-accept-jailbreak-risk"

# Every file written to the Kindle is pinned in scripts/pins.json.
PIN_KEYS = (
    "winterbreak",
    "winterbreak2",
    "languagebreak",
    "adbreak",
    "hotfix",
    "mrpi-coplate",
    "mrpi-hardfloat",
    "kual-coplate",
    "kual-hardfloat",
    "peki",
    "koreader-kindlepw2",
    "koreader-kindlehf",
    "weread",
    "miuread",
    "simpleui",
)
KUAL_PIN_KEYS = {
    "coplate": ("mrpi-coplate", "kual-coplate"),
    "hardfloat": ("mrpi-hardfloat", "kual-hardfloat"),
}

SERIAL_PREFIXES = {
    "B00E": "Kindle Paperwhite 1 (2012) WiFi",
    "B00F": "Kindle Paperwhite 1 (2012) 3G",
    "B011": "Kindle Paperwhite 2 (2013) WiFi",
    "B017": "Kindle Paperwhite 2 (2013) 3G",
    "B01F": "Kindle Voyage (2014) WiFi",
    "B020": "Kindle Voyage (2014) 3G",
    "B024": "Kindle 7 (2014) WiFi",
    "B0D4": "Kindle Oasis (2016) WiFi",
    "G090": "Kindle Paperwhite 3 (2015, 7th gen, KP3) WiFi",
    "G091": "Kindle Paperwhite 3 (2015, 7th gen, KP3) 3G",
    "G0B0": "Kindle 8 (2016) WiFi",
    "G0D4": "Kindle Oasis 2 (2017) WiFi",
    "G0D5": "Kindle Oasis 2 (2017) 3G",
}

PLUGIN_DIRS = {
    "weread": "weread.koplugin",
    "miuread": "miuread.koplugin",
}
SIMPLEUI_DIR = "simpleui.koplugin"
SIMPLEUI_ASSET = "simpleui.koplugin.zip"
WEKINDLE_DIR = "wekindle.koplugin"
ONBOARDING_SKIP_LUA = """-- kindle-weread-skill: a fresh install skips the layout picker
return {
    ["simpleui_onboarding_done"] = true,
}
"""
SIMPLEUI_MIN_KOREADER = "2026.03"
# reader.lua opens the quickstart guide and ignores start_with until this is set.
# 202607010000 is KOReader v2026.07.1; the guide stays hidden at or above 2021070000.
KOREADER_QUICKSTART_SHOWN = "202607010000"
REMOTE_SUI_SETTINGS = "/mnt/us/koreader/settings/simpleui/sui_settings.lua"


class SafetyError(Exception):
    """Raised when a write would be unsafe or the requested package does not match the device."""


class NetworkError(Exception):
    """Raised when a download or GitHub API call fails after retries."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def version_parts(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def version_cmp(left: str, right: str) -> int:
    aa = version_parts(left)
    bb = version_parts(right)
    width = max(len(aa), len(bb))
    aa = aa + (0,) * (width - len(aa))
    bb = bb + (0,) * (width - len(bb))
    if aa < bb:
        return -1
    if aa > bb:
        return 1
    return 0


def version_lt(left: str, right: str) -> bool:
    return version_cmp(left, right) < 0


def version_ge(left: str, right: str) -> bool:
    return version_cmp(left, right) >= 0


def route_firmware(firmware: str) -> dict[str, str]:
    if version_lt(firmware, HF_CUTOFF):
        return {
            "architecture": "legacy_softfloat",
            "koreader_arch": "kindlepw2",
            "kual_variant": "coplate",
        }
    return {
        "architecture": "kindlehf",
        "koreader_arch": "kindlehf",
        "kual_variant": "hardfloat",
    }


def require_jailbreak_acknowledgement(accepted: bool) -> None:
    """Block install-jailbreak until the operator accepts warranty loss, brick risk, and recovery."""
    if accepted:
        return
    raise SafetyError(
        "越狱会让这台 Kindle 失去亚马逊保修。失败时可能变砖：开不了机，或者卡在更新失败。"
        "恢复方法：先把 documents 文件夹复制到电脑；设备还能开机时，按 "
        "https://kindlemodding.org/jailbreaking/unjailbreaking.html 退出越狱并装回官方固件；"
        "若卡在 Update Failed，按 https://kindlemodding.org/jailbreaking/recovering-from-a-reset.html "
        "把对应机型的官方固件 .bin 放到 Kindle 根目录后重启；"
        "AdBreak 改过的广告文件可以用 restore-assets 还原；"
        "电脑完全认不出磁盘时，这个工具修不好。"
        f"确认这些之后，重新运行并加上 {JAILBREAK_ACK_FLAG}。"
    )


def recommend_method(firmware: str) -> str:
    if version_ge(firmware, ADBREAK_MIN):
        return "adbreak"
    return "winterbreak"


def parse_firmware(text: str) -> str | None:
    found = re.findall(r"\d+\.\d+\.\d+(?:\.\d+)*", text)
    for item in found:
        if item.startswith("5."):
            return item
    if found:
        return found[0]
    return None


def default_cache() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "kindle-weread-skill"


def clean_mirror(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        raise SafetyError("镜像前缀必须以 http:// 或 https:// 开头。")
    return text if text.endswith("/") else text + "/"


def mirror_candidates(url: str, mirror: str | None) -> list[str]:
    if mirror and url.startswith(("http://", "https://")):
        return [url, mirror + url]
    return [url]


def should_retry(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def download_file(url: str, dest: Path, mirror: str | None, timeout: int = 180) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    merged = {"User-Agent": USER_AGENT}
    last_error: Exception | None = None
    for candidate in mirror_candidates(url, mirror):
        for attempt in range(3):
            try:
                log(f"下载 {candidate}")
                request = urllib.request.Request(candidate, headers=merged)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    written = 0
                    next_report = 1024 * 1024
                    with partial.open("wb") as handle:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            written += len(chunk)
                            if written >= next_report:
                                log(f"  已接收 {written // (1024 * 1024)} MB")
                                next_report += 1024 * 1024
                partial.replace(dest)
                log(f"已保存 {dest.name} ({written} bytes)")
                return dest
            except Exception as exc:
                last_error = exc
                if partial.exists():
                    partial.unlink()
                if not should_retry(exc):
                    break
                delay = 2 ** attempt
                log(f"下载失败，{delay} 秒后重试 ({attempt + 1}/3): {exc}")
                time.sleep(delay)
    raise NetworkError(f"下载失败: {url} ({last_error})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pins_path() -> Path:
    return Path(__file__).resolve().parent / "pins.json"


def _validate_pin(key: str, item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        raise SafetyError(f"下载清单里的 {key} 不是对象。")
    version = str(item.get("version") or "").strip()
    filename = str(item.get("filename") or "").strip()
    url = str(item.get("url") or "").strip()
    digest = str(item.get("sha256") or "").strip().lower()
    if not version or not filename or not url.startswith("https://"):
        raise SafetyError(f"下载清单里的 {key} 缺少 version、filename 或 https URL。")
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise SafetyError(f"下载清单里的 {key} 文件名不安全。")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SafetyError(f"下载清单里的 {key} SHA-256 无效。")
    return {"version": version, "filename": filename, "url": url, "sha256": digest}


def load_pins() -> dict[str, dict[str, str]]:
    path = pins_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError(f"无法读取下载清单 {path.name}。") from exc
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, dict):
        raise SafetyError("下载清单缺少 packages。")
    loaded = {str(key): _validate_pin(str(key), item) for key, item in packages.items()}
    missing = [key for key in PIN_KEYS if key not in loaded]
    if missing:
        raise SafetyError("下载清单缺少: " + ", ".join(missing))
    return loaded


def pin_for(key: str) -> dict[str, str]:
    pins = load_pins()
    if key not in pins:
        raise SafetyError(f"下载清单里没有 {key}。")
    return pins[key]


def verify_pinned_file(path: Path, expected: str, label: str) -> None:
    """Reject a file whose SHA-256 is not the pinned digest. Deletes it on mismatch."""
    if not path.is_file():
        raise SafetyError(f"缺少 {label}，已停止，没有写入 Kindle。")
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        path.unlink(missing_ok=True)
        raise SafetyError(
            f"{label} 的 SHA-256 与下载清单不一致，已删除该文件并停止，没有写入 Kindle。"
            f" 期望 {expected.lower()}，实际 {actual.lower()}。"
        )


def _cached_pin_matches(path: Path, expected: str) -> bool:
    return path.is_file() and sha256_file(path).lower() == expected.lower()


def download_pinned(key: str, cache: Path, mirror: str | None) -> tuple[dict[str, str], Path]:
    """Download one pinned file. Mirror bytes are hashed the same way and rejected on mismatch."""
    pin = pin_for(key)
    dest = cache / "downloads" / pin["filename"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _cached_pin_matches(dest, pin["sha256"]):
        log(f"使用已校验的缓存 {dest.name} ({pin['version']})")
        return pin, dest
    if dest.exists():
        log(f"缓存 {dest.name} 与清单不一致，重新下载")
        dest.unlink()
    network_errors: list[str] = []
    saw_mismatch = False
    for url in mirror_candidates(pin["url"], mirror):
        try:
            download_file(url, dest, None)
        except NetworkError as exc:
            network_errors.append(str(exc))
            if dest.exists():
                dest.unlink()
            continue
        if _cached_pin_matches(dest, pin["sha256"]):
            log(f"SHA-256 校验通过: {dest.name} ({pin['version']})")
            return pin, dest
        saw_mismatch = True
        log(f"{pin['filename']} 与清单不一致，已丢弃: {url}")
        if dest.exists():
            dest.unlink()
    if dest.exists():
        dest.unlink()
    if saw_mismatch:
        raise SafetyError(f"{pin['filename']} 的 SHA-256 与下载清单不一致，已停止，没有写入 Kindle。")
    detail = network_errors[-1] if network_errors else "没有可用的下载地址"
    raise NetworkError(f"下载失败: {pin['filename']} ({detail})")


def select_release(releases: Sequence[dict], predicate: Callable[[str], bool]) -> tuple[dict, dict]:
    for release in releases:
        if release.get("draft"):
            continue
        for asset in release.get("assets", []):
            if predicate(str(asset.get("name", ""))):
                return release, asset
    raise NetworkError(
        "没有找到 simpleui.koplugin.zip。GitHub 页面上的 Source code ZIP 不能当安装包。"
    )


def is_simpleui_asset(name: str) -> bool:
    return name == SIMPLEUI_ASSET


def checksum_from_sums(text: str, filename: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        name = parts[-1].lstrip("*")
        if name != filename and not name.endswith("/" + filename):
            continue
        digest = parts[0]
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    return None


def safe_relative(name: str) -> Path | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise SafetyError(f"压缩包包含绝对路径，已拒绝: {name}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SafetyError(f"压缩包包含不安全路径，已拒绝: {name}")
        parts.append(part)
    if not parts:
        return None
    if parts[0] == "__MACOSX":
        return None
    return Path(*parts)


def extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        _extract_zip(archive, dest)
        return
    if name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        _extract_tar(archive, dest)
        return
    raise SafetyError(f"不认识的压缩格式: {archive.name}")


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            relative = safe_relative(info.filename)
            if relative is None:
                continue
            target = dest / relative
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, mode="r:*") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                raise SafetyError(f"压缩包包含链接，已拒绝: {member.name}")
            relative = safe_relative(member.name)
            if relative is None:
                continue
            target = dest / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise SafetyError(f"无法读取压缩包成员: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def merge_tree(source: Path, dest: Path) -> int:
    copied = 0
    dest.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in {"__MACOSX", ".DS_Store"}:
            continue
        if child.is_symlink():
            raise SafetyError(f"拒绝复制符号链接: {child}")
        target = dest / child.name
        if child.is_dir():
            copied += merge_tree(child, target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(child, target)
        except PermissionError as exc:
            raise SafetyError(f"无法写入 {target}: {exc}") from exc
        copied += 1
    return copied


def sync_kindle(kindle: Path) -> None:
    probe = kindle / ".kindle_helper_sync"
    try:
        with probe.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("sync\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if probe.exists():
            probe.unlink()
    if hasattr(os, "sync"):
        os.sync()


def normalize_root(path: Path) -> Path:
    text = str(path)
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", text):
        return Path(text + "\\")
    return path


def _windows_drive_type(root: Path) -> int | None:
    if os.name != "nt":
        return None
    drive = root.drive
    if not drive:
        return None
    return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))


def _suppress_windows_disk_dialogs() -> None:
    """Avoid the 'insert a disk' dialog when probing empty removable drives."""
    if os.name != "nt":
        return
    sem_failcriticalerrors = 0x0001
    sem_noopenfileerrorbox = 0x8000
    ctypes.windll.kernel32.SetErrorMode(sem_failcriticalerrors | sem_noopenfileerrorbox)


def _on_allowed_mount(path: Path) -> bool:
    resolved = str(path.resolve())
    if os.name == "nt":
        system_drive = os.environ.get("SystemDrive", "C:").upper()
        drive = path.drive.upper()
        if not drive or drive == system_drive:
            return False
        drive_type = _windows_drive_type(path)
        # 2 = removable, 3 = fixed. Some Kindles enumerate as fixed disks.
        if drive_type is not None and drive_type not in {2, 3}:
            return False
        return True
    if sys.platform == "darwin":
        blocked = {"Macintosh HD", "Recovery", "Preboot", "com.apple.TimeMachine.localsnapshots"}
        return resolved.startswith("/Volumes/") and path.name not in blocked
    prefixes = ("/media/", "/mnt/", "/run/media/")
    return resolved == "/media" or resolved == "/mnt" or any(resolved.startswith(prefix) for prefix in prefixes)


def _has_os_markers(path: Path) -> bool:
    names = {child.name.lower() for child in _safe_children(path)}
    blocked = {"windows", "program files", "program files (x86)", "users", "$recycle.bin"}
    if names & blocked:
        return True
    resolved = str(path.resolve()).replace("\\", "/").lower()
    if resolved in {"/", "/usr", "/bin", "/etc", "/system", "/system/library"}:
        return True
    return False


def _safe_children(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _has_kindle_marker(path: Path) -> bool:
    markers = [
        path / "system" / "version.txt",
        path / "system" / ".assets",
        path / "documents" / "dictionaries",
        path / "audible",
        path / ".active_content_sandbox",
        path / "extensions",
    ]
    return any(marker.exists() for marker in markers)


def kindle_problem(path: Path, *, _bypass_location: bool = False) -> str | None:
    path = normalize_root(path)
    if not path.exists() or not path.is_dir():
        return f"路径不存在或不是目录: {path}"
    if _has_os_markers(path):
        return f"这个路径像系统盘，已拒绝写入: {path}"
    if not _bypass_location and not _on_allowed_mount(path):
        return f"这个路径不是已挂载的移动磁盘，已拒绝写入: {path}"
    if not (path / "system").is_dir() or not (path / "documents").is_dir():
        return "目录里没有同时存在 system 和 documents，不像 Kindle 磁盘。"
    if not _has_kindle_marker(path):
        return "没有找到 version.txt、dictionaries 或其他 Kindle 标记，已拒绝写入。"
    return None


def assert_kindle_volume(path: Path) -> Path:
    path = normalize_root(path)
    problem = kindle_problem(path)
    if problem:
        raise SafetyError(problem)
    return path.resolve()


def candidate_roots() -> Iterable[Path]:
    if os.name == "nt":
        _suppress_windows_disk_dialogs()
        system_drive = os.environ.get("SystemDrive", "C:").upper()
        present = int(ctypes.windll.kernel32.GetLogicalDrives())
        for index in range(26):
            if not present & (1 << index):
                continue
            letter = chr(ord("A") + index)
            if letter in {"A", "B"} or f"{letter}:" == system_drive:
                continue
            root = Path(f"{letter}:\\")
            # 2 = removable, 3 = fixed. Skip CD/network/RAM drives before touching them.
            if _windows_drive_type(root) not in {2, 3}:
                continue
            yield root
        return
    if sys.platform == "darwin":
        volumes = Path("/Volumes")
        if volumes.is_dir():
            yield from (child for child in _safe_children(volumes) if child.is_dir())
        return
    for base in (Path("/media"), Path("/mnt"), Path("/run/media")):
        if not base.is_dir():
            continue
        yield base
        for child in _safe_children(base):
            if not child.is_dir():
                continue
            yield child
            for nested in _safe_children(child):
                if nested.is_dir():
                    yield nested


def find_kindles() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in candidate_roots():
        try:
            problem = kindle_problem(root)
            if problem is not None:
                if (root / "documents").is_dir() or (root / "system").is_dir():
                    log(f"跳过 {root}: {problem}")
                continue
            resolved = str(root.resolve())
        except OSError as exc:
            log(f"跳过 {root}: {exc}")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(root.resolve())
    return found


def require_kindle(args: argparse.Namespace) -> Path:
    if args.path:
        return assert_kindle_volume(Path(args.path))
    found = find_kindles()
    if len(found) == 1:
        return found[0]
    if not found:
        raise SafetyError("没有找到 Kindle。请确认已经用 USB 连接，或用 --path 指定盘符。")
    joined = ", ".join(str(item) for item in found)
    raise SafetyError(f"找到多个可能的 Kindle: {joined}。请用 --path 指定其中一个。")


def read_firmware(kindle: Path) -> str | None:
    for relative in (Path("system") / "version.txt", Path("system") / "Version.txt"):
        source = kindle / relative
        if not source.is_file():
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        firmware = parse_firmware(text)
        if firmware:
            return firmware
    return None


def firmware_of(kindle: Path, args: argparse.Namespace) -> str | None:
    found = read_firmware(kindle)
    override = getattr(args, "firmware", None)
    if found and override and found != override:
        raise SafetyError(f"盘内固件是 {found}，与传入的 {override} 不一致。")
    return found or override


def require_firmware(kindle: Path, args: argparse.Namespace) -> str:
    firmware = firmware_of(kindle, args)
    if not firmware:
        raise SafetyError(
            "读不到 system/version.txt。请到 Kindle 的设置 → 设备选项 → 设备信息查看固件版本，"
            "然后用 --firmware 传入，例如 --firmware 5.16.2.1.1。"
        )
    return firmware


def scan_serial(kindle: Path) -> str | None:
    pattern = re.compile(r"\b([A-Z0-9]{16})\b")
    ranked: list[str] = []
    system = kindle / "system"
    files: list[Path] = []
    if system.is_dir():
        try:
            for child in system.rglob("*"):
                if len(files) >= 60:
                    break
                if child.is_file() and child.stat().st_size <= 262144:
                    files.append(child)
        except OSError:
            return None
    for source in files:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        for match in pattern.findall(text):
            if match not in ranked:
                ranked.append(match)
    for serial in ranked:
        if serial[:4] in SERIAL_PREFIXES:
            return serial
    return ranked[0] if ranked else None


def kual_present(kindle: Path) -> bool:
    documents = kindle / "documents"
    if (documents / "KUAL.jar").is_file() and (documents / "KUAL.sh").is_file():
        return True
    if documents.is_dir():
        for child in _safe_children(documents):
            if child.is_file() and child.name.lower().startswith("kual") and child.suffix.lower() in {".azw2", ".azw3", ".kual"}:
                return True
    extensions = kindle / "extensions"
    if extensions.is_dir():
        for child in _safe_children(extensions):
            if child.is_dir() and child.name.lower() == "kual":
                return True
    return False


def jailbreak_markers(kindle: Path) -> list[str]:
    markers: list[str] = []
    for name in ("mkk", "extensions", "mrpackages"):
        if (kindle / name).is_dir():
            markers.append(name)
    if (kindle / "extensions" / "MRInstaller").is_dir():
        markers.append("mrinstaller")
    if kual_present(kindle):
        markers.append("kual")
    return markers


def is_jailbroken(markers: Sequence[str]) -> bool:
    return any(marker in markers for marker in ("mkk", "mrinstaller", "kual"))


def free_mb(kindle: Path) -> int:
    usage = shutil.disk_usage(kindle)
    return int(usage.free // (1024 * 1024))


def require_space(kindle: Path, minimum_mb: int) -> None:
    available = free_mb(kindle)
    if available < minimum_mb:
        raise SafetyError(f"Kindle 剩余空间只有 {available} MB，这一步至少需要 {minimum_mb} MB。")


def state_path(cache: Path) -> Path:
    return cache / "state.json"


def read_state(cache: Path) -> dict:
    path = state_path(cache)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_state(cache: Path, **updates: object) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    current = read_state(cache)
    current.update(updates)
    state_path(cache).write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_dir(args: argparse.Namespace) -> Path:
    if args.cache:
        return Path(args.cache)
    return default_cache()


def clean_ota(kindle: Path, *, remove_hotfix_bins: bool = False) -> list[str]:
    removed: list[str] = []
    for child in _safe_children(kindle):
        if not child.is_file():
            continue
        lower = child.name.lower()
        delete = False
        if lower in {"update.bin.tmp.partial", "update.bin.tmp", "update.bin"} or lower.endswith(".tmp.partial"):
            delete = True
        elif lower.startswith("update_kindle") and lower.endswith(".bin"):
            delete = True
        elif remove_hotfix_bins and "hotfix" in lower and lower.endswith(".bin"):
            delete = True
        if not delete:
            continue
        child.unlink()
        removed.append(child.name)
    return removed


def payload_root(extracted: Path, required_names: Sequence[str]) -> Path:
    def has_any(folder: Path) -> bool:
        return any((folder / name).exists() for name in required_names)

    if has_any(extracted):
        return extracted
    directories = [child for child in extracted.iterdir() if child.is_dir() and child.name != "__MACOSX"]
    if len(directories) == 1 and has_any(directories[0]):
        return directories[0]
    parents: set[Path] = set()
    for name in required_names:
        for match in extracted.rglob(name):
            parents.add(match.parent)
    if len(parents) == 1:
        return parents.pop()
    preview = ", ".join(child.name for child in extracted.iterdir())
    raise SafetyError(f"压缩包里找不到 {', '.join(required_names)}。顶层是: {preview}")


def fresh_extract(archive: Path, cache: Path) -> Path:
    extracted = cache / "extracted" / archive.stem
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.mkdir(parents=True)
    extract_archive(archive, extracted)
    return extracted


def method_steps(method: str) -> list[str]:
    if method == "winterbreak":
        return [
            "安全弹出 Kindle。",
            "点主屏幕上的购物车图标，打开 Kindle 商店。",
            "如果提示关闭飞行模式，点 Yes。",
            "商店打开后点 WinterBreak 图标。",
            "等大约 30 秒，看到屏幕刷出文字并重启界面后，重新插上数据线，回复「已重启」。",
        ]
    if method == "winterbreak2":
        return [
            "安全弹出 Kindle，并连上 Wi-Fi。",
            "打开实验浏览器：主菜单里的 Experimental Browser，或 设置 → 设备选项 → 高级选项。",
            "访问 https://penguins184.xyz/wb2 ，点 Jailbreak，等待它跑完。",
            "重新插上数据线，回复「已重启」。",
        ]
    if method == "languagebreak":
        return [
            "这一步会清空 Kindle 上的内容。确认备份后，在搜索栏输入 ;enter_demo 并回车，然后按住电源键选择重启。",
            "重启后跳过 Wi-Fi，随便填资料，依次选 Skip、Standard、Done。",
            "进入演示模式后，在搜索栏输入 ;demo ，选择 Sideload Content。",
            "重新插上电脑。回复「可以写入」，我会再复制一次 LanguageBreak 文件。",
            "复制完成后弹出 Kindle，回到演示菜单，选 Resell Device 并确认。",
            "一出现 Press the Power Button 就立刻插回电脑，回复「第二次写入」。这一步要快。",
            "第二次写入完成后弹出 Kindle，按住电源键重启。",
            "语言列表里选「简体中文」（在 Pseudot 上面）。看到日志并重启后，插回电脑，回复「已重启」。",
        ]
    if method == "adbreak":
        return [
            "安全弹出 Kindle。",
            "点开一条锁屏广告，一直点到出现 Bang!，然后点 Close。",
            "可以忽略 application error 弹窗。脚本跑完后重新插上数据线，回复「已重启」。",
        ]
    raise SafetyError(f"未知越狱方案: {method}")


def validate_method(method: str, firmware: str, force: bool, wipe_ack: bool) -> None:
    if method == "winterbreak" and version_ge(firmware, ADBREAK_MIN):
        raise SafetyError(f"固件 {firmware} 不能使用 WinterBreak。这一版应使用 AdBreak。")
    if method == "winterbreak2" and version_ge(firmware, WINTERBREAK2_MAX):
        raise SafetyError(f"固件 {firmware} 不在 WinterBreak2 的支持范围（低于 {WINTERBREAK2_MAX}）。")
    if method == "languagebreak":
        if version_cmp(firmware, LANGUAGEBREAK_MAX) > 0:
            raise SafetyError(f"LanguageBreak 只支持 {LANGUAGEBREAK_MAX} 及更低固件，当前是 {firmware}。")
        if not wipe_ack:
            raise SafetyError(
                "LanguageBreak 会清空 Kindle 里的内容。确认已经备份后，"
                "再加 --i-understand-this-wipes-the-kindle 。默认方案是 WinterBreak，不会走这一步。"
            )
    if method == "adbreak" and version_lt(firmware, ADBREAK_MIN) and not force:
        raise SafetyError(f"固件 {firmware} 低于 {ADBREAK_MIN}。默认使用 WinterBreak；确认要改用 AdBreak 时才加 --force。")


def apply_adbreak(kindle: Path, archive: Path, cache: Path) -> dict[str, object]:
    assets = kindle / "system" / ".assets"
    if not assets.is_dir():
        raise SafetyError(
            "找不到 system/.assets。请先让 Kindle 联网下载锁屏广告，打开右上角菜单的 View all ads，"
            "确认能看到 Special Offers，再开飞行模式并重新连接。资源管理器需要显示隐藏文件夹。"
        )
    backup = cache / "backups" / "assets"
    if backup.exists():
        shutil.rmtree(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(assets, backup)
    staging = cache / "work" / "adbreak-staging"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(assets, staging)
    extract_archive(archive, staging)
    html_files = list(staging.rglob("adbreak.html"))
    if len(html_files) != 1:
        raise SafetyError("AdBreak 压缩包里没有唯一的 adbreak.html。设备上的 .assets 还没改。")
    replaced = 0
    for details in staging.rglob("details.html"):
        shutil.copyfile(html_files[0], details)
        replaced += 1
    if replaced == 0:
        raise SafetyError(".assets 里没有 details.html。请先联网把广告下载完整，再重新运行。设备上的原文件夹还在。")
    if not list(staging.rglob("jb.sh")):
        raise SafetyError("AdBreak 压缩包里没有 jb.sh。已停止，设备上的 .assets 未替换。")
    shutil.rmtree(assets)
    try:
        shutil.copytree(staging, assets)
        if not list(assets.rglob("jb.sh")) or not list(assets.rglob("adbreak.html")):
            raise SafetyError("写入后的 .assets 不完整。")
    except Exception as exc:
        if assets.exists():
            shutil.rmtree(assets, ignore_errors=True)
        shutil.copytree(backup, assets)
        if isinstance(exc, SafetyError):
            raise SafetyError(str(exc) + " 已自动恢复原来的 .assets。") from exc
        raise
    return {"replaced_ads": replaced, "backup": str(backup)}


def install_jailbreak_payload(kindle: Path, method: str, archive: Path, cache: Path, locale: str) -> dict[str, object]:
    if method == "adbreak":
        return apply_adbreak(kindle, archive, cache)
    extracted = fresh_extract(archive, cache)
    if method == "winterbreak":
        root = payload_root(extracted, [".active_content_sandbox"])
    elif method == "winterbreak2":
        root = payload_root(extracted, ["winterbreak2"])
    elif method == "languagebreak":
        root = payload_root(extracted, ["LanguageBreak"])
        hotfix_name = f"Update_hotfix_languagebreak-{locale}.bin"
        matches = list(extracted.rglob(hotfix_name))
        if len(matches) != 1:
            available = sorted(path.name for path in extracted.rglob("Update_hotfix_languagebreak-*.bin"))
            raise SafetyError(f"找不到 {hotfix_name}。可选语言包: {', '.join(available)}")
        cached_hotfix = cache / "hotfix" / matches[0].name
        cached_hotfix.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(matches[0], cached_hotfix)
        write_state(
            cache,
            hotfix_path=str(cached_hotfix),
            hotfix_name=cached_hotfix.name,
            hotfix_sha256=sha256_file(cached_hotfix),
            jailbreak_method=method,
        )
    else:
        raise SafetyError(f"未知越狱方案: {method}")
    copied = merge_tree(root, kindle)
    if method == "winterbreak" and not (kindle / ".active_content_sandbox").is_dir():
        raise SafetyError("写入后没有找到 .active_content_sandbox。请不要继续在 Kindle 上触发越狱。")
    if method == "winterbreak2" and not (kindle / "winterbreak2").is_dir():
        raise SafetyError("写入后没有找到 winterbreak2 文件夹。请不要继续。")
    if method == "languagebreak" and not (kindle / "jb").exists() and not (kindle / "patchedUks.sqsh").exists():
        raise SafetyError("LanguageBreak 的 jb 或 patchedUks.sqsh 没有写入 Kindle 根目录。")
    return {"copied_files": copied}


def remember_hotfix(cache: Path, blob: Path, digest: str) -> Path:
    cached = cache / "hotfix" / blob.name
    cached.parent.mkdir(parents=True, exist_ok=True)
    if blob.resolve() != cached.resolve():
        shutil.copy2(blob, cached)
    write_state(cache, hotfix_path=str(cached), hotfix_name=cached.name, hotfix_sha256=digest.lower())
    return cached


def verified_hotfix_file(cache: Path, mirror: str | None) -> Path:
    """Return a hotfix blob whose bytes match a recorded or pinned SHA-256."""
    state = read_state(cache)
    raw = state.get("hotfix_path")
    expected = str(state.get("hotfix_sha256") or "").lower()
    hotfix = Path(str(raw)) if raw else None
    if (
        hotfix is not None
        and hotfix.is_file()
        and re.fullmatch(r"[0-9a-f]{64}", expected)
        and sha256_file(hotfix).lower() == expected
    ):
        return hotfix
    if hotfix is not None and hotfix.is_file():
        log("缓存的 Hotfix 校验失败，改为下载清单里的通用 Hotfix。")
        hotfix.unlink()
    pin, archive = download_pinned("hotfix", cache, mirror)
    verify_pinned_file(archive, pin["sha256"], pin["filename"])
    return remember_hotfix(cache, archive, pin["sha256"])


def select_install_bin(extracted: Path) -> Path:
    bins = [path for path in extracted.rglob("*.bin") if "uninstall" not in path.name.lower()]
    primary = [path for path in bins if "hotfix" not in path.name.lower()]
    chosen = primary or bins
    if len(chosen) != 1:
        names = ", ".join(path.name for path in chosen)
        raise SafetyError(f"无法确定要安装的 KUAL 安装包: {names}")
    return chosen[0]


def install_peki(kindle: Path, cache: Path, mirror: str | None) -> dict[str, object]:
    pin, archive = download_pinned("peki", cache, mirror)
    verify_pinned_file(archive, pin["sha256"], pin["filename"])
    extracted = fresh_extract(archive, cache)
    jars = list(extracted.rglob("KUAL.jar"))
    scripts = list(extracted.rglob("KUAL.sh"))
    if len(jars) != 1 or len(scripts) != 1:
        raise SafetyError("PEKI 压缩包里没有唯一的 KUAL.jar 和 KUAL.sh。")
    documents = kindle / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    shutil.copy2(jars[0], documents / "KUAL.jar")
    shutil.copy2(scripts[0], documents / "KUAL.sh")
    sync_kindle(kindle)
    return {
        "ok": True,
        "status": "awaiting_user",
        "launcher": "peki",
        "message": "KUAL 已经放到书库。请安全弹出 Kindle，在书库里点开 KUAL。能打开后回复「KUAL 已装」。",
        "user_steps": ["安全弹出 Kindle。", "在书库里点开 KUAL。能打开后重新插上数据线，回复「KUAL 已装」。"],
    }


def koreader_root(extracted: Path) -> Path:
    return payload_root(extracted, ["koreader"])


def plugin_source(extracted: Path, folder_name: str) -> Path:
    matches = [path for path in extracted.rglob(folder_name) if path.is_dir()]
    if matches:
        matches.sort(key=lambda path: len(path.parts))
        chosen = matches[0]
        if not (chosen / "main.lua").is_file():
            raise SafetyError(f"{folder_name} 里没有 main.lua，插件不会被 KOReader 加载。")
        return chosen
    if (extracted / "main.lua").is_file():
        return extracted
    raise SafetyError(f"压缩包里没有名为 {folder_name} 的插件目录。")


def deploy_plugin(kindle: Path, source: Path, folder_name: str) -> Path:
    plugins = kindle / "koreader" / "plugins"
    if not plugins.is_dir():
        raise SafetyError("还没有 koreader/plugins。请先完成 KOReader 安装。")
    dest = plugins / folder_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    if dest.name != folder_name or not (dest / "main.lua").is_file():
        raise SafetyError(f"插件目录校验失败: {dest}")
    nested = [path.name for path in dest.iterdir() if path.is_dir() and path.name == folder_name]
    if nested:
        raise SafetyError(f"插件被套了两层 {folder_name}，已停止。")
    return dest


def plugin_choices(choice: str) -> list[str]:
    if choice == "both":
        return ["weread", "miuread"]
    if choice not in PLUGIN_DIRS:
        raise SafetyError("--choice 只能是 weread、miuread 或 both。")
    return [choice]


WEREAD_LOGIN_MARKER = "wekindle: homepage sets wr_gid"


def _replace_once(text: str, old: str, new: str, *, marker: str, required_anchor: str, error: str) -> str:
    if marker in text:
        return text
    if old not in text:
        if required_anchor in text:
            raise SafetyError(error)
        return text
    return text.replace(old, new, 1)


def patch_weread_qr_login_text(text: str) -> str:
    """Patch weread v1.6.0 QR login without touching the pinned zip.

    The skills page no longer sets wr_gid, so getLoginInfo answers HTTP 401.
    The homepage still sets it. Showing the QR immediately also clips it on Kindle.
    """
    text = _replace_once(
        text,
        (
            "function QRLogin:_begin_protocol()\n"
            "    local login_cookies = { wr_fp = self.settings:get_device_fingerprint() }\n"
            "    local _, page_code, page_headers = self.client:request_follow({\n"
            "        url = SKILLS_PAGE_URL,"
        ),
        (
            "function QRLogin:_begin_protocol()\n"
            "    local login_cookies = { wr_fp = self.settings:get_device_fingerprint() }\n"
            "    -- wekindle: homepage sets wr_gid; /r/weread-skills no longer does\n"
            "    local _, home_code, home_headers = self.client:request_follow({\n"
            '        url = BASE_URL .. "/",\n'
            '        method = "GET",\n'
            "        skip_cookie = true,\n"
            "        maxredirects = 5,\n"
            "        timeout = { 10, 20 },\n"
            "        headers = {\n"
            '            ["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",\n'
            '            ["Cookie"] = Cookie.to_header(login_cookies),\n'
            "        },\n"
            "    })\n"
            "    login_cookies = merge_response_cookies(login_cookies, home_headers)\n"
            "    if not home_code or home_code < 200 or home_code >= 300 then\n"
            '        error("Unable to open WeRead home (HTTP " .. tostring(home_code) .. ")")\n'
            "    end\n"
            '    if type(login_cookies.wr_gid) ~= "string" or login_cookies.wr_gid == "" then\n'
            '        error("WeRead home did not return wr_gid")\n'
            "    end\n"
            "    local _, page_code, page_headers = self.client:request_follow({\n"
            "        url = SKILLS_PAGE_URL,"
        ),
        marker=WEREAD_LOGIN_MARKER,
        required_anchor="function QRLogin:_begin_protocol()",
        error="weread 扫码登录补丁对不上 v1.6.0 的 qr_login.lua，已停止。",
    )
    text = _replace_once(
        text,
        (
            "    self.qr_dialog = dialog\n"
            "    UIManager:show(dialog)\n"
            "    self.host:refreshUI()\n"
        ),
        (
            "    self.qr_dialog = dialog\n"
            "    -- wekindle: wait until the surrounding UI has finished, or Kindle clips the QR\n"
            "    UIManager:scheduleIn(0.2, function()\n"
            "        if generation == self.generation and self.qr_dialog == dialog then\n"
            "            UIManager:show(dialog)\n"
            "        end\n"
            "    end)\n"
        ),
        marker="wekindle: wait until the surrounding UI has finished",
        required_anchor="function QRLogin:_show_qr(",
        error="weread 二维码显示补丁对不上 v1.6.0 的 qr_login.lua，已停止。",
    )
    return _replace_once(
        text,
        (
            "        if is_timeout_error(result) then\n"
            "            self:_schedule_poll(uid, generation)\n"
            "            return\n"
            "        end\n"
        ),
        (
            "        if is_timeout_error(result) or tostring(result):find(\"HTTP 401\", 1, true) then\n"
            "            -- wekindle: a 401 while the QR is up means this poll was rejected; keep waiting\n"
            "            self:_schedule_poll(uid, generation)\n"
            "            return\n"
            "        end\n"
        ),
        marker="wekindle: a 401 while the QR is up",
        required_anchor="function QRLogin:_poll(",
        error="weread 扫码轮询补丁对不上 v1.6.0 的 qr_login.lua，已停止。",
    )


def patch_weread_qr_login(source: Path) -> None:
    path = source / "weread" / "lib" / "qr_login.lua"
    if not path.is_file():
        raise SafetyError("weread 插件里没有 weread/lib/qr_login.lua，不能修补扫码登录。")
    text = path.read_text(encoding="utf-8")
    path.write_text(patch_weread_qr_login_text(text), encoding="utf-8", newline="\n")


def fetch_plugin_source(choice: str, cache: Path, mirror: str | None) -> tuple[str, str, Path]:
    folder_name = PLUGIN_DIRS[choice]
    pin, archive = download_pinned(choice, cache, mirror)
    verify_pinned_file(archive, pin["sha256"], pin["filename"])
    source = plugin_source(fresh_extract(archive, cache), folder_name)
    if choice == "weread":
        patch_weread_qr_login(source)
    return folder_name, pin["version"], source


def remember_plugins(cache: Path, choices: Sequence[str]) -> list[str]:
    previous = read_state(cache).get("plugins", [])
    if not isinstance(previous, list):
        previous = []
    merged: list[str] = []
    for item in list(previous) + list(choices):
        text = str(item)
        if text not in merged:
            merged.append(text)
    return merged


def koreader_short_version(text: str) -> str | None:
    match = re.search(r"v(\d{4}\.\d{2}(?:\.\d{1,2})?)", text)
    if not match:
        return None
    return match.group(1)


def read_koreader_version(kindle: Path) -> str | None:
    path = kindle / "koreader" / "git-rev"
    if not path.is_file():
        return None
    return koreader_short_version(path.read_text(encoding="utf-8", errors="replace")[:200])


def coverbrowser_disabled(settings_text: str) -> bool:
    return bool(re.search(r'\["coverbrowser\.koplugin"\]\s*=\s*true', settings_text))


def sui_settings_path(kindle: Path) -> Path:
    return kindle / "koreader" / "settings" / "simpleui" / "sui_settings.lua"


def read_optional_bytes(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    return path.read_bytes()


def restore_sui_settings(kindle: Path, payload: bytes | None) -> None:
    path = sui_settings_path(kindle)
    if payload is None:
        if path.is_file():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def require_simpleui_device(kindle: Path) -> str:
    if not (kindle / "koreader" / "plugins").is_dir():
        raise SafetyError("还没有安装 KOReader，不能装 SimpleUI。")
    version = read_koreader_version(kindle)
    if version is None:
        raise SafetyError("读不到 KOReader 版本（koreader/git-rev）。乔木 SimpleUI 需要 2026.03 或更新。")
    if version_lt(version, SIMPLEUI_MIN_KOREADER):
        raise SafetyError(f"这台 KOReader 是 {version}。乔木 SimpleUI 需要 {SIMPLEUI_MIN_KOREADER} 或更新。")
    cover = kindle / "koreader" / "plugins" / "coverbrowser.koplugin"
    if not cover.is_dir() or (cover / "disable").exists():
        raise SafetyError("没有可用的 CoverBrowser。乔木 SimpleUI 需要 KOReader 自带并启用的 CoverBrowser。")
    settings = kindle / "koreader" / "settings.reader.lua"
    if settings.is_file() and coverbrowser_disabled(settings.read_text(encoding="utf-8", errors="replace")):
        raise SafetyError("CoverBrowser 在 KOReader 设置里被关掉了。请先启用它，再装 SimpleUI。")
    return version


def assert_not_kindle_tree(source: Path) -> None:
    for parent in [source, *source.parents]:
        if (parent / "system").is_dir() and (parent / "documents").is_dir() and (parent / "koreader").is_dir():
            raise SafetyError("不要直接改 Kindle 上的插件。请把 simpleui.koplugin 复制到电脑上再改，然后用 --source 写回去。")
        if parent == parent.parent:
            break


def resolve_simpleui_source(raw: str) -> Path:
    path = Path(raw)
    if not path.exists():
        raise SafetyError(f"找不到 --source：{raw}")
    path = path.resolve()
    assert_not_kindle_tree(path)
    if path.name == SIMPLEUI_DIR and (path / "main.lua").is_file():
        source = path
    elif (path / SIMPLEUI_DIR / "main.lua").is_file():
        source = path / SIMPLEUI_DIR
    else:
        raise SafetyError("请指向 simpleui.koplugin 目录，或指向直接包含它的目录。")
    if not (source / "_meta.lua").is_file():
        raise SafetyError("simpleui.koplugin 里没有 _meta.lua。请使用 Release 里的 simpleui.koplugin，或它的本地副本。")
    if (source / SIMPLEUI_DIR).is_dir():
        raise SafetyError("插件套了两层 simpleui.koplugin，已停止。")
    return source


def simpleui_version_label(source: Path, release: str | None) -> str:
    if release:
        return release
    meta = (source / "_meta.lua").read_text(encoding="utf-8", errors="replace")
    match = re.search(r'version\s*=\s*"([^"]+)"', meta)
    return match.group(1) if match else "local"


def fetch_simpleui_release(cache: Path, mirror: str | None) -> tuple[str, Path]:
    pin, dest = download_pinned("simpleui", cache, mirror)
    verify_pinned_file(dest, pin["sha256"], pin["filename"])
    if dest.name != SIMPLEUI_ASSET:
        dest.unlink(missing_ok=True)
        raise SafetyError("拒绝下载 Source code ZIP。安装包必须正好叫 simpleui.koplugin.zip。")
    source = plugin_source(fresh_extract(dest, cache), SIMPLEUI_DIR)
    if source.name != SIMPLEUI_DIR or not (source / "_meta.lua").is_file():
        raise SafetyError("安装包里没有合格的 simpleui.koplugin。不要改用 GitHub Source code ZIP。")
    return pin["version"], source


def backup_installed_plugin(kindle: Path, cache: Path, folder_name: str) -> Path | None:
    current = kindle / "koreader" / "plugins" / folder_name
    if not current.is_dir():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = cache / "backups" / f"{folder_name}-{stamp}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(current, dest)
    return dest


def restore_plugin_dir(kindle: Path, folder_name: str, backup: Path | None) -> None:
    dest = kindle / "koreader" / "plugins" / folder_name
    if dest.exists():
        shutil.rmtree(dest)
    if backup is not None:
        shutil.copytree(backup, dest)


def simpleui_message(label: str, had_backup: bool, *, wifi: bool) -> str:
    restart = "请在 KOReader 里完全退出，再重新打开。" if wifi else "请安全弹出 Kindle，再打开 KOReader。"
    previous = "原来的插件已备份。" if had_backup else ""
    ssh = SSH_OFF_NOTE if wifi else ""
    return f"乔木 SimpleUI {label} 已写入。{restart}{previous}打开后会直接进入新主页。个人设置没有改。{ssh}"


def open_home_message(*, wifi: bool) -> str:
    if wifi:
        lead = "装好了。请在 KOReader 里完全退出，再重新打开。"
    else:
        lead = "装好了。请安全弹出 Kindle，连上 Wi-Fi，打开 KUAL，点 KOReader。"
    text = (
        f"{lead}第一次会直接进入新主页。"
        "屏幕会先说明如何在手机微信读书里获取 API Key，然后显示微信二维码。"
    )
    if wifi:
        text += SSH_OFF_NOTE
    return text


def simpleui_installed_on(kindle: Path) -> bool:
    return (kindle / "koreader" / "plugins" / SIMPLEUI_DIR / "main.lua").is_file()


def should_install_default_simpleui(already_present: bool, skipped: bool) -> bool:
    return not skipped and not already_present


def reading_login_note(plugins: Sequence[str]) -> str:
    if "weread" in plugins:
        return ""
    sentences: list[str] = []
    if "miuread" in plugins:
        sentences.append("觅阅：打开后按屏幕提示扫码。书架管理会再要求一次授权。")
    return "".join(sentences)


def plugin_bundle_message(
    names: str,
    plugins: Sequence[str],
    *,
    wifi: bool,
    simpleui: dict | None,
    error: str,
    already_present: bool,
) -> str:
    home = simpleui is not None or already_present
    if "weread" in plugins and not error and home:
        kept = "乔木 SimpleUI 已经在设备上，这次没有覆盖。" if already_present and simpleui is None else ""
        return open_home_message(wifi=wifi) + kept
    if simpleui is not None:
        text = f"已装好 {names}。" + str(simpleui.get("message", ""))
        text += reading_login_note(plugins)
        return text
    if error:
        if wifi:
            return wifi_done_message(plugins) + f"乔木 SimpleUI 没有装上：{error}"
        return f"已装好 {names}。乔木 SimpleUI 没有装上：{error} 数据线先不要拔。"
    if already_present:
        kept = "乔木 SimpleUI 已经在设备上，这次没有覆盖。"
        if wifi:
            return wifi_done_message(plugins) + kept
        return f"已装好 {names}。{kept}可以继续收尾并安全弹出。"
    if wifi:
        return wifi_done_message(plugins)
    return f"已装好 {names}。可以继续收尾并安全弹出。"


def install_simpleui_usb(kindle: Path, cache: Path, source: Path, label: str) -> dict:
    require_simpleui_device(kindle)
    require_space(kindle, 40)
    settings = sui_settings_path(kindle)
    before = read_optional_bytes(settings)
    backup = backup_installed_plugin(kindle, cache, SIMPLEUI_DIR)
    deploy_plugin(kindle, source, SIMPLEUI_DIR)
    if read_optional_bytes(settings) != before:
        restore_plugin_dir(kindle, SIMPLEUI_DIR, backup)
        restore_sui_settings(kindle, before)
        sync_kindle(kindle)
        raise SafetyError("安装碰到了 settings/simpleui，已撤回插件并写回原来的设置。")
    prepare_home_usb(kindle)
    clear_usb_ssh_autostart(kindle)
    backup_text = None if backup is None else str(backup)
    write_state(cache, simpleui_release=label, simpleui_backup=backup_text)
    return {
        "ok": True,
        "command": "install-simpleui",
        "status": "installed",
        "release": label,
        "path": str(kindle / "koreader" / "plugins" / SIMPLEUI_DIR),
        "source_path": str(source),
        "backup": backup_text,
        "settings_preserved": True,
        "message": simpleui_message(label, backup is not None, wifi=False),
    }


def _replace_lua_setting(text: str, key: str, value: str) -> tuple[str, bool]:
    literal = r'"(?:\\.|[^"\\])*"|true|false|nil|-?\d+'
    patterns = (
        re.compile(rf'(\["{re.escape(key)}"\]\s*=\s*)({literal})'),
        re.compile(rf"(\b{re.escape(key)}\s*=\s*)({literal})"),
    )
    for pattern in patterns:
        updated, count = pattern.subn(rf"\g<1>{value}", text, count=1)
        if count:
            return updated, True
    return text, False


def _lua_setting_present(text: str, key: str) -> bool:
    return bool(re.search(rf'(?:\["{re.escape(key)}"\]|\b{re.escape(key)})\s*=', text))


def ensure_lua_settings(path: Path, values: dict[str, str], *, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        lines = ["-- koreader/settings.reader.lua", "return {"]
        for key, value in values.items():
            lines.append(f'    ["{key}"] = {value},')
        lines.append("}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    for key, value in values.items():
        text, changed = _replace_lua_setting(text, key, value)
        if changed:
            continue
        brace = text.rfind("}")
        if brace == -1:
            raise SafetyError(error)
        text = text[:brace] + f'    ["{key}"] = {value},\n' + text[brace:]
    path.write_text(text, encoding="utf-8", newline="\n")


def disable_koreader_ssh_autostart(kindle: Path) -> None:
    """Keep KOReader from starting SSH on launch. Never write SSH_autostart = true."""
    directory = kindle / "koreader"
    if not directory.is_dir():
        raise SafetyError("没有 koreader 目录，不能关闭 SSH 自启动。")
    ensure_lua_settings(
        directory / "settings.reader.lua",
        {"SSH_autostart": "false"},
        error="settings.reader.lua 无法识别，没有关闭 SSH 自启动。",
    )


def clear_usb_ssh_autostart(kindle: Path) -> None:
    """Turn off SSH autostart on a USB install, including a file left true by an older install."""
    disable_koreader_ssh_autostart(kindle)
    sync_kindle(kindle)


def preset_simpleui_launch(kindle: Path) -> None:
    """Make the next KOReader launch open SimpleUI, without touching an existing home folder."""
    directory = kindle / "koreader"
    if not directory.is_dir():
        raise SafetyError("没有 koreader 目录，不能设置启动主页。")
    path = directory / "settings.reader.lua"
    existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    values = {
        "start_with": '"homescreen_simpleui"',
        "quickstart_shown_version": KOREADER_QUICKSTART_SHOWN,
    }
    if not _lua_setting_present(existing, "home_dir"):
        values["home_dir"] = '"/mnt/us/documents"'
        if not _lua_setting_present(existing, "lock_home_folder"):
            values["lock_home_folder"] = "true"
    ensure_lua_settings(path, values, error="settings.reader.lua 无法识别，没有改启动主页。")


def seed_simpleui_skip_onboarding(kindle: Path) -> bool:
    """Skip the layout picker only when the user has no SimpleUI settings yet."""
    path = sui_settings_path(kindle)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ONBOARDING_SKIP_LUA, encoding="utf-8", newline="\n")
    return True


def wekindle_source() -> Path:
    source = Path(__file__).resolve().parent.parent / "plugins" / WEKINDLE_DIR
    if not (source / "main.lua").is_file() or not (source / "_meta.lua").is_file():
        raise SafetyError("缺少 plugins/wekindle.koplugin，不能完成首次扫码引导。")
    return source


def deploy_wekindle_usb(kindle: Path) -> Path:
    return deploy_plugin(kindle, wekindle_source(), WEKINDLE_DIR)


def deploy_wekindle_wifi(ip: str, port: int, cache: Path) -> str:
    """Copy the guide plugin. It clears SSH autostart on the next launch, not over the live settings file."""
    staged = stage_plugin(wekindle_source(), cache, WEKINDLE_DIR)
    return upload_plugin(staged, ip, port, cache)


def prepare_home_usb(kindle: Path) -> None:
    preset_simpleui_launch(kindle)
    seed_simpleui_skip_onboarding(kindle)
    marker = kindle / "koreader" / "wekindle_home"
    marker.write_text("homescreen_simpleui\n", encoding="utf-8", newline="\n")
    if (kindle / "koreader" / "plugins" / PLUGIN_DIRS["weread"] / "main.lua").is_file():
        deploy_wekindle_usb(kindle)
    sync_kindle(kindle)


def is_lan_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_link_local:
        return False
    return bool(addr.is_private or addr in CGNAT)


def assert_lan_ip(value: str) -> str:
    text = value.strip()
    if not is_lan_ip(text):
        raise SafetyError(f"{value} 不是局域网 IPv4。无线部署只连接私有地址上的这台 Kindle。")
    return text


def validate_port(value: int) -> int:
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise SafetyError("端口必须是 1 到 65535。")
    return value


def scan_network(addr: str, prefix: int) -> ipaddress.IPv4Network:
    bounded = prefix if prefix >= 24 else 24
    network = ipaddress.ip_network(f"{addr}/{bounded}", strict=False)
    if not isinstance(network, ipaddress.IPv4Network):
        raise SafetyError(f"无法解释网段: {addr}/{prefix}")
    return network


def prefix_from_mask(mask: str) -> int:
    if mask.lower().startswith("0x"):
        return bin(int(mask, 16)).count("1")
    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def decode_console(raw: bytes) -> str:
    """Pick the decoding that yields the most ipconfig addresses.

    Chinese Windows prints ipconfig in GBK/OEM. The GBK codec is in the
    standard library on Linux and macOS too; trying it only when os.name is
    nt made the self-check fail on those systems.
    """
    encodings = ["oem", "mbcs", "gbk", "utf-8"]
    best = raw.decode("utf-8", errors="replace")
    best_score = len(parse_ipconfig(best))
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = len(parse_ipconfig(text))
        if score > best_score:
            best = text
            best_score = score
    return best


def _is_virtual_adapter(name: str) -> bool:
    lowered = name.lower()
    tokens = (
        "vethernet",
        "wsl",
        "vmware",
        "virtualbox",
        "vbox",
        "hyper-v",
        "docker",
        "bluetooth",
        "loopback",
        "tap-windows",
        "vpn",
        "tunnel",
    )
    return any(token in lowered for token in tokens)


def parse_ipconfig(text: str) -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    adapter = ""
    current_ip: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and line[:1] not in {" ", "\t"}:
            adapter = stripped[:-1].strip()
            current_ip = None
            continue
        ip_match = re.search(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", line, re.IGNORECASE)
        if ip_match:
            current_ip = ip_match.group(1)
            continue
        mask_match = re.search(
            r"(?:Subnet Mask|子网掩码|Masque de sous-réseau).*?:\s*(\d+\.\d+\.\d+\.\d+)",
            line,
            re.IGNORECASE,
        )
        if mask_match and current_ip:
            results.append((current_ip, prefix_from_mask(mask_match.group(1)), adapter))
            current_ip = None
    return results


def posix_addresses() -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    try:
        output = subprocess.check_output(["ip", "-4", "-o", "addr"], text=True, errors="replace")
    except (OSError, subprocess.SubprocessError):
        output = ""
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output):
        results.append((match.group(1), int(match.group(2))))
    if results:
        return results
    try:
        output = subprocess.check_output(["ifconfig"], text=True, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    current_ip: str | None = None
    for line in output.splitlines():
        inet_match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if inet_match:
            current_ip = inet_match.group(1)
        mask_match = re.search(r"netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", line)
        if current_ip and mask_match:
            results.append((current_ip, prefix_from_mask(mask_match.group(1))))
            current_ip = None
    return results


def guessed_local_ip() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if is_lan_ip(address):
        return address
    return None


def local_ipv4_networks() -> tuple[list[ipaddress.IPv4Network], list[str], set[str]]:
    notes: list[str] = []
    found: list[tuple[str, int, str]] = []
    if os.name == "nt":
        try:
            raw = subprocess.check_output(["ipconfig"])
            found.extend(parse_ipconfig(decode_console(raw)))
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"读取 ipconfig 失败: {exc}")
    else:
        found.extend((addr, prefix, "") for addr, prefix in posix_addresses())
    if not found:
        guessed = guessed_local_ip()
        if guessed:
            found.append((guessed, 24, ""))
            notes.append("没有读到子网掩码，只扫描本机地址所在的 /24。")
    networks: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    local_ips: set[str] = set()
    for addr, prefix, adapter in found:
        if not is_lan_ip(addr):
            continue
        if adapter and _is_virtual_adapter(adapter):
            notes.append(f"跳过虚拟网卡 {adapter}（{addr}）。")
            continue
        local_ips.add(addr)
        if prefix < 24:
            notes.append(f"{addr} 的网段大于 /24，只扫描它所在的 /24。")
        network = scan_network(addr, prefix)
        if str(network) in seen:
            continue
        seen.add(str(network))
        networks.append(network)
    return networks, notes, local_ips


def port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_ssh_banner(ip: str, port: int, timeout: float) -> str:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(128)
    except OSError:
        return ""
    line = data.decode("utf-8", errors="replace").splitlines()
    return line[0].strip() if line else ""


def probe_ssh(ip: str, port: int, timeout: float) -> dict[str, object] | None:
    if not port_open(ip, port, timeout):
        return None
    return {"ip": ip, "port": port, "banner": read_ssh_banner(ip, port, timeout)}


def scan_networks(
    networks: Sequence[ipaddress.IPv4Network],
    port: int,
    local_ips: set[str],
    timeout: float = 0.35,
) -> list[dict[str, object]]:
    hosts = [str(host) for network in networks for host in network.hosts() if str(host) not in local_ips]
    found: list[dict[str, object]] = []
    if not hosts:
        return found
    log(f"扫描 {len(hosts)} 个局域网地址的 {port} 端口")
    with ThreadPoolExecutor(max_workers=80) as pool:
        futures = [pool.submit(probe_ssh, host, port, timeout) for host in hosts]
        for future in as_completed(futures):
            item = future.result()
            if item:
                found.append(item)
                log(f"发现 {item['ip']}:{port} {item['banner']}")
    found.sort(key=lambda item: ipaddress.ip_address(str(item["ip"])))
    return found


def choose_kindle(candidates: Sequence[dict[str, object]]) -> dict[str, object] | None:
    dropbear = [item for item in candidates if "dropbear" in str(item.get("banner", "")).lower()]
    if len(dropbear) == 1:
        return dropbear[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def remote_plugin_dir(folder_name: str) -> str:
    allowed = set(PLUGIN_DIRS.values()) | {SIMPLEUI_DIR, WEKINDLE_DIR}
    if folder_name not in allowed or not re.fullmatch(r"[A-Za-z0-9._-]+", folder_name):
        raise SafetyError("拒绝把未知目录传到 Kindle。")
    return f"{REMOTE_PLUGIN_PARENT}/{folder_name}"


def stage_plugin(source: Path, cache: Path, folder_name: str) -> Path:
    destination = cache / "staging" / folder_name
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    if destination.name != folder_name or not (destination / "main.lua").is_file():
        raise SafetyError(f"本地暂存的插件目录不正确: {destination}")
    return destination


def known_hosts_file(cache: Path) -> str:
    path = cache / "known_hosts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path.as_posix()


def ssh_option_sets(cache: Path) -> list[list[str]]:
    base = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        f"UserKnownHostsFile={known_hosts_file(cache)}",
    ]
    return [
        base + ["-o", "StrictHostKeyChecking=accept-new"],
        base + ["-o", "StrictHostKeyChecking=no"],
    ]


def run_checked(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    log("运行 " + " ".join(command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SafetyError("连接 Kindle 超时。请确认它和电脑在同一个 Wi-Fi，并且 KOReader 的 SSH 已经打开。") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SafetyError(detail or f"命令失败，退出码 {completed.returncode}")
    return completed


def openssh_available() -> bool:
    return shutil.which("ssh") is not None and shutil.which("scp") is not None


def openssh_exec(ip: str, port: int, cache: Path, remote_command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    last_error: SafetyError | None = None
    for options in ssh_option_sets(cache):
        command = ["ssh", "-p", str(port), *options, f"{SSH_USER}@{ip}", remote_command]
        try:
            return run_checked(command, timeout)
        except SafetyError as exc:
            last_error = exc
            if "Bad configuration" not in str(exc) and "Unsupported" not in str(exc):
                raise
    if last_error:
        raise last_error
    raise SafetyError("SSH 命令没有执行。")


def openssh_require_koreader(ip: str, port: int, cache: Path) -> None:
    try:
        completed = openssh_exec(
            ip,
            port,
            cache,
            "if test -d /mnt/us/koreader; then echo KOREADER_OK; else echo KOREADER_MISSING; fi",
        )
    except SafetyError as exc:
        raise SafetyError(ssh_enable_message(ip)) from exc
    text = completed.stdout or ""
    if "KOREADER_MISSING" in text:
        raise SafetyError("这台设备没有 /mnt/us/koreader。请先用 USB 安装 KOReader。")
    if "KOREADER_OK" not in text:
        raise SafetyError(ssh_enable_message(ip))


def openssh_upload(local_dir: Path, ip: str, port: int, cache: Path) -> None:
    remote_dir = remote_plugin_dir(local_dir.name)
    openssh_exec(ip, port, cache, "mkdir -p /mnt/us/koreader/plugins")
    last_error: SafetyError | None = None
    for options in ssh_option_sets(cache):
        command = [
            "scp",
            "-P",
            str(port),
            *options,
            "-r",
            str(local_dir),
            f"{SSH_USER}@{ip}:{REMOTE_PLUGIN_PARENT}/",
        ]
        try:
            run_checked(command, 180)
            openssh_exec(ip, port, cache, f"test -d {remote_dir} && test -f {remote_dir}/main.lua && sync")
            return
        except SafetyError as exc:
            last_error = exc
            if "Bad configuration" not in str(exc) and "Unsupported" not in str(exc):
                raise
    if last_error:
        raise last_error


def sftp_mkdirs(sftp: object, remote: str) -> None:
    current = ""
    for part in remote.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)  # type: ignore[attr-defined]
        except OSError:
            sftp.mkdir(current)  # type: ignore[attr-defined]


def sftp_upload_dir(sftp: object, local: Path, remote: str) -> None:
    sftp_mkdirs(sftp, remote)
    for child in local.iterdir():
        if child.name in {"__MACOSX", ".DS_Store"} or child.is_symlink():
            continue
        target = remote + "/" + child.name
        if child.is_dir():
            sftp_upload_dir(sftp, child, target)
        else:
            sftp.put(str(child), target)  # type: ignore[attr-defined]


def paramiko_upload(local_dir: Path, ip: str, port: int) -> None:
    if paramiko is None:
        raise SafetyError(
            "电脑上没有 scp/ssh，也没有 paramiko。请启用 Windows 的 OpenSSH 客户端，或运行 pip install paramiko。"
        )
    remote_dir = remote_plugin_dir(local_dir.name)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ip,
            port=port,
            username=SSH_USER,
            password="",
            allow_agent=False,
            look_for_keys=False,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
        )
        _stdin, stdout, _stderr = client.exec_command("test -d /mnt/us/koreader && echo yes")
        if stdout.read().decode("utf-8", errors="replace").strip() != "yes":
            raise SafetyError("这台设备没有 /mnt/us/koreader。请先用 USB 安装 KOReader。")
        sftp = client.open_sftp()
        try:
            sftp_upload_dir(sftp, local_dir, remote_dir)
            try:
                sftp.stat(remote_dir + "/main.lua")
            except OSError as exc:
                raise SafetyError("无线传输后没有找到插件的 main.lua。") from exc
        finally:
            sftp.close()
        _stdin, stdout, _stderr = client.exec_command("sync")
        stdout.channel.recv_exit_status()
    except SafetyError:
        raise
    except Exception as exc:
        raise SafetyError(
            "无线登录失败。请在 KOReader 顶部菜单 → 网络 → SSH 服务器中临时勾选 SSH 服务器，并勾选允许无密码登录。"
            f"{SSH_OFF_NOTE} 详细信息: {exc}"
        ) from exc
    finally:
        client.close()


def upload_plugin(local_dir: Path, ip: str, port: int, cache: Path) -> str:
    if openssh_available():
        openssh_require_koreader(ip, port, cache)
        openssh_upload(local_dir, ip, port, cache)
        return "openssh"
    paramiko_upload(local_dir, ip, port)
    return "paramiko"


def paramiko_connect(ip: str, port: int) -> object:
    if paramiko is None:
        raise SafetyError(
            "电脑上没有 scp/ssh，也没有 paramiko。请启用 Windows 的 OpenSSH 客户端，或运行 pip install paramiko。"
        )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ip,
            port=port,
            username=SSH_USER,
            password="",
            allow_agent=False,
            look_for_keys=False,
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
        )
    except Exception as exc:
        client.close()
        raise SafetyError(
            "无线登录失败。请在 KOReader 顶部菜单 → 网络 → SSH 服务器中临时勾选 SSH 服务器，并勾选允许无密码登录。"
            f"{SSH_OFF_NOTE} 详细信息: {exc}"
        ) from exc
    return client


def remote_shell(ip: str, port: int, cache: Path, command: str) -> str:
    if openssh_available():
        openssh_require_koreader(ip, port, cache)
        completed = openssh_exec(ip, port, cache, command, timeout=60)
        return completed.stdout or ""
    client = paramiko_connect(ip, port)
    try:
        _stdin, stdout, stderr = client.exec_command(command)  # type: ignore[attr-defined]
        text = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise SafetyError(error.strip() or text.strip() or f"远程命令失败，退出码 {code}")
        return text
    finally:
        client.close()


def has_line(text: str, token: str) -> bool:
    return any(line.strip() == token for line in text.splitlines())


def simpleui_probe_command() -> str:
    return " ".join((
        "echo REV_BEGIN;",
        "cat /mnt/us/koreader/git-rev 2>/dev/null;",
        "echo;",
        "echo REV_END;",
        "if test -d /mnt/us/koreader/plugins/coverbrowser.koplugin && "
        "test ! -e /mnt/us/koreader/plugins/coverbrowser.koplugin/disable; then echo COVER_OK; "
        "else echo COVER_MISSING; fi;",
        "if test -d /mnt/us/koreader/plugins/simpleui.koplugin; then echo PLUGIN_PRESENT; else echo PLUGIN_ABSENT; fi;",
        "if test -f /mnt/us/koreader/settings/simpleui/sui_settings.lua; then echo SUI_PRESENT; else echo SUI_ABSENT; fi;",
        "echo COVER_SETTING_BEGIN;",
        "if test -f /mnt/us/koreader/settings.reader.lua; then "
        "grep -F '[\"coverbrowser.koplugin\"]' /mnt/us/koreader/settings.reader.lua || true; fi;",
        "echo COVER_SETTING_END;",
    ))


def parse_simpleui_probe(text: str) -> dict[str, object]:
    start = text.find("REV_BEGIN")
    end = text.find("REV_END")
    if start < 0 or end < start:
        raise SafetyError("读不到 KOReader 版本（koreader/git-rev）。乔木 SimpleUI 需要 2026.03 或更新。")
    version = koreader_short_version(text[start:end])
    if version is None:
        raise SafetyError("读不到 KOReader 版本（koreader/git-rev）。乔木 SimpleUI 需要 2026.03 或更新。")
    if version_lt(version, SIMPLEUI_MIN_KOREADER):
        raise SafetyError(f"这台 KOReader 是 {version}。乔木 SimpleUI 需要 {SIMPLEUI_MIN_KOREADER} 或更新。")
    head, marker, tail = text.partition("COVER_SETTING_BEGIN")
    if not marker or "COVER_SETTING_END" not in tail:
        raise SafetyError("Kindle 返回的检查结果不完整，已停止。")
    if not has_line(head, "COVER_OK") or has_line(head, "COVER_MISSING"):
        raise SafetyError("没有可用的 CoverBrowser。乔木 SimpleUI 需要 KOReader 自带并启用的 CoverBrowser。")
    if coverbrowser_disabled(tail.split("COVER_SETTING_END", 1)[0]):
        raise SafetyError("CoverBrowser 在 KOReader 设置里被关掉了。请先启用它，再装 SimpleUI。")
    return {
        "version": version,
        "plugin_present": has_line(head, "PLUGIN_PRESENT"),
        "settings_present": has_line(head, "SUI_PRESENT"),
    }


def fetch_remote_sui_settings(ip: str, port: int, cache: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    if openssh_available():
        last_error: SafetyError | None = None
        for options in ssh_option_sets(cache):
            command = ["scp", "-P", str(port), *options, f"{SSH_USER}@{ip}:{REMOTE_SUI_SETTINGS}", str(dest)]
            try:
                run_checked(command, 60)
                return
            except SafetyError as exc:
                last_error = exc
                if "Bad configuration" not in str(exc) and "Unsupported" not in str(exc):
                    raise
        if last_error:
            raise last_error
        raise SafetyError("没有读到 settings/simpleui/sui_settings.lua。")
    client = paramiko_connect(ip, port)
    try:
        sftp = client.open_sftp()  # type: ignore[attr-defined]
        try:
            sftp.get(REMOTE_SUI_SETTINGS, str(dest))
        finally:
            sftp.close()
    finally:
        client.close()


def upload_remote_sui_settings(ip: str, port: int, cache: Path, source: Path) -> None:
    if openssh_available():
        last_error: SafetyError | None = None
        for options in ssh_option_sets(cache):
            command = ["scp", "-P", str(port), *options, str(source), f"{SSH_USER}@{ip}:{REMOTE_SUI_SETTINGS}"]
            try:
                run_checked(command, 60)
                return
            except SafetyError as exc:
                last_error = exc
                if "Bad configuration" not in str(exc) and "Unsupported" not in str(exc):
                    raise
        if last_error:
            raise last_error
        raise SafetyError("没有写回 settings/simpleui/sui_settings.lua。")
    client = paramiko_connect(ip, port)
    try:
        sftp = client.open_sftp()  # type: ignore[attr-defined]
        try:
            sftp.put(str(source), REMOTE_SUI_SETTINGS)
        finally:
            sftp.close()
    finally:
        client.close()


def valid_simpleui_backup(path: str) -> bool:
    return bool(re.fullmatch(r"/mnt/us/koreader/backups/simpleui\.koplugin-\d{8}-\d{6}", path))


def remote_backup_simpleui(ip: str, port: int, cache: Path, present: bool) -> str | None:
    remote = remote_plugin_dir(SIMPLEUI_DIR)
    if not present:
        text = remote_shell(ip, port, cache, f"rm -rf {remote} && echo REMOVED_OK")
        if not has_line(text, "REMOVED_OK"):
            raise SafetyError("无法准备 SimpleUI 目录，已停止。")
        return None
    backup = f"/mnt/us/koreader/backups/{SIMPLEUI_DIR}-{time.strftime('%Y%m%d-%H%M%S')}"
    if not valid_simpleui_backup(backup):
        raise SafetyError("备份路径异常，已停止。")
    command = (
        f"mkdir -p /mnt/us/koreader/backups && cp -R {remote} {backup} && "
        f"test -f {backup}/main.lua && rm -rf {remote} && echo BACKUP_OK"
    )
    text = remote_shell(ip, port, cache, command)
    if not has_line(text, "BACKUP_OK"):
        raise SafetyError("没有完成 SimpleUI 备份，已停止，没有替换现有插件。")
    return backup


def remote_rollback_simpleui(ip: str, port: int, cache: Path, backup: str | None) -> None:
    remote = remote_plugin_dir(SIMPLEUI_DIR)
    if backup is None:
        remote_shell(ip, port, cache, f"rm -rf {remote} && sync")
        return
    if not valid_simpleui_backup(backup):
        raise SafetyError("备份路径异常，没有自动撤回插件。")
    text = remote_shell(
        ip,
        port,
        cache,
        f"rm -rf {remote} && cp -R {backup} {remote} && test -f {remote}/main.lua && sync && echo ROLLBACK_OK",
    )
    if not has_line(text, "ROLLBACK_OK"):
        raise SafetyError("撤回 SimpleUI 失败。")


def remote_settings_intact(ip: str, port: int, cache: Path, settings_present: bool, settings_copy: Path) -> bool:
    text = remote_shell(
        ip,
        port,
        cache,
        "if test -f /mnt/us/koreader/settings/simpleui/sui_settings.lua; then echo SUI_PRESENT; else echo SUI_ABSENT; fi",
    )
    after_present = has_line(text, "SUI_PRESENT")
    if settings_present != after_present:
        return False
    if not settings_present:
        return True
    verify = cache / "backups" / "sui_settings-after.lua"
    fetch_remote_sui_settings(ip, port, cache, verify)
    return verify.is_file() and settings_copy.is_file() and verify.read_bytes() == settings_copy.read_bytes()


def install_simpleui_wifi(ip: str, port: int, cache: Path, source: Path, label: str) -> dict:
    facts = parse_simpleui_probe(remote_shell(ip, port, cache, simpleui_probe_command()))
    plugin_present = bool(facts["plugin_present"])
    settings_present = bool(facts["settings_present"])
    settings_copy = cache / "backups" / f"sui_settings-{time.strftime('%Y%m%d-%H%M%S')}.lua"
    if settings_present:
        fetch_remote_sui_settings(ip, port, cache, settings_copy)
        if not settings_copy.is_file():
            raise SafetyError("读不到 settings/simpleui/sui_settings.lua，已停止，没有替换插件。")
    staged = stage_plugin(source, cache, SIMPLEUI_DIR)
    backup_remote = remote_backup_simpleui(ip, port, cache, plugin_present)
    try:
        transport = upload_plugin(staged, ip, port, cache)
    except SafetyError as exc:
        try:
            remote_rollback_simpleui(ip, port, cache, backup_remote)
        except SafetyError as rollback_error:
            raise SafetyError(f"SimpleUI 没有写完，撤回也失败了: {rollback_error}。原来的错误: {exc}") from exc
        raise SafetyError(f"SimpleUI 没有写完，已尝试撤回原来的插件。settings/simpleui 没有改。详细信息: {exc}") from exc
    try:
        intact = remote_settings_intact(ip, port, cache, settings_present, settings_copy)
    except SafetyError:
        intact = False
    if not intact:
        remote_rollback_simpleui(ip, port, cache, backup_remote)
        if settings_present and settings_copy.is_file():
            upload_remote_sui_settings(ip, port, cache, settings_copy)
        else:
            remote_shell(ip, port, cache, f"rm -f {REMOTE_SUI_SETTINGS} && sync")
        raise SafetyError("安装碰到了 settings/simpleui，已撤回插件并写回原来的设置。")
    write_state(
        cache,
        simpleui_release=label,
        simpleui_backup=backup_remote,
        deploy_mode="wifi",
        kindle_ip=ip,
    )
    prepare_home_wifi(ip, port, cache)
    return {
        "ok": True,
        "command": "wifi-install-simpleui",
        "status": "installed",
        "transport": transport,
        "kindle_ip": ip,
        "port": port,
        "release": label,
        "path": remote_plugin_dir(SIMPLEUI_DIR),
        "source_path": str(source),
        "backup": backup_remote,
        "settings_preserved": True,
        "message": simpleui_message(label, backup_remote is not None, wifi=True),
    }


def prepare_home_wifi(ip: str, port: int, cache: Path) -> None:
    """Arm the next launch. Do not rewrite settings.reader.lua while KOReader is running."""
    remote_shell(
        ip,
        port,
        cache,
        "mkdir -p /mnt/us/koreader && printf '%s\\n' homescreen_simpleui > /mnt/us/koreader/wekindle_home && sync",
    )
    text = remote_shell(
        ip,
        port,
        cache,
        "if test -f /mnt/us/koreader/settings/simpleui/sui_settings.lua; then echo SUI_PRESENT; "
        "else echo SUI_ABSENT; fi",
    )
    if has_line(text, "SUI_ABSENT") and not has_line(text, "SUI_PRESENT"):
        remote_shell(ip, port, cache, "mkdir -p /mnt/us/koreader/settings/simpleui && echo DIR_OK")
        local = cache / "work" / "sui_settings.lua"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(ONBOARDING_SKIP_LUA, encoding="utf-8", newline="\n")
        upload_remote_sui_settings(ip, port, cache, local)
        remote_shell(ip, port, cache, "sync")
    deploy_wekindle_wifi(ip, port, cache)


def ssh_enable_message(ip: str | None = None) -> str:
    target = f"{ip} 的 " if ip else ""
    return (
        f"还连不上{target}{SSH_PORT} 端口。请打开 KOReader，进入顶部菜单 → 网络 → SSH 服务器，"
        "临时勾选 SSH 服务器，并勾选允许无密码登录。确认和电脑在同一个 Wi-Fi 后，回复「SSH 已开」。"
        + SSH_OFF_NOTE
    )


def login_hint(plugins: Sequence[str], *, home: bool = False) -> str:
    if home and "weread" in plugins:
        return open_home_message(wifi=False)
    sentences = ["装好了。请安全弹出 Kindle，打开 KUAL，点 KOReader。"]
    if home:
        sentences.append("第一次会直接进入新主页。")
    if "miuread" in plugins:
        sentences.append("觅阅会在屏幕上提示扫码。书架管理会再要求一次授权。")
    if "weread" in plugins and not home:
        sentences.append("微信读书会在屏幕上提示扫码。")
    if not plugins and not home:
        sentences.append("然后从顶部工具栏进入已经安装的阅读插件。")
    return "".join(sentences)


def wifi_done_message(plugins: Sequence[str], *, home: bool = False) -> str:
    if home and "weread" in plugins:
        return open_home_message(wifi=True)
    sentences = ["装好了。请在 KOReader 里完全退出，再重新打开。"]
    if home:
        sentences.append("第一次会直接进入新主页。")
    if "miuread" in plugins:
        sentences.append("觅阅会在屏幕上提示扫码。书架管理会再要求一次授权。")
    if "weread" in plugins and not home:
        sentences.append("微信读书会在屏幕上提示扫码。")
    if not plugins and not home:
        sentences.append("然后从顶部工具栏进入已经安装的阅读插件。")
    return "".join(sentences) + SSH_OFF_NOTE


def emit(args: argparse.Namespace, payload: dict, code: int) -> None:
    payload.setdefault("ok", code == 0)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        message = str(payload.get("message") or payload.get("error") or "")
        if message:
            sys.stdout.write(message + "\n")
    raise SystemExit(code)


def cmd_detect(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    firmware = firmware_of(kindle, args)
    serial = scan_serial(kindle)
    prefix = serial[:4] if serial else None
    markers = jailbreak_markers(kindle)
    payload: dict[str, object] = {
        "ok": True,
        "command": "detect",
        "kindle_path": str(kindle),
        "free_mb": free_mb(kindle),
        "firmware": firmware,
        "serial": serial,
        "serial_prefix": prefix,
        "model": SERIAL_PREFIXES.get(prefix or ""),
        "markers": markers,
        "jailbroken": is_jailbroken(markers),
        "kual_present": "kual" in markers,
        "koreader_present": (kindle / "koreader").is_dir(),
        "simpleui_present": (kindle / "koreader" / "plugins" / SIMPLEUI_DIR / "main.lua").is_file(),
    }
    payload["deploy_mode"] = "usb_or_wifi" if payload["koreader_present"] else "usb_required"
    if firmware:
        payload.update(route_firmware(firmware))
        payload["jailbreak_method"] = recommend_method(firmware)
        model = payload["model"] or "已识别的 Kindle"
        state = "已经有 KOReader。" if payload["koreader_present"] else "还没有 KOReader，首次安装必须使用 USB。"
        payload["message"] = (
            f"已识别 {model}，固件 {firmware}，架构 {payload['architecture']}。"
            f"KOReader 用 {payload['koreader_arch']}，KUAL 用 {payload['kual_variant']}。{state}"
        )
    else:
        payload["message"] = (
            "Kindle 已连接，但读不到固件版本。请打开 设置 → 设备选项 → 设备信息，"
            "把型号和固件版本发我。基准机是 Paperwhite 第 7 代 KP3，固件 5.16.2.1.1。"
        )
    return payload


def cmd_clean_ota(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    removed = clean_ota(kindle, remove_hotfix_bins=args.include_hotfix)
    sync_kindle(kindle)
    if removed:
        message = "已删除升级残留: " + ", ".join(removed)
    else:
        message = "根目录没有 update.bin.tmp.partial 或其他升级残留。"
    return {"ok": True, "command": "clean-ota", "removed": removed, "message": message}


def cmd_install_jailbreak(args: argparse.Namespace) -> dict:
    require_jailbreak_acknowledgement(args.i_accept_jailbreak_risk)
    kindle = require_kindle(args)
    firmware = require_firmware(kindle, args)
    method = args.method or recommend_method(firmware)
    validate_method(method, firmware, args.force, args.i_understand_this_wipes_the_kindle)
    require_space(kindle, 80)
    cache = cache_dir(args)
    pin, archive = download_pinned(method, cache, args.mirror)
    verify_pinned_file(archive, pin["sha256"], pin["filename"])
    hotfix_pin: dict[str, str] | None = None
    hotfix_archive: Path | None = None
    if method != "languagebreak":
        hotfix_pin, hotfix_archive = download_pinned("hotfix", cache, args.mirror)
        verify_pinned_file(hotfix_archive, hotfix_pin["sha256"], hotfix_pin["filename"])
    removed = clean_ota(kindle)
    if hotfix_pin is not None and hotfix_archive is not None:
        remember_hotfix(cache, hotfix_archive, hotfix_pin["sha256"])
    details = install_jailbreak_payload(kindle, method, archive, cache, args.locale)
    write_state(cache, jailbreak_method=method)
    sync_kindle(kindle)
    steps = method_steps(method)
    routing = route_firmware(firmware)
    message = steps[0] if method == "languagebreak" else "文件已经写入 Kindle。" + "".join(steps)
    return {
        "ok": True,
        "command": "install-jailbreak",
        "status": "awaiting_user",
        "firmware": firmware,
        "method": method,
        "release": pin["version"],
        "removed_ota": removed,
        "user_steps": steps,
        "message": message,
        **routing,
        **details,
    }


def cmd_install_hotfix(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    cache = cache_dir(args)
    hotfix = verified_hotfix_file(cache, args.mirror)
    expected = str(read_state(cache).get("hotfix_sha256") or "")
    verify_pinned_file(hotfix, expected, hotfix.name)
    require_space(kindle, 20)
    clean_ota(kindle, remove_hotfix_bins=True)
    target = kindle / hotfix.name
    shutil.copy2(hotfix, target)
    sync_kindle(kindle)
    if not target.is_file():
        raise SafetyError("Hotfix 没有出现在 Kindle 根目录。")
    return {
        "ok": True,
        "command": "install-hotfix",
        "status": "awaiting_user",
        "hotfix": target.name,
        "message": (
            f"已把 {target.name} 放到 Kindle 根目录。请安全弹出，打开设置，只点一次 Update Your Kindle。"
            "完成后重新插上数据线，回复「Hotfix 已装」。"
        ),
        "user_steps": [
            "安全弹出 Kindle。",
            "打开设置，点 Update Your Kindle。只点这一次。",
            "更新完成后重新插上数据线，回复「Hotfix 已装」。",
        ],
    }


def cmd_install_kual(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    if kual_present(kindle) and not args.force:
        return {
            "ok": True,
            "command": "install-kual",
            "status": "already_present",
            "message": "KUAL 已经在这台 Kindle 上。先不要弹出，下一步直接安装 KOReader。",
        }
    if args.launcher == "peki":
        require_space(kindle, 30)
        return install_peki(kindle, cache_dir(args), args.mirror)
    firmware = require_firmware(kindle, args)
    routing = route_firmware(firmware)
    variant = args.variant or routing["kual_variant"]
    if variant != routing["kual_variant"] and not args.force:
        raise SafetyError(
            f"固件 {firmware} 应使用 {routing['kual_variant']} 版 KUAL，你传入了 {variant}。确认后可加 --force。"
        )
    require_space(kindle, 80)
    cache = cache_dir(args)
    mrpi_key, kual_key = KUAL_PIN_KEYS[variant]
    mrpi_pin, mrpi_archive = download_pinned(mrpi_key, cache, args.mirror)
    kual_pin, kual_archive = download_pinned(kual_key, cache, args.mirror)
    verify_pinned_file(mrpi_archive, mrpi_pin["sha256"], mrpi_pin["filename"])
    verify_pinned_file(kual_archive, kual_pin["sha256"], kual_pin["filename"])
    mrpi_root = fresh_extract(mrpi_archive, cache)
    for folder_name in ("extensions", "mrpackages"):
        source = payload_root(mrpi_root, [folder_name]) / folder_name
        if not source.is_dir():
            raise SafetyError(f"MRPI 压缩包里没有 {folder_name}。")
        merge_tree(source, kindle / folder_name)
    booklet = select_install_bin(fresh_extract(kual_archive, cache))
    destination = kindle / "mrpackages" / booklet.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(booklet, destination)
    sync_kindle(kindle)
    write_state(cache, kual_variant=variant, kual_bin=booklet.name)
    return {
        "ok": True,
        "command": "install-kual",
        "status": "awaiting_user",
        "kual_variant": variant,
        "package": booklet.name,
        "message": "MRPI 和 KUAL 安装包已放好。请安全弹出 Kindle，在搜索栏输入 ;log mrpi 并回车。装完后重新连接，回复「KUAL 已装」。",
        "user_steps": [
            "安全弹出 Kindle。",
            "在主屏幕搜索栏输入 ;log mrpi 并回车。",
            "安装结束后重新插上数据线，回复「KUAL 已装」。",
        ],
    }


def cmd_install_koreader(args: argparse.Namespace) -> dict:
    if args.arch not in {"kindlepw2", "kindlehf"}:
        raise SafetyError("--arch 只能是 kindlepw2 或 kindlehf。")
    kindle = require_kindle(args)
    firmware = firmware_of(kindle, args)
    if firmware:
        expected = route_firmware(firmware)["koreader_arch"]
        if expected != args.arch and not args.force:
            raise SafetyError(f"固件 {firmware} 必须安装 {expected}，不能安装 {args.arch}。")
    require_space(kindle, 300)
    cache = cache_dir(args)
    pin_key = f"koreader-{args.arch}"
    pin, archive = download_pinned(pin_key, cache, args.mirror)
    verify_pinned_file(archive, pin["sha256"], pin["filename"])
    if f"koreader-{args.arch}-" not in pin["filename"]:
        raise SafetyError(f"清单里的 {pin_key} 不是 {args.arch} 安装包，已停止，没有写入 Kindle。")
    extracted = fresh_extract(archive, cache)
    copied = merge_tree(koreader_root(extracted), kindle)
    launcher = kindle / "extensions" / "koreader"
    program = kindle / "koreader"
    if not program.is_dir() or not launcher.is_dir():
        raise SafetyError("解压后没有同时得到 koreader 和 extensions/koreader。Kindle 上的半成品请先不要使用。")
    disable_koreader_ssh_autostart(kindle)
    sync_kindle(kindle)
    write_state(
        cache,
        koreader_arch=args.arch,
        koreader_release=pin["version"],
        ssh_autostart=False,
    )
    return {
        "ok": True,
        "command": "install-koreader",
        "status": "installed",
        "arch": args.arch,
        "release": pin["version"],
        "copied_files": copied,
        "ssh_autostart": False,
        "message": (
            f"KOReader {pin['version']}（{args.arch}）已写入 Kindle。"
            "SSH 不会随 KOReader 启动。需要无线操作时，再在 KOReader 里临时打开 SSH。"
            "先不要弹出。接下来会装上微信读书和新主页。"
        ),
    }


def attach_default_simpleui_usb(
    kindle: Path,
    cache: Path,
    mirror: str | None,
    skipped: bool,
) -> tuple[dict | None, str, bool]:
    present = simpleui_installed_on(kindle)
    if not should_install_default_simpleui(present, skipped):
        return None, "", present
    try:
        require_simpleui_device(kindle)
        release, source = fetch_simpleui_release(cache, mirror)
        payload = install_simpleui_usb(kindle, cache, source, simpleui_version_label(source, release))
    except (SafetyError, NetworkError) as exc:
        return None, str(exc), False
    return payload, "", False


def attach_default_simpleui_wifi(
    ip: str,
    port: int,
    cache: Path,
    mirror: str | None,
    skipped: bool,
) -> tuple[dict | None, str, bool]:
    if skipped:
        return None, "", False
    try:
        facts = parse_simpleui_probe(remote_shell(ip, port, cache, simpleui_probe_command()))
    except (SafetyError, NetworkError) as exc:
        return None, str(exc), False
    if facts["plugin_present"]:
        return None, "", True
    try:
        release, source = fetch_simpleui_release(cache, mirror)
        payload = install_simpleui_wifi(ip, port, cache, source, simpleui_version_label(source, release))
    except (SafetyError, NetworkError) as exc:
        return None, str(exc), False
    return payload, "", False


def cmd_install_plugin(args: argparse.Namespace) -> dict:
    choices = plugin_choices(args.choice)
    kindle = require_kindle(args)
    if not (kindle / "koreader" / "plugins").is_dir():
        raise SafetyError("还没有安装 KOReader，不能先装插件。")
    skipped = bool(args.no_simpleui)
    present = simpleui_installed_on(kindle)
    require_space(kindle, 40 if not should_install_default_simpleui(present, skipped) else 80)
    cache = cache_dir(args)
    installed: list[dict[str, str]] = []
    for choice in choices:
        folder_name, release, source = fetch_plugin_source(choice, cache, args.mirror)
        dest = deploy_plugin(kindle, source, folder_name)
        installed.append({"choice": choice, "release": release, "path": str(dest)})
    sync_kindle(kindle)
    plugins = remember_plugins(cache, choices)
    write_state(cache, plugins=plugins, deploy_mode="usb")
    simpleui, simpleui_error, already = attach_default_simpleui_usb(kindle, cache, args.mirror, skipped)
    if not skipped and simpleui_installed_on(kindle):
        prepare_home_usb(kindle)
    elif "weread" in choices:
        deploy_wekindle_usb(kindle)
        sync_kindle(kindle)
    clear_usb_ssh_autostart(kindle)
    home_ready = not skipped and not simpleui_error and simpleui_installed_on(kindle)
    write_state(cache, simpleui_ready=home_ready)
    names = "、".join(PLUGIN_DIRS[choice] for choice in choices)
    return {
        "ok": True,
        "command": "install-plugin",
        "status": "installed",
        "installed": installed,
        "simpleui": simpleui,
        "simpleui_error": simpleui_error,
        "simpleui_already_present": already,
        "message": plugin_bundle_message(
            names,
            plugins,
            wifi=False,
            simpleui=simpleui,
            error=simpleui_error,
            already_present=already,
        ),
    }


def cmd_scan_wifi(args: argparse.Namespace) -> dict:
    port = validate_port(args.port)
    if args.ip:
        ip = assert_lan_ip(args.ip)
        found = probe_ssh(ip, port, 1.0)
        candidates = [found] if found else []
        notes: list[str] = []
        networks: list[str] = []
    else:
        nets, notes, local_ips = local_ipv4_networks()
        if not nets:
            raise SafetyError("没有找到电脑的局域网 IPv4。请先把电脑连上和 Kindle 相同的 Wi-Fi。")
        networks = [str(item) for item in nets]
        candidates = scan_networks(nets, port, local_ips)
    chosen = choose_kindle(candidates)
    if chosen:
        message = f"找到 Kindle：{chosen['ip']}:{port}。可以直接无线安装插件。"
        status = "found"
    elif candidates:
        joined = "、".join(str(item["ip"]) for item in candidates)
        message = f"找到多台开着 {port} 端口的设备：{joined}。请用 --ip 指定 Kindle。"
        status = "ambiguous"
    else:
        message = f"没有发现开放 {port} 端口的 Kindle。"
        status = "not_found"
    if notes:
        message = message + "".join(notes)
    return {
        "ok": True,
        "command": "scan-wifi",
        "status": status,
        "mode": "wifi" if chosen else "not_found",
        "port": port,
        "networks": networks,
        "notes": notes,
        "candidates": candidates,
        "kindle_ip": None if chosen is None else chosen["ip"],
        "message": message,
    }


def cmd_wifi_install_plugin(args: argparse.Namespace) -> dict:
    choices = plugin_choices(args.choice)
    ip = assert_lan_ip(args.ip)
    port = validate_port(args.port)
    if not port_open(ip, port, 1.5):
        return {
            "ok": True,
            "command": "wifi-install-plugin",
            "status": "awaiting_user",
            "kindle_ip": ip,
            "port": port,
            "message": ssh_enable_message(ip),
        }
    cache = cache_dir(args)
    installed: list[dict[str, str]] = []
    transport = ""
    for choice in choices:
        folder_name, release, source = fetch_plugin_source(choice, cache, args.mirror)
        staged = stage_plugin(source, cache, folder_name)
        transport = upload_plugin(staged, ip, port, cache)
        installed.append({"choice": choice, "release": release, "path": remote_plugin_dir(folder_name)})
    plugins = remember_plugins(cache, choices)
    write_state(cache, plugins=plugins, deploy_mode="wifi", kindle_ip=ip)
    simpleui, simpleui_error, already = attach_default_simpleui_wifi(ip, port, cache, args.mirror, bool(args.no_simpleui))
    if not args.no_simpleui and (simpleui is not None or already):
        prepare_home_wifi(ip, port, cache)
    else:
        deploy_wekindle_wifi(ip, port, cache)
    home_ready = not args.no_simpleui and not simpleui_error and (simpleui is not None or already)
    write_state(cache, simpleui_ready=home_ready)
    names = "、".join(PLUGIN_DIRS[choice] for choice in choices)
    return {
        "ok": True,
        "command": "wifi-install-plugin",
        "status": "installed",
        "transport": transport,
        "kindle_ip": ip,
        "port": port,
        "installed": installed,
        "simpleui": simpleui,
        "simpleui_error": simpleui_error,
        "simpleui_already_present": already,
        "message": plugin_bundle_message(
            names,
            plugins,
            wifi=True,
            simpleui=simpleui,
            error=simpleui_error,
            already_present=already,
        ),
    }


def load_simpleui_source(args: argparse.Namespace, cache: Path) -> tuple[str | None, Path]:
    if args.source:
        return None, resolve_simpleui_source(args.source)
    release, source = fetch_simpleui_release(cache, args.mirror)
    return release, source


def cmd_install_simpleui(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    cache = cache_dir(args)
    release, source = load_simpleui_source(args, cache)
    return install_simpleui_usb(kindle, cache, source, simpleui_version_label(source, release))


def cmd_wifi_install_simpleui(args: argparse.Namespace) -> dict:
    ip = assert_lan_ip(args.ip)
    port = validate_port(args.port)
    if not port_open(ip, port, 1.5):
        return {
            "ok": True,
            "command": "wifi-install-simpleui",
            "status": "awaiting_user",
            "kindle_ip": ip,
            "port": port,
            "message": ssh_enable_message(ip),
        }
    cache = cache_dir(args)
    release, source = load_simpleui_source(args, cache)
    return install_simpleui_wifi(ip, port, cache, source, simpleui_version_label(source, release))


def cmd_restore_assets(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    backup = cache_dir(args) / "backups" / "assets"
    if not backup.is_dir():
        raise SafetyError("没有找到 AdBreak 之前的 .assets 备份。")
    target = kindle / "system" / ".assets"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(backup, target)
    sync_kindle(kindle)
    return {"ok": True, "command": "restore-assets", "message": "已把 system/.assets 恢复为 AdBreak 之前的备份。"}


def cmd_finalize(args: argparse.Namespace) -> dict:
    kindle = None
    try:
        kindle = require_kindle(args)
        sync_kindle(kindle)
    except SafetyError:
        if args.path:
            raise
    cache = cache_dir(args)
    state = read_state(cache)
    for folder_name in ("downloads", "extracted", "work", "staging"):
        folder = cache / folder_name
        if folder.exists():
            shutil.rmtree(folder)
    plugins = state.get("plugins", [])
    if not isinstance(plugins, list):
        plugins = []
    plugin_names = [str(item) for item in plugins]
    home = state.get("simpleui_ready") is True
    if state.get("deploy_mode") == "wifi":
        message = wifi_done_message(plugin_names, home=home)
    else:
        message = login_hint(plugin_names, home=home)
    backup = cache / "backups" / "assets"
    if backup.is_dir():
        message += f" AdBreak 修改前的 .assets 备份仍留在 {backup}。"
    return {
        "ok": True,
        "command": "finalize",
        "status": "done",
        "message": message,
    }


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)


def cmd_self_check(_args: argparse.Namespace) -> dict:
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        if not condition:
            failures.append(label)

    reference = route_firmware(REFERENCE_FIRMWARE)
    check(reference["architecture"] == "legacy_softfloat", "reference architecture")
    check(reference["koreader_arch"] == "kindlepw2", "reference koreader arch")
    check(reference["kual_variant"] == "coplate", "reference kual")
    check(recommend_method(REFERENCE_FIRMWARE) == "winterbreak", "reference method")
    check(route_firmware("5.16.3")["koreader_arch"] == "kindlehf", "hf cutoff equal")
    check(route_firmware("5.16.2.1.1")["koreader_arch"] == "kindlepw2", "just below cutoff")
    check(recommend_method("5.18.1") == "adbreak", "adbreak threshold")
    check(version_lt("5.16.2.1.1", "5.16.3"), "version compare")
    check(parse_firmware("System Software Version: 329 (5.16.2.1.1)") == "5.16.2.1.1", "parse firmware")
    check(SERIAL_PREFIXES["G090"].startswith("Kindle Paperwhite 3"), "G090 model")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        check(kindle_problem(root) is not None, "reject ordinary directory")
        (root / "system").mkdir()
        (root / "documents").mkdir()
        (root / "documents" / "dictionaries").mkdir()
        (root / "system" / "version.txt").write_text("Kindle 5.16.2.1.1\n", encoding="utf-8")
        check(kindle_problem(root) is not None, "reject fake kindle on system disk")
        check(kindle_problem(root, _bypass_location=True) is None, "accept marked test volume")
        (root / "update.bin.tmp.partial").write_text("ota", encoding="utf-8")
        (root / "Update_hotfix_universal.bin").write_text("keep", encoding="utf-8")
        removed = clean_ota(root)
        check(removed == ["update.bin.tmp.partial"], "clean partial only")
        check((root / "Update_hotfix_universal.bin").is_file(), "keep hotfix bin")

        malicious = root / "bad.zip"
        _write_zip(malicious, {"../evil.txt": "nope"})
        try:
            extract_archive(malicious, root / "bad-out")
            check(False, "zip slip rejected")
        except SafetyError:
            check(not (root.parent / "evil.txt").exists(), "zip slip did not escape")

        plugin_zip = root / "plugin.zip"
        _write_zip(plugin_zip, {"wrapper/weread.koplugin/main.lua": "return {}"})
        extracted = root / "plugin-src"
        extract_archive(plugin_zip, extracted)
        source = plugin_source(extracted, "weread.koplugin")
        plugins = root / "koreader" / "plugins"
        plugins.mkdir(parents=True)
        deploy_plugin(root, source, "weread.koplugin")
        check((plugins / "weread.koplugin" / "main.lua").is_file(), "plugin flattened")
        check(not (plugins / "weread.koplugin" / "weread.koplugin").exists(), "plugin not nested")

        booklet_dir = root / "booklet"
        booklet_dir.mkdir()
        (booklet_dir / "Update_KUALBooklet_uninstall.bin").write_text("x", encoding="utf-8")
        (booklet_dir / "Update_KUALBooklet_hotfix_install.bin").write_text("x", encoding="utf-8")
        (booklet_dir / "Update_KUALBooklet_install.bin").write_text("x", encoding="utf-8")
        check(select_install_bin(booklet_dir).name == "Update_KUALBooklet_install.bin", "select kual bin")

        digest = sha256_file(booklet_dir / "Update_KUALBooklet_install.bin")
        check(len(digest) == 64, "sha256")
        pins = load_pins()
        for key in PIN_KEYS:
            item = pins[key]
            check(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None, f"pin hash {key}")
            check(item["url"].startswith("https://"), f"pin url {key}")
            check("/" not in item["filename"] and "\\" not in item["filename"], f"pin name {key}")
        for arch in ("kindlepw2", "kindlehf"):
            version = pins[f"koreader-{arch}"]["version"].removeprefix("v")
            check(version_ge(version, SIMPLEUI_MIN_KOREADER), f"pinned {arch} meets simpleui")
        check(
            pins["koreader-kindlepw2"]["version"] == pins["koreader-kindlehf"]["version"],
            "koreader arch versions match",
        )
        check(pins["simpleui"]["filename"] == SIMPLEUI_ASSET, "simpleui pin filename")
        tampered = root / "tampered.bin"
        tampered.write_bytes(b"not-the-release")
        try:
            verify_pinned_file(tampered, pins["hotfix"]["sha256"], "tampered.bin")
            check(False, "pin mismatch raises")
        except SafetyError as exc:
            check(not tampered.exists(), "pin mismatch deletes file")
            check("没有写入 Kindle" in str(exc), "pin mismatch fail closed")
        kept = root / "kept.bin"
        kept.write_bytes(b"pinned-bytes")
        verify_pinned_file(kept, hashlib.sha256(b"pinned-bytes").hexdigest(), "kept.bin")
        check(kept.is_file(), "matching pin kept")
        check(not _cached_pin_matches(kept, pins["hotfix"]["sha256"]), "bad cache is not a pin hit")
        mirrored = mirror_candidates(pins["weread"]["url"], "https://ghfast.top/")
        check(len(mirrored) == 2 and mirrored[1].startswith("https://ghfast.top/https://"), "mirror still verified by hash")
        helper_now = Path(__file__).read_text(encoding="utf-8")
        check("def github_" + "latest(" not in helper_now, "no latest release helper")
        check("def download_" + "named_release(" not in helper_now, "no unpinned release helper")
        check("/releases/" + "latest" not in helper_now, "no latest release url")

        tar_path = root / "sample.tar"
        payload = b"marker"
        with tarfile.open(tar_path, "w") as bundle:
            directory = tarfile.TarInfo(name=".")
            directory.type = tarfile.DIRTYPE
            bundle.addfile(directory)
            marker = tarfile.TarInfo(name="./.active_content_sandbox/marker.txt")
            marker.size = len(payload)
            bundle.addfile(marker, io.BytesIO(payload))
        tar_dest = root / "tar-out"
        extract_archive(tar_path, tar_dest)
        check((tar_dest / ".active_content_sandbox" / "marker.txt").read_bytes() == payload, "tar dot entries")
        check(payload_root(tar_dest, [".active_content_sandbox"]).name == "tar-out", "payload root")

        ssh_root = root / "ssh-kindle"
        (ssh_root / "koreader").mkdir(parents=True)
        disable_koreader_ssh_autostart(ssh_root)
        ssh_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["SSH_autostart"] = false' in ssh_text, "ssh autostart off")
        check("SSH_allow_no_password" not in ssh_text, "install does not enable passwordless ssh")
        check("SSH_port" not in ssh_text, "install does not preset ssh port")
        preset_simpleui_launch(ssh_root)
        launch_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["start_with"] = "homescreen_simpleui"' in launch_text, "start with simpleui")
        check('["home_dir"] = "/mnt/us/documents"' in launch_text, "default documents home")
        check('["SSH_autostart"] = false' in launch_text, "launch preset keeps ssh off")
        check(
            '["quickstart_shown_version"] = ' + KOREADER_QUICKSTART_SHOWN in launch_text,
            "skip koreader quickstart",
        )
        (ssh_root / "koreader" / "settings.reader.lua").write_text(
            'return {\n    ["home_dir"] = "/mnt/us/documents/Books",\n    ["start_with"] = "filemanager",\n}\n',
            encoding="utf-8",
        )
        preset_simpleui_launch(ssh_root)
        launch_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('"/mnt/us/documents/Books"' in launch_text, "keep custom home")
        check(launch_text.count('["home_dir"]') == 1, "home dir not duplicated")
        check('["start_with"] = "homescreen_simpleui"' in launch_text, "override start with")
        check(seed_simpleui_skip_onboarding(ssh_root), "seed fresh onboarding skip")
        seeded = ssh_root / "koreader" / "settings" / "simpleui" / "sui_settings.lua"
        seeded_bytes = seeded.read_bytes()
        check(b"simpleui_onboarding_done" in seeded_bytes, "onboarding skip key")
        check(not seed_simpleui_skip_onboarding(ssh_root), "do not reseed onboarding")
        check(seeded.read_bytes() == seeded_bytes, "reseed leaves settings")
        try:
            require_jailbreak_acknowledgement(False)
            check(False, "jailbreak ack required")
        except SafetyError as exc:
            ack_text = str(exc)
            check("保修" in ack_text and "变砖" in ack_text and "官方固件" in ack_text, "jailbreak ack names risk and recovery")
            check(JAILBREAK_ACK_FLAG in ack_text, "jailbreak ack names the flag")
        require_jailbreak_acknowledgement(True)
        (ssh_root / "koreader" / "settings.reader.lua").write_text(
            'return {\n    ["font"] = "Droid",\n    ["SSH_autostart"] = true,\n    ["SSH_port"] = "2222",\n}\n',
            encoding="utf-8",
        )
        disable_koreader_ssh_autostart(ssh_root)
        ssh_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["font"] = "Droid"' in ssh_text, "ssh disable keeps settings")
        check('["SSH_autostart"] = false' in ssh_text, "ssh autostart cleared")
        check('["SSH_port"] = "2222"' in ssh_text, "ssh disable leaves port")
        check('["SSH_autostart"] = true' not in ssh_text, "ssh autostart true removed")
        legacy = root / "legacy-plugin-kindle"
        (legacy / "koreader").mkdir(parents=True)
        (legacy / "koreader" / "settings.reader.lua").write_text(
            'return {\n    ["SSH_autostart"] = true,\n    ["SSH_allow_no_password"] = true,\n    ["SSH_port"] = "2222",\n}\n',
            encoding="utf-8",
        )
        clear_usb_ssh_autostart(legacy)
        legacy_text = (legacy / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["SSH_autostart"] = false' in legacy_text, "plugin usb clears old ssh autostart")
        check('["SSH_autostart"] = true' not in legacy_text, "plugin usb removes ssh autostart true")
        check('["SSH_allow_no_password"] = true' in legacy_text, "plugin usb leaves other ssh flags")
        check('["SSH_port"] = "2222"' in legacy_text, "plugin usb leaves ssh port")
        check(
            "clear_usb_ssh_autostart(kindle)" in inspect.getsource(cmd_install_plugin),
            "install-plugin clears old ssh",
        )
        check(
            "clear_usb_ssh_autostart(kindle)" in inspect.getsource(install_simpleui_usb),
            "install-simpleui clears old ssh",
        )
        for wifi_command in (cmd_wifi_install_plugin, install_simpleui_wifi, prepare_home_wifi):
            check(
                "clear_usb_ssh_autostart" not in inspect.getsource(wifi_command),
                "wifi install leaves the running settings file",
            )
        helper_source = Path(__file__).read_text(encoding="utf-8")
        forbidden_autostart = '"SSH_autostart"' + ': "true"'
        check(forbidden_autostart not in helper_source, "source never sets ssh autostart")
        check("取消勾选" in ssh_enable_message(), "ssh enable says turn off later")
        check("取消勾选" in open_home_message(wifi=True), "wifi home says turn ssh off")
        check("取消勾选" not in open_home_message(wifi=False), "usb home leaves ssh note out")
        check("取消勾选" in wifi_done_message(["miuread"]), "wifi done says turn ssh off")
        check("取消勾选" in simpleui_message("v1", False, wifi=True), "simpleui wifi says turn ssh off")
        check("取消勾选" not in simpleui_message("v1", False, wifi=False), "simpleui usb leaves ssh note out")
        check(is_lan_ip("192.168.1.20") and is_lan_ip("10.0.0.2"), "lan ip")
        check(not is_lan_ip("8.8.8.8") and not is_lan_ip("127.0.0.1"), "reject public and loopback")
        check(str(scan_network("10.1.2.3", 8)) == "10.1.2.0/24", "collapse large subnet")
        check(remote_plugin_dir("miuread.koplugin") == "/mnt/us/koreader/plugins/miuread.koplugin", "remote plugin path")
        try:
            assert_lan_ip("1.1.1.1")
            check(False, "public ip raises")
        except SafetyError:
            check(True, "public ip raises")
        parsed = parse_ipconfig(
            "IPv4 Address. . . . . . . . . . . : 192.168.1.23\n"
            "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
            "   IPv4 地址 . . . . . . . . . . . . : 10.0.0.8\n"
            "   子网掩码  . . . . . . . . . . . . : 255.255.255.0\n"
        )
        check(parsed == [("192.168.1.23", 24, ""), ("10.0.0.8", 24, "")], "parse ipconfig")
        gbk_raw = (
            "   IPv4 地址 . . . . . . . . . . . . : 192.168.8.9\n"
            "   子网掩码  . . . . . . . . . . . . : 255.255.255.0\n"
        ).encode("gbk")
        check(parse_ipconfig(decode_console(gbk_raw)) == [("192.168.8.9", 24, "")], "parse gbk ipconfig")
        check(_is_virtual_adapter("以太网适配器 vEthernet (WSL)"), "virtual wsl")
        check(not _is_virtual_adapter("无线局域网适配器 WLAN"), "real wifi adapter")

        check(
            remote_plugin_dir(SIMPLEUI_DIR) == "/mnt/us/koreader/plugins/simpleui.koplugin",
            "remote simpleui path",
        )
        try:
            remote_plugin_dir("random.koplugin")
            check(False, "unknown plugin rejected")
        except SafetyError:
            check(True, "unknown plugin rejected")
        check(remote_plugin_dir(WEKINDLE_DIR).endswith("/wekindle.koplugin"), "wekindle remote dir")
        guide = (wekindle_source() / "main.lua").read_text(encoding="utf-8")
        check("homescreen_simpleui" in guide and "qr_login" in guide, "login guide plugin")
        check("quickstart-" in guide and "isQuickstartDocument" in guide, "login guide covers quickstart")
        login_sample = (
            "function QRLogin:_begin_protocol()\n"
            "    local login_cookies = { wr_fp = self.settings:get_device_fingerprint() }\n"
            "    local _, page_code, page_headers = self.client:request_follow({\n"
            "        url = SKILLS_PAGE_URL,"
        )
        patched_login = patch_weread_qr_login_text(login_sample)
        check(WEREAD_LOGIN_MARKER in patched_login and "login_cookies.wr_gid" in patched_login, "weread login patch")
        qr_sample = (
            "function QRLogin:_show_qr(uid, generation)\n"
            "    self.qr_dialog = dialog\n"
            "    UIManager:show(dialog)\n"
            "    self.host:refreshUI()\n"
            "function QRLogin:_poll(uid, generation, otp)\n"
            "        if is_timeout_error(result) then\n"
            "            self:_schedule_poll(uid, generation)\n"
            "            return\n"
            "        end\n"
        )
        patched_qr = patch_weread_qr_login_text(qr_sample)
        check("scheduleIn(0.2" in patched_qr and 'find("HTTP 401", 1, true)' in patched_qr, "weread qr and 401 patch")
        check(patch_weread_qr_login_text(patched_login) == patched_login, "weread login patch stays")
        check(patch_weread_qr_login_text(patched_qr) == patched_qr, "weread qr patch stays")
        check('saveSetting("SSH_autostart", false)' in guide, "wekindle clears ssh on next launch")
        check('saveSetting("SSH_autostart", true)' not in guide, "wekindle never enables ssh autostart")
        init_body = guide.split("function WeKindle:init()", 1)[1]
        check("self:disableSshAutostart()" in init_body, "wekindle init disables ssh")
        ensure_body = guide.split("function WeKindle:ensureHome()", 1)[1].split("function WeKindle:", 1)[0]
        check("SSH_autostart" not in ensure_body, "ssh off is not skipped with start_with")
        chosen, chosen_asset = select_release(
            [
                {"draft": True, "tag_name": "draft", "assets": [{"name": SIMPLEUI_ASSET}]},
                {"draft": False, "tag_name": "source-only", "assets": [{"name": "qiaomu-simpleui-main.zip"}]},
                {
                    "draft": False,
                    "prerelease": True,
                    "tag_name": "v3.0.0-qiaomu.3",
                    "assets": [{"name": SIMPLEUI_ASSET}],
                },
            ],
            is_simpleui_asset,
        )
        check(chosen["tag_name"] == "v3.0.0-qiaomu.3", "select prerelease simpleui asset")
        check(chosen_asset["name"] == SIMPLEUI_ASSET, "simpleui asset name")
        check(not is_simpleui_asset("qiaomu-simpleui-main.zip"), "reject source zip name")
        check(should_install_default_simpleui(False, False), "default simpleui when missing")
        check(not should_install_default_simpleui(True, False), "keep existing simpleui")
        check(not should_install_default_simpleui(False, True), "explicit skip simpleui")
        kept = plugin_bundle_message(
            "weread.koplugin",
            ["weread"],
            wifi=False,
            simpleui=None,
            error="",
            already_present=True,
        )
        check("没有覆盖" in kept and "要装吗" not in kept, "bundle keeps existing simpleui")
        bundled = plugin_bundle_message(
            "weread.koplugin",
            ["weread"],
            wifi=True,
            simpleui={"message": "乔木 SimpleUI v1 已写入。"},
            error="",
            already_present=False,
        )
        check("新主页" in bundled and "API Key" in bundled and "二维码" in bundled, "bundle opens home")
        try:
            select_release([{"draft": False, "assets": [{"name": "Source-code.zip"}]}], is_simpleui_asset)
            check(False, "source zip release rejected")
        except NetworkError:
            check(True, "source zip release rejected")
        digest = "a" * 64
        check(
            checksum_from_sums(f"{digest} *simpleui.koplugin.zip\n", SIMPLEUI_ASSET) == digest,
            "parse sha256sums",
        )
        check(
            valid_simpleui_backup("/mnt/us/koreader/backups/simpleui.koplugin-20261024-083011"),
            "backup path",
        )
        check(not valid_simpleui_backup("/mnt/us/koreader/plugins/simpleui.koplugin"), "backup path not plugin")
        check(
            not valid_simpleui_backup("/mnt/us/koreader/backups/simpleui.koplugin-20261024-083011;rm"),
            "backup path injection",
        )
        probe = "\n".join((
            "REV_BEGIN",
            "v2026.07.1-3-gabc",
            "REV_END",
            "COVER_OK",
            "PLUGIN_PRESENT",
            "SUI_PRESENT",
            "COVER_SETTING_BEGIN",
            "COVER_SETTING_END",
        ))
        facts = parse_simpleui_probe(probe)
        check(facts["version"] == "2026.07.1" and facts["settings_present"] is True, "parse simpleui probe")
        try:
            parse_simpleui_probe(probe.replace("v2026.07.1", "v2025.08"))
            check(False, "probe rejects old koreader")
        except SafetyError:
            check(True, "probe rejects old koreader")
        try:
            parse_simpleui_probe(
                probe.replace("COVER_SETTING_END", '["coverbrowser.koplugin"] = true,\nCOVER_SETTING_END')
            )
            check(False, "probe rejects disabled coverbrowser")
        except SafetyError:
            check(True, "probe rejects disabled coverbrowser")

        device = root / "simpleui-kindle"
        (device / "system").mkdir(parents=True)
        (device / "documents").mkdir()
        device_plugins = device / "koreader" / "plugins"
        (device_plugins / "coverbrowser.koplugin").mkdir(parents=True)
        (device / "koreader" / "git-rev").write_text("v2025.12-1-gabc\n", encoding="utf-8")
        try:
            require_simpleui_device(device)
            check(False, "reject old koreader on disk")
        except SafetyError:
            check(True, "reject old koreader on disk")
        (device / "koreader" / "git-rev").write_text("v2026.07.1-3-gabc_kindle\n", encoding="utf-8")
        (device_plugins / "coverbrowser.koplugin" / "disable").write_text("", encoding="utf-8")
        try:
            require_simpleui_device(device)
            check(False, "reject disabled coverbrowser file")
        except SafetyError:
            check(True, "reject disabled coverbrowser file")
        (device_plugins / "coverbrowser.koplugin" / "disable").unlink()
        settings = device / "koreader" / "settings" / "simpleui" / "sui_settings.lua"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(b"return { keep = true }\n")
        old_plugin = device_plugins / SIMPLEUI_DIR
        old_plugin.mkdir()
        (old_plugin / "main.lua").write_text("old-ui", encoding="utf-8")
        (old_plugin / "_meta.lua").write_text('version = "old"\n', encoding="utf-8")
        try:
            resolve_simpleui_source(str(old_plugin))
            check(False, "reject kindle-tree source")
        except SafetyError:
            check(True, "reject kindle-tree source")
        with tempfile.TemporaryDirectory() as other:
            source = Path(other) / SIMPLEUI_DIR
            source.mkdir()
            (source / "main.lua").write_text("new-ui", encoding="utf-8")
            (source / "_meta.lua").write_text('version = "3.0.0-qiaomu.3"\n', encoding="utf-8")
            (source / "sui_settings.lua").write_text("do-not-copy-to-settings", encoding="utf-8")
            check(resolve_simpleui_source(str(source.parent)) == source.resolve(), "resolve parent source")
            (device / "koreader" / "settings.reader.lua").write_text(
                'return {\n    ["font"] = "Droid",\n    ["SSH_autostart"] = true,\n'
                '    ["SSH_allow_no_password"] = true,\n    ["SSH_port"] = "2222",\n}\n',
                encoding="utf-8",
            )
            result = install_simpleui_usb(device, root / "simpleui-cache", source, "v3.0.0-qiaomu.3")
        check((device_plugins / SIMPLEUI_DIR / "main.lua").read_text(encoding="utf-8") == "new-ui", "simpleui replaced")
        check(settings.read_bytes() == b"return { keep = true }\n", "simpleui settings preserved")
        check((device_plugins / SIMPLEUI_DIR / "sui_settings.lua").is_file(), "plugin file stays in plugin")
        backup = Path(str(result["backup"]))
        check((backup / "main.lua").read_text(encoding="utf-8") == "old-ui", "simpleui backup")
        check(result["settings_preserved"] is True, "settings flag")
        check("新主页" in str(result["message"]), "simpleui message opens home")
        reader = (device / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["start_with"] = "homescreen_simpleui"' in reader, "install sets start_with")
        check('["SSH_autostart"] = false' in reader, "usb simpleui clears old ssh autostart")
        check('["SSH_autostart"] = true' not in reader, "usb simpleui removes ssh autostart true")
        check('["font"] = "Droid"' in reader, "usb simpleui keeps reader settings")
        check('["SSH_port"] = "2222"' in reader, "usb simpleui leaves ssh port")
        check((device / "koreader" / "wekindle_home").is_file(), "home marker")
        (device_plugins / "weread.koplugin").mkdir()
        (device_plugins / "weread.koplugin" / "main.lua").write_text("return {}", encoding="utf-8")
        prepare_home_usb(device)
        check((device_plugins / WEKINDLE_DIR / "main.lua").is_file(), "login guide installed")
        check(settings.read_bytes() == b"return { keep = true }\n", "guide keeps simpleui settings")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        check(port_open("127.0.0.1", listener.getsockname()[1], 0.5), "local port open")
    finally:
        listener.close()

    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if windows.exists():
        check(kindle_problem(windows) is not None, "reject windows directory")

    if failures:
        raise SafetyError("self-check 失败: " + ", ".join(failures))
    return {
        "ok": True,
        "command": "self-check",
        "message": "self-check 通过。基准机 KP3 / 5.16.2.1.1 会路由到 kindlepw2 和 coplate KUAL。",
        "reference": {
            "model": "Kindle Paperwhite 3 (7th gen, KP3)",
            "serial_prefix": "G090",
            "firmware": REFERENCE_FIRMWARE,
            **reference,
            "jailbreak_method": "winterbreak",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="把结果以 JSON 写到 stdout")
    common.add_argument("--path", help="Kindle 挂载路径，例如 E:\\ 或 /Volumes/Kindle")
    common.add_argument("--mirror", default=os.environ.get("KINDLE_GITHUB_MIRROR"), help="GitHub 下载镜像前缀")
    common.add_argument("--cache", help="下载缓存目录")
    common.add_argument("--firmware", help="version.txt 缺失时使用的固件版本")

    parser = argparse.ArgumentParser(
        prog="kindle_helper.py",
        description="检测 Kindle 并安装 KOReader、微信读书插件和默认的乔木 SimpleUI。写入前会确认目标是 Kindle 磁盘。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("detect", parents=[common], help="识别挂载的 Kindle、固件和越狱状态")
    clean = subparsers.add_parser("clean-ota", parents=[common], help="删除根目录里的升级残留")
    clean.add_argument("--include-hotfix", action="store_true", help="同时删除根目录里的 Hotfix bin")

    jailbreak = subparsers.add_parser("install-jailbreak", parents=[common], help="下载并写入越狱包")
    jailbreak.add_argument("--method", choices=["winterbreak", "winterbreak2", "languagebreak", "adbreak"])
    jailbreak.add_argument("--locale", default="zh-Hans-CN", help="LanguageBreak 完成包的语言")
    jailbreak.add_argument("--force", action="store_true")
    jailbreak.add_argument("--i-understand-this-wipes-the-kindle", action="store_true")
    jailbreak.add_argument(
        "--i-accept-jailbreak-risk",
        action="store_true",
        help="确认已知晓保修失效、变砖风险和恢复方法",
    )

    subparsers.add_parser("install-hotfix", parents=[common], help="把 Hotfix 写到 Kindle 根目录")

    kual = subparsers.add_parser("install-kual", parents=[common], help="部署 MRPI 和对应架构的 KUAL")
    kual.add_argument("--variant", choices=["coplate", "hardfloat"])
    kual.add_argument("--launcher", choices=["mrpi", "peki"], default="mrpi")
    kual.add_argument("--force", action="store_true")

    reader = subparsers.add_parser("install-koreader", parents=[common], help="下载并解压对应架构的 KOReader")
    reader.add_argument("--arch", required=True, choices=["kindlepw2", "kindlehf"])
    reader.add_argument("--force", action="store_true")

    plugin = subparsers.add_parser("install-plugin", parents=[common], help="安装微信读书插件，并默认装上乔木 SimpleUI")
    plugin.add_argument("--choice", required=True, choices=["weread", "miuread", "both"])
    plugin.add_argument("--no-simpleui", action="store_true", help="不安装默认的乔木 SimpleUI")

    scan = subparsers.add_parser("scan-wifi", parents=[common], help="扫描局域网里开着 KOReader SSH 的 Kindle")
    scan.add_argument("--ip", help="已知的 Kindle 局域网 IP，跳过扫描")
    scan.add_argument("--port", type=int, default=SSH_PORT, help="KOReader SSH 端口，默认 2222")

    wireless = subparsers.add_parser("wifi-install-plugin", parents=[common], help="通过 SSH/SFTP 无线安装插件，并默认装上乔木 SimpleUI")
    wireless.add_argument("--ip", required=True, help="Kindle 的局域网 IP")
    wireless.add_argument("--choice", required=True, choices=["weread", "miuread", "both"])
    wireless.add_argument("--port", type=int, default=SSH_PORT, help="KOReader SSH 端口，默认 2222")
    wireless.add_argument("--no-simpleui", action="store_true", help="不安装默认的乔木 SimpleUI")

    simple = subparsers.add_parser("install-simpleui", parents=[common], help="安装乔木 SimpleUI")
    simple.add_argument("--source", help="本地 simpleui.koplugin 目录，用来写回改过的界面")

    simple_wifi = subparsers.add_parser("wifi-install-simpleui", parents=[common], help="通过 SSH/SFTP 安装乔木 SimpleUI")
    simple_wifi.add_argument("--ip", required=True, help="Kindle 的局域网 IP")
    simple_wifi.add_argument("--port", type=int, default=SSH_PORT, help="KOReader SSH 端口，默认 2222")
    simple_wifi.add_argument("--source", help="本地 simpleui.koplugin 目录，用来写回改过的界面")

    subparsers.add_parser("restore-assets", parents=[common], help="恢复 AdBreak 修改前的 system/.assets")
    subparsers.add_parser("finalize", parents=[common], help="同步磁盘、删除下载缓存，并给出最终操作")
    subparsers.add_parser("self-check", parents=[common], help="离线检查路由、路径拒绝和压缩包安全逻辑")
    return parser


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def main(argv: Sequence[str] | None = None) -> None:
    _force_utf8_stdio()
    if sys.version_info < (3, 9):
        sys.stderr.write("需要 Python 3.9 或更高版本。\n")
        raise SystemExit(1)
    args = build_parser().parse_args(argv)
    try:
        args.mirror = clean_mirror(args.mirror)
    except SafetyError as exc:
        emit(args, {"ok": False, "error": str(exc), "code": "safety"}, 2)
    commands = {
        "detect": cmd_detect,
        "clean-ota": cmd_clean_ota,
        "install-jailbreak": cmd_install_jailbreak,
        "install-hotfix": cmd_install_hotfix,
        "install-kual": cmd_install_kual,
        "install-koreader": cmd_install_koreader,
        "install-plugin": cmd_install_plugin,
        "scan-wifi": cmd_scan_wifi,
        "wifi-install-plugin": cmd_wifi_install_plugin,
        "install-simpleui": cmd_install_simpleui,
        "wifi-install-simpleui": cmd_wifi_install_simpleui,
        "restore-assets": cmd_restore_assets,
        "finalize": cmd_finalize,
        "self-check": cmd_self_check,
    }
    try:
        payload = commands[args.command](args)
    except SafetyError as exc:
        emit(args, {"ok": False, "error": str(exc), "code": "safety"}, 2)
    except NetworkError as exc:
        emit(
            args,
            {
                "ok": False,
                "error": str(exc),
                "code": "network",
                "message": "下载失败。可以重试，或加上 GitHub 镜像，例如 --mirror https://ghfast.top/",
            },
            3,
        )
    except Exception as exc:
        log(f"未预期的错误: {type(exc).__name__}: {exc}")
        emit(args, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "code": "error"}, 1)
    emit(args, payload, 0)


if __name__ == "__main__":
    main()
