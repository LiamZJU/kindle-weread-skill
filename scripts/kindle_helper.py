#!/usr/bin/env python3
"""Kindle mount detection and package installer for kindle-weread-skill.

Standard library, with optional paramiko for Wi-Fi uploads when OpenSSH is absent.
Python 3.9+. Logs go to stderr. Use --json for the machine-readable result on stdout.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
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
SSH_LUA_SETTINGS = {
    "SSH_port": '"2222"',
    "SSH_allow_no_password": "true",
    "SSH_autostart": "true",
    "SSH_key_only_auth": "false",
}

# Verified 2026-10-21 against kindlemodding.org and the upstream release archives.
# Softfloat MRPI is NiLuJe's classic 1.7.N package. The khf archive is only for
# firmware >= 5.16.3. The coplate-era KUAL booklet is the f190a38 snapshot.
MRPI_URLS = {
    "coplate": (
        "https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/"
        "installing-kual-mrpi/kual-mrinstaller-1.7.N-r19303.zip"
    ),
    "hardfloat": (
        "https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/"
        "installing-kual-mrpi/kual-mrinstaller-khf.zip"
    ),
}
KUAL_URLS = {
    "coplate": (
        "https://storage.gra.cloud.ovh.net/v1/AUTH_2ac4bfee353948ec8ea7fd1710574097/"
        "mr-public/KUAL/KUAL-f190a38-20240104.tar.xz"
    ),
    "hardfloat": (
        "https://github.com/yparitcher/KUAL_Booklet/releases/download/V1.4/"
        "KOL-V1.4.2-g3795770-20231106-Frameworkless.tar.xz"
    ),
}
PEKI_URL = "https://github.com/KindleTweaks/PEKI/releases/latest/download/PEKI.zip"

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

GITHUB_REPOS = {
    "winterbreak": "KindleModding/WinterBreak",
    "winterbreak2": "KindleModding/Winterbreak2",
    "languagebreak": "notmarek/LanguageBreak",
    "adbreak": "KindleModding/AdBreak",
    "hotfix": "KindleModding/Hotfix",
    "koreader": "koreader/koreader",
    "weread": "finlater/weread.koplugin",
    "miuread": "miumiupy98-art/miuread-koreader",
}


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


def request_bytes(url: str, timeout: int, mirror: str | None, headers: dict[str, str] | None = None) -> bytes:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    last_error: Exception | None = None
    for candidate in mirror_candidates(url, mirror):
        for attempt in range(3):
            try:
                request = urllib.request.Request(candidate, headers=merged)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except Exception as exc:
                last_error = exc
                if not should_retry(exc):
                    break
                delay = 2 ** attempt
                log(f"请求失败，{delay} 秒后重试 ({attempt + 1}/3): {exc}")
                time.sleep(delay)
    raise NetworkError(f"下载失败: {url} ({last_error})")


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


def github_latest(repo: str, mirror: str | None) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    raw = request_bytes(
        url,
        timeout=30,
        mirror=mirror,
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NetworkError(f"GitHub API 返回的不是 JSON: {repo}") from exc
    if "assets" not in payload:
        message = payload.get("message", "未知错误")
        raise NetworkError(f"读取 {repo} 最新 Release 失败: {message}")
    return payload


def choose_asset(release: dict, predicate: Callable[[str], bool], prefer: str | None = None) -> dict:
    matched = [asset for asset in release.get("assets", []) if predicate(asset.get("name", ""))]
    if prefer:
        preferred = [asset for asset in matched if prefer in asset.get("name", "").lower()]
        if preferred:
            matched = preferred
    if not matched:
        tag = release.get("tag_name", "unknown")
        raise NetworkError(f"在 Release {tag} 里没有找到需要的安装包。")
    return matched[0]


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


def download_named_release(repo: str, cache: Path, mirror: str | None, predicate: Callable[[str], bool], prefer: str | None = None) -> tuple[dict, Path]:
    release = github_latest(repo, mirror)
    asset = choose_asset(release, predicate, prefer)
    dest = cache / "downloads" / asset["name"]
    if dest.is_file() and dest.stat().st_size == asset.get("size", dest.stat().st_size):
        log(f"使用已缓存的 {dest.name}")
    else:
        download_file(asset["browser_download_url"], dest, mirror)
    sidecar_name = asset["name"] + ".sha256"
    sidecar = next((item for item in release.get("assets", []) if item.get("name") == sidecar_name), None)
    if sidecar:
        sidecar_path = cache / "downloads" / sidecar_name
        download_file(sidecar["browser_download_url"], sidecar_path, mirror)
        digest = sidecar_path.read_text(encoding="utf-8", errors="replace").strip().split()[0]
        actual = sha256_file(dest)
        if actual.lower() != digest.lower():
            dest.unlink(missing_ok=True)
            raise SafetyError(f"{dest.name} 的 SHA-256 校验失败。")
        log(f"SHA-256 校验通过: {dest.name}")
    return release, dest


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
        write_state(cache, hotfix_path=str(cached_hotfix), hotfix_name=cached_hotfix.name, jailbreak_method=method)
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


def cache_universal_hotfix(cache: Path, mirror: str | None) -> Path:
    release, archive = download_named_release(
        GITHUB_REPOS["hotfix"],
        cache,
        mirror,
        lambda name: name.lower() == "update_hotfix_universal.bin" or (
            "universal" in name.lower() and name.lower().endswith(".bin")
        ),
    )
    del release
    cached = cache / "hotfix" / archive.name
    cached.parent.mkdir(parents=True, exist_ok=True)
    if archive.resolve() != cached.resolve():
        shutil.copy2(archive, cached)
    write_state(cache, hotfix_path=str(cached), hotfix_name=cached.name)
    return cached


def select_install_bin(extracted: Path) -> Path:
    bins = [path for path in extracted.rglob("*.bin") if "uninstall" not in path.name.lower()]
    primary = [path for path in bins if "hotfix" not in path.name.lower()]
    chosen = primary or bins
    if len(chosen) != 1:
        names = ", ".join(path.name for path in chosen)
        raise SafetyError(f"无法确定要安装的 KUAL 安装包: {names}")
    return chosen[0]


def install_peki(kindle: Path, cache: Path, mirror: str | None) -> dict[str, object]:
    archive = cache / "downloads" / "PEKI.zip"
    download_file(PEKI_URL, archive, mirror)
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


def fetch_plugin_source(choice: str, cache: Path, mirror: str | None) -> tuple[str, str, Path]:
    folder_name = PLUGIN_DIRS[choice]
    if choice == "weread":
        predicate = lambda name: name.lower().endswith(".zip") and "weread.koplugin" in name.lower()
        prefer = None
    else:
        predicate = lambda name: (
            name.lower().endswith(".zip") and "miuread" in name.lower() and "beta" not in name.lower()
        )
        prefer = "full"
    release, archive = download_named_release(GITHUB_REPOS[choice], cache, mirror, predicate, prefer)
    source = plugin_source(fresh_extract(archive, cache), folder_name)
    return folder_name, str(release.get("tag_name")), source


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


def preset_koreader_ssh(kindle: Path) -> None:
    """Write KOReader's own SSH settings so the server can start on the next launch."""
    directory = kindle / "koreader"
    if not directory.is_dir():
        raise SafetyError("没有 koreader 目录，不能预置 SSH。")
    settings = directory / "settings.reader.lua"
    if not settings.exists():
        lines = ["-- koreader/settings.reader.lua", "return {"]
        for key, value in SSH_LUA_SETTINGS.items():
            lines.append(f'    ["{key}"] = {value},')
        lines.append("}")
        settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    text = settings.read_text(encoding="utf-8", errors="replace")
    for key, value in SSH_LUA_SETTINGS.items():
        text, changed = _replace_lua_setting(text, key, value)
        if changed:
            continue
        brace = text.rfind("}")
        if brace == -1:
            raise SafetyError("settings.reader.lua 无法识别，没有改动 SSH 设置。请在 KOReader 里手动开启 SSH。")
        text = text[:brace] + f'    ["{key}"] = {value},\n' + text[brace:]
    settings.write_text(text, encoding="utf-8")


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
    encodings = ["oem", "mbcs", "gbk", "utf-8"] if os.name == "nt" else ["utf-8"]
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
    if folder_name not in PLUGIN_DIRS.values() or not re.fullmatch(r"[A-Za-z0-9._-]+", folder_name):
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
            "无线登录失败。请在 KOReader 顶部菜单 → 网络 → SSH 服务器中勾选 SSH 服务器，并勾选允许无密码登录。"
            f" 详细信息: {exc}"
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


