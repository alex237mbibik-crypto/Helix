"""Автообновление Windows-сборки с GitHub Releases.

Пока программа запущена, exe нельзя перезаписать. Новая сборка качается
в фоне, затем короткий перезапуск ставит файлы на место.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from sheets_hub.config import app_root, user_data_dir

GITHUB_REPO = (os.environ.get("SHEETS_HUB_GITHUB") or "alex237mbibik-crypto/Helix").strip()
ASSET_NAME = "SheetsHub-Windows.zip"


@dataclass
class LocalVersion:
    version: str
    build: int
    frozen: bool


def local_version() -> LocalVersion:
    frozen = bool(getattr(sys, "frozen", False))
    version = "0.1.0"
    build = 0
    try:
        from sheets_hub import __version__ as pkg_ver

        version = str(pkg_ver or version)
    except Exception:
        pass
    path = app_root() / "version.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            version = str(data.get("version") or version)
            build = int(data.get("build") or 0)
        except Exception:
            pass
    return LocalVersion(version=version, build=build, frozen=frozen)


def _updates_dir() -> Path:
    path = user_data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _headers() -> dict[str, str]:
    return {"User-Agent": "SheetsHub-Updater", "Accept": "application/vnd.github+json"}


def _curl_bytes(url: str, dest: Path | None = None, *, timeout: int = 30) -> bytes:
    """GitHub через curl: на Windows запрос из Python часто не доходит, и кнопка не появляется."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise OSError("curl не найден")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    cmd = [
        curl,
        "-fsSL",
        "-k",
        "--max-time",
        str(timeout),
        "--connect-timeout",
        "8",
        "-H",
        "User-Agent: SheetsHub-Updater",
        "-H",
        "Accept: application/vnd.github+json",
        "-L",
        url,
    ]
    if dest is not None:
        cmd.extend(["-o", str(dest)])
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout + 5,
        creationflags=flags,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"curl завершился с кодом {result.returncode}")
    if dest is not None:
        return b""
    return result.stdout or b""