def ssh_enable_message(ip: str | None = None) -> str:
    target = f"{ip} 的 " if ip else ""
    return (
        f"还连不上{target}{SSH_PORT} 端口。请打开 KOReader，进入顶部菜单 → 网络 → SSH 服务器，"
        "勾选 SSH 服务器，并勾选允许无密码登录。确认和电脑在同一个 Wi-Fi 后，回复「SSH 已开」。"
    )


def login_hint(plugins: Sequence[str]) -> str:
    sentences = ["安全弹出 Kindle。打开 KUAL，启动 KOReader。"]
    if "weread" in plugins:
        sentences.append(
            "微信读书：先在手机微信读书打开「我 → 设置 → 微信读书 Skill」获取 API Key，"
            "再点 KOReader 顶部工具栏里的微信读书扫码。"
        )
    if "miuread" in plugins:
        sentences.append("觅阅：点 KOReader 顶部工具栏里的觅阅，按提示扫码。书架管理会再要求一次授权。")
    if not plugins:
        sentences.append("然后从顶部工具栏进入已经安装的微信读书插件并扫码登录。")
    return "".join(sentences)


def wifi_done_message(plugins: Sequence[str]) -> str:
    sentences = ["插件已经通过 Wi-Fi 传到 Kindle。请在 KOReader 里完全退出并重新打开。"]
    if "weread" in plugins:
        sentences.append(
            "微信读书：先在手机微信读书打开「我 → 设置 → 微信读书 Skill」获取 API Key，再从顶部工具栏扫码。"
        )
    if "miuread" in plugins:
        sentences.append("觅阅：从顶部工具栏打开觅阅并按提示扫码。书架管理会再要求一次授权。")
    if not plugins:
        sentences.append("然后从顶部工具栏进入插件并扫码登录。")
    return "".join(sentences)


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
    kindle = require_kindle(args)
    firmware = require_firmware(kindle, args)
    method = args.method or recommend_method(firmware)
    validate_method(method, firmware, args.force, args.i_understand_this_wipes_the_kindle)
    require_space(kindle, 80)
    cache = cache_dir(args)
    removed = clean_ota(kindle)
    repo = GITHUB_REPOS[method]
    if method == "winterbreak2":
        predicate = lambda name: name.lower() == "wb2.zip"
    elif method == "languagebreak":
        predicate = lambda name: name.lower().endswith((".tar.gz", ".tgz", ".zip"))
    elif method == "adbreak":
        predicate = lambda name: "adbreak" in name.lower() and name.lower().endswith(".zip")
    else:
        predicate = lambda name: name.lower().endswith((".tar.gz", ".tgz", ".zip"))
    prefer = ".tar.gz" if method in {"winterbreak", "languagebreak"} else None
    release, archive = download_named_release(repo, cache, args.mirror, predicate, prefer)
    if method != "languagebreak":
        cache_universal_hotfix(cache, args.mirror)
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
        "release": release.get("tag_name"),
        "removed_ota": removed,
        "user_steps": steps,
        "message": message,
        **routing,
        **details,
    }


def cmd_install_hotfix(args: argparse.Namespace) -> dict:
    kindle = require_kindle(args)
    cache = cache_dir(args)
    state = read_state(cache)
    hotfix = Path(str(state["hotfix_path"])) if state.get("hotfix_path") else None
    if hotfix is None or not hotfix.is_file():
        hotfix = cache_universal_hotfix(cache, args.mirror)
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
    mrpi_archive = cache / "downloads" / Path(MRPI_URLS[variant]).name
    kual_archive = cache / "downloads" / Path(KUAL_URLS[variant]).name
    download_file(MRPI_URLS[variant], mrpi_archive, args.mirror)
    download_file(KUAL_URLS[variant], kual_archive, args.mirror)
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
    pattern = re.compile(rf"^koreader-{re.escape(args.arch)}-v.+\.zip$", re.IGNORECASE)
    release, archive = download_named_release(
        GITHUB_REPOS["koreader"],
        cache,
        args.mirror,
        lambda name: bool(pattern.match(name)),
    )
    extracted = fresh_extract(archive, cache)
    copied = merge_tree(koreader_root(extracted), kindle)
    launcher = kindle / "extensions" / "koreader"
    program = kindle / "koreader"
    if not program.is_dir() or not launcher.is_dir():
        raise SafetyError("解压后没有同时得到 koreader 和 extensions/koreader。Kindle 上的半成品请先不要使用。")
    ssh_preset = True
    ssh_error = ""
    try:
        preset_koreader_ssh(kindle)
    except SafetyError as exc:
        ssh_preset = False
        ssh_error = str(exc)
        log(ssh_error)
    sync_kindle(kindle)
    write_state(cache, koreader_arch=args.arch, koreader_release=release.get("tag_name"), ssh_preset=ssh_preset)
    if ssh_preset:
        ssh_note = "SSH 已预置为端口 2222，会随 KOReader 启动，并允许无密码登录。"
    else:
        ssh_note = "SSH 没能写入配置，稍后需要在 KOReader 里手动开启。"
    return {
        "ok": True,
        "command": "install-koreader",
        "status": "installed",
        "arch": args.arch,
        "release": release.get("tag_name"),
        "copied_files": copied,
        "ssh_preset": ssh_preset,
        "ssh_error": ssh_error,
        "message": (
            f"KOReader {release.get('tag_name')}（{args.arch}）已写入 Kindle。{ssh_note}"
            "先不要弹出。要装哪一个插件？A 是 weread，轻量；B 是觅阅，功能更多。也可以两个都装。"
        ),
    }