def _github_json(url: str) -> dict[str, Any]:
    try:
        raw = _curl_bytes(url, timeout=20)
        data = json.loads(raw.decode("utf-8-sig"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    data = requests.get(url, timeout=20, headers=_headers()).json()
    return data if isinstance(data, dict) else {}


def _parse_build(tag: str, body: str = "", name: str = "") -> int:
    """Номер сборки берём из тега и названия. В тексте релиза есть ссылка
    compare/b107...b108 — оттуда нельзя брать первое число, это предыдущая сборка.
    """
    for text in (tag or "", name or ""):
        match = re.search(r"\bb(\d{1,9})\b", text, re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"build\s+(\d{1,9})", text, re.I)
        if match:
            return int(match.group(1))
    found = [int(item) for item in re.findall(r"\bb(\d{1,9})\b", body or "", re.I)]
    if found:
        return max(found)
    digits = re.findall(r"\d+", tag or "")
    if digits:
        return int(digits[-1])
    return 0


def fetch_latest_release() -> dict[str, Any]:
    override = (os.environ.get("SHEETS_HUB_UPDATE_JSON") or "").strip()
    if override:
        data = requests.get(override, timeout=20, headers=_headers()).json()
        return data if isinstance(data, dict) else {}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    data = _github_json(url)
    if not isinstance(data, dict):
        raise RuntimeError("Неожиданный ответ GitHub")
    if str(data.get("message") or "").lower() == "not found":
        return {"tag_name": "b0", "assets": [], "name": ""}
    if data.get("message"):
        raise RuntimeError(str(data.get("message")))
    return data


def _asset_url(release: dict[str, Any]) -> str:
    env_url = (os.environ.get("SHEETS_HUB_UPDATE_ZIP") or "").strip()
    if env_url:
        return env_url
    for asset in release.get("assets") or []:
        name = str((asset or {}).get("name") or "")
        url = str((asset or {}).get("browser_download_url") or "")
        if name == ASSET_NAME and url:
            return url
    for asset in release.get("assets") or []:
        url = str((asset or {}).get("browser_download_url") or "")
        if url.endswith(".zip"):
            return url
    raise RuntimeError("В релизе нет zip-сборки SheetsHub-Windows.zip")


def inspect_latest() -> dict[str, Any]:
    current = local_version()
    out: dict[str, Any] = {
        "ok": True,
        "status": "current",
        "current": current.version,
        "build": current.build,
        "frozen": current.frozen,
        "latest": current.version,
        "latest_build": current.build,
        "notes": "",
        "error": "",
    }
    try:
        release = fetch_latest_release()
    except Exception as exc:
        out["status"] = "error"
        out["error"] = str(exc).split("\n")[0]
        return out
    tag = str(release.get("tag_name") or "")
    release_name = str(release.get("name") or "")
    latest_build = _parse_build(tag, str(release.get("body") or ""), release_name)
    out["latest"] = release_name or tag or current.version
    out["latest_build"] = latest_build
    out["tag"] = tag
    out["notes"] = str(release.get("body") or "")[:800]
    if latest_build > current.build:
        out["status"] = "available"
        try:
            out["zip_url"] = _asset_url(release)
        except Exception as exc:
            out["status"] = "error"
            out["error"] = str(exc)
    return out


def _payload_dir(extracted: Path) -> Path:
    exe_here = list(extracted.glob("SheetsHub.exe"))
    if exe_here:
        return extracted
    nested = [p for p in extracted.iterdir() if p.is_dir()]
    for folder in nested:
        if (folder / "SheetsHub.exe").exists():
            return folder
    raise RuntimeError("В архиве нет SheetsHub.exe")


def download_latest(progress: dict[str, Any] | None = None) -> Path:
    info = inspect_latest()
    if info.get("status") != "available":
        raise RuntimeError(info.get("error") or "Нет новой версии")
    zip_url = str(info.get("zip_url") or "")
    if not zip_url:
        raise RuntimeError("Нет ссылки на сборку")
    dest_zip = _updates_dir() / "pending.zip"
    dest_dir = _updates_dir() / "pending"
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if progress is not None:
        progress["status"] = "downloading"
        progress["error"] = ""
        progress["percent"] = 0
    try:
        _curl_bytes(zip_url, dest_zip, timeout=180)
        if progress is not None:
            progress["percent"] = 100
    except Exception:
        with requests.get(zip_url, stream=True, timeout=120, headers=_headers()) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with dest_zip.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None and total:
                        progress["percent"] = int(done * 100 / total)
    with zipfile.ZipFile(dest_zip) as zf:
        zf.extractall(dest_dir)
    payload = _payload_dir(dest_dir)
    for name in ("config.yaml", "credentials.json", "token.json"):
        junk = payload / name
        if junk.exists():
            junk.unlink()
    fresh = inspect_latest()
    fresh_build = int(fresh.get("latest_build") or 0)
    if fresh_build > int(info.get("latest_build") or 0):
        raise RuntimeError("На GitHub появилась более новая сборка")
    ready = _updates_dir() / "ready"
    if ready.exists():
        shutil.rmtree(ready, ignore_errors=True)
    shutil.copytree(payload, ready)
    marker = _updates_dir() / "ready.json"
    marker.write_text(
        json.dumps(
            {"build": info.get("latest_build"), "tag": info.get("tag"), "when": time.time()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if progress is not None:
        progress["status"] = "ready"
        progress["percent"] = 100
        progress["latest"] = info.get("latest")
        progress["latest_build"] = info.get("latest_build")
    return ready


def pending_ready() -> bool:
    return (_updates_dir() / "ready" / "SheetsHub.exe").exists()


def pending_build() -> int:
    if not pending_ready():
        return 0
    marker = _updates_dir() / "ready.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return int(data.get("build") or 0)
    except Exception:
        return 0


def discard_pending() -> None:
    root = _updates_dir()
    for name in ("ready", "pending"):
        path = root / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    for name in ("pending.zip", "ready.json"):
        path = root / name
        if path.exists():
            path.unlink(missing_ok=True)


def _apply_script(src: Path, dst: Path, exe: Path) -> str:
    src_s = str(src)
    dst_s = str(dst)
    exe_s = str(exe)
    return f"""@echo off
chcp 65001 >nul
timeout /t 2 /nobreak >nul
robocopy "{src_s}" "{dst_s}" /E /R:4 /W:2 /NFL /NDL /NJH /NJS /nc /ns /np /XF credentials.json token.json config.yaml /XD webview_data SheetsHub_data updates >nul
start "" "{exe_s}"
"""


def schedule_apply_and_restart() -> None:
    src = _updates_dir() / "ready"
    if not (src / "SheetsHub.exe").exists():
        raise RuntimeError("Обновление ещё не скачано")
    dst = app_root()
    exe = dst / "SheetsHub.exe"
    if not exe.exists():
        exe = Path(sys.executable)
    bat = Path(tempfile.gettempdir()) / "sheetshub_apply_update.bat"
    bat.write_text(_apply_script(src, dst, exe), encoding="utf-8")
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        cwd=str(dst),
        creationflags=flags,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class UpdateController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "status": "idle",
            "percent": 0,
            "error": "",
            **asdict(local_version()),
            "latest": "",
            "latest_build": 0,
        }
        self._started = False
        self._downloading = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self.state)
        local = local_version()
        data["current"] = local.version
        data["build"] = local.build
        data["frozen"] = local.frozen
        ready_build = pending_build()
        latest_build = int(data.get("latest_build") or 0)
        # Скачанный архив старее GitHub — это не то обновление, которое нужно ставить.
        if ready_build and latest_build and ready_build < latest_build:
            ready_build = 0
        if (
            ready_build
            and data.get("status") in {"idle", "current", "available", "downloading"}
            and data.get("status") != "downloading"
        ):
            data["status"] = "ready"
            data["latest_build"] = ready_build
        return data

    def _merge(self, **kwargs: Any) -> None:
        with self._lock:
            self.state.update(kwargs)

    def start_background_check(self) -> None:
        if self._started:
            return
        self._started = True
        auto_download = sys.platform == "win32" and local_version().frozen

        def work() -> None:
            time.sleep(2)
            while True:
                try:
                    self.check(download=auto_download)
                except Exception:
                    pass
                time.sleep(90)

        threading.Thread(target=work, daemon=True).start()

    def check(self, *, download: bool = False) -> dict[str, Any]:
        info = inspect_latest()
        if info.get("status") == "error":
            with self._lock:
                prev_latest = int(self.state.get("latest_build") or 0)
                prev_build = int(self.state.get("build") or 0)
                prev_name = str(self.state.get("latest") or "")
            if prev_latest > prev_build:
                info["status"] = "available"
                info["latest_build"] = prev_latest
                info["latest"] = prev_name or info.get("latest")
                info["error"] = ""
        latest_build = int(info.get("latest_build") or 0)
        ready_build = pending_build()
        if ready_build and latest_build and ready_build < latest_build:
            discard_pending()
            ready_build = 0
        self._merge(**{k: v for k, v in info.items() if k != "notes"})
        if ready_build and latest_build and ready_build >= latest_build and info.get("status") == "available":
            self._merge(status="ready", percent=100, latest_build=ready_build)
            return self.snapshot()
        if info.get("status") == "available" and download and not self._downloading:
            self._downloading = True
            self._merge(status="downloading", percent=0, error="")
            threading.Thread(target=self._download_bg, daemon=True).start()
        return self.snapshot()

    def _download_bg(self) -> None:
        try:
            download_latest(self.state)
        except Exception as exc:
            self._merge(status="error", error=str(exc).split("\n")[0])
        finally:
            self._downloading = False

    def apply(self) -> dict[str, Any]:
        # Сначала сверить номер с GitHub: скачанная 107 не должна ставиться, если уже есть 108.
        self.check(download=False)
        ready = pending_build()
        latest = int(self.state.get("latest_build") or 0)
        if ready and latest and ready < latest:
            discard_pending()
            ready = 0
        if not ready:
            self.check(download=True)
            raise RuntimeError(
                "Скачиваю последнюю сборку с GitHub. "
                "Нажмите ещё раз, когда кнопка станет «Обновить и перезапустить»."
            )
        schedule_apply_and_restart()
        self._merge(status="restarting")
        return self.snapshot()