def cmd_install_plugin(args: argparse.Namespace) -> dict:
    choices = plugin_choices(args.choice)
    kindle = require_kindle(args)
    if not (kindle / "koreader" / "plugins").is_dir():
        raise SafetyError("还没有安装 KOReader，不能先装插件。")
    require_space(kindle, 40)
    cache = cache_dir(args)
    installed: list[dict[str, str]] = []
    for choice in choices:
        folder_name, release, source = fetch_plugin_source(choice, cache, args.mirror)
        dest = deploy_plugin(kindle, source, folder_name)
        installed.append({"choice": choice, "release": release, "path": str(dest)})
    sync_kindle(kindle)
    write_state(cache, plugins=remember_plugins(cache, choices), deploy_mode="usb")
    names = "、".join(PLUGIN_DIRS[choice] for choice in choices)
    return {
        "ok": True,
        "command": "install-plugin",
        "status": "installed",
        "installed": installed,
        "message": f"已装好 {names}。可以继续收尾并安全弹出。",
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
    return {
        "ok": True,
        "command": "wifi-install-plugin",
        "status": "installed",
        "transport": transport,
        "kindle_ip": ip,
        "port": port,
        "installed": installed,
        "message": wifi_done_message(plugins),
    }


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
    if state.get("deploy_mode") == "wifi" and kindle is None:
        message = wifi_done_message(plugin_names)
    else:
        message = login_hint(plugin_names)
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
        preset_koreader_ssh(ssh_root)
        ssh_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["SSH_port"] = "2222"' in ssh_text, "ssh port preset")
        check('["SSH_allow_no_password"] = true' in ssh_text, "ssh no password")
        check('["SSH_autostart"] = true' in ssh_text, "ssh autostart")
        check('["SSH_key_only_auth"] = false' in ssh_text, "ssh key auth off")
        (ssh_root / "koreader" / "settings.reader.lua").write_text(
            'return {\n    ["font"] = "Droid",\n    ["SSH_port"] = "22",\n}\n',
            encoding="utf-8",
        )
        preset_koreader_ssh(ssh_root)
        ssh_text = (ssh_root / "koreader" / "settings.reader.lua").read_text(encoding="utf-8")
        check('["font"] = "Droid"' in ssh_text, "ssh preset keeps settings")
        check('["SSH_port"] = "2222"' in ssh_text, "ssh preset updates port")
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
        description="检测 Kindle 并安装 KOReader 与微信读书插件。写入前会确认目标是 Kindle 磁盘。",
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

    subparsers.add_parser("install-hotfix", parents=[common], help="把 Hotfix 写到 Kindle 根目录")

    kual = subparsers.add_parser("install-kual", parents=[common], help="部署 MRPI 和对应架构的 KUAL")
    kual.add_argument("--variant", choices=["coplate", "hardfloat"])
    kual.add_argument("--launcher", choices=["mrpi", "peki"], default="mrpi")
    kual.add_argument("--force", action="store_true")

    reader = subparsers.add_parser("install-koreader", parents=[common], help="下载并解压对应架构的 KOReader")
    reader.add_argument("--arch", required=True, choices=["kindlepw2", "kindlehf"])
    reader.add_argument("--force", action="store_true")

    plugin = subparsers.add_parser("install-plugin", parents=[common], help="安装微信读书插件")
    plugin.add_argument("--choice", required=True, choices=["weread", "miuread", "both"])

    scan = subparsers.add_parser("scan-wifi", parents=[common], help="扫描局域网里开着 KOReader SSH 的 Kindle")
    scan.add_argument("--ip", help="已知的 Kindle 局域网 IP，跳过扫描")
    scan.add_argument("--port", type=int, default=SSH_PORT, help="KOReader SSH 端口，默认 2222")

    wireless = subparsers.add_parser("wifi-install-plugin", parents=[common], help="通过 SSH/SFTP 无线安装插件")
    wireless.add_argument("--ip", required=True, help="Kindle 的局域网 IP")
    wireless.add_argument("--choice", required=True, choices=["weread", "miuread", "both"])
    wireless.add_argument("--port", type=int, default=SSH_PORT, help="KOReader SSH 端口，默认 2222")

    subparsers.add_parser("restore-assets", parents=[common], help="恢复 AdBreak 修改前的 system/.assets")
    subparsers.add_parser("finalize", parents=[common], help="同步磁盘、删除下载缓存，并给出最终操作")
    subparsers.add_parser("self-check", parents=[common], help="离线检查路由、路径拒绝和压缩包安全逻辑")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
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
