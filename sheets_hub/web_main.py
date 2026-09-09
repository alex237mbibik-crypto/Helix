"""Запуск Sheets Hub с HTML-интерфейсом Helix_Front (pywebview)."""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

from sheets_hub.config import app_root, user_data_dir, writable_data_dir
from sheets_hub.ssl_setup import configure_tls
from sheets_hub.web_api import HelixApi


def _ui_index() -> Path:
    here = Path(__file__).resolve().parent / "webui" / "index.html"
    if here.exists():
        return here
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for candidate in (
            meipass / "webui" / "index.html",
            Path(sys.executable).resolve().parent / "webui" / "index.html",
            meipass / "sheets_hub" / "webui" / "index.html",
        ):
            if candidate.exists():
                return candidate
    raise FileNotFoundError("Не найден webui/index.html")


def _is_temp_windows_profile() -> bool:
    """Временные профили Windows (TEMP.xxx) часто без нормальной записи в AppData."""
    if sys.platform != "win32":
        return False
    markers = (
        os.environ.get("USERNAME") or "",
        os.environ.get("USERPROFILE") or "",
        str(Path.home()),
        os.environ.get("LOCALAPPDATA") or "",
    )
    blob = " ".join(markers).upper()
    return "TEMP." in blob or "\\TEMP\\" in blob or "/TEMP/" in blob


def _probe_dir(path: Path) -> bool:
    """Проверка, что каталог и подпапка EBWebView реально пишутся."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        eb = path / "EBWebView"
        eb.mkdir(parents=True, exist_ok=True)
        for folder in (path, eb):
            probe = folder / ".sheets_hub_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _webview_data_dir() -> Path:
    """Каталог Edge WebView2: несколько запасных путей для киосков/временных профилей."""
    temp_root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
    program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
    candidates: list[Path] = []

    # Временный профиль → не кладём WebView в LOCALAPPDATA (там Edge падает).
    if _is_temp_windows_profile() or getattr(sys, "frozen", False):
        candidates.extend(
            [
                app_root() / "webview_data",
                writable_data_dir() / "webview_data",
                temp_root / "SheetsHub" / "webview",
                program_data / "SheetsHub" / "webview",
            ]
        )

    candidates.extend(
        [
            user_data_dir() / "webview",
            writable_data_dir() / "webview_data",
            app_root() / "webview_data",
            temp_root / "SheetsHub" / "webview",
            program_data / "SheetsHub" / "webview",
            Path.home() / "SheetsHub" / "webview",
        ]
    )

    seen: set[str] = set()
    tried: list[str] = []
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        tried.append(str(path))
        if _probe_dir(path):
            return path

    detail = "\n".join(f"• {p}" for p in tried[:8])
    raise PermissionError(
        "Не удалось создать каталог данных для Microsoft Edge WebView2.\n\n"
        "Частая причина — временный профиль Windows (TEMP.…),\n"
        "у которого нет нормальной записи в AppData.\n\n"
        "Что сделать:\n"
        "1) Запустите SheetsHub из папки, куда можно писать (не Program Files),\n"
        "   либо положите программу на рабочий стол / диск D:.\n"
        "2) Войдите под обычным (не временным) пользователем Windows.\n"
        "3) Или задайте переменную SHEETS_HUB_WEBVIEW_DIR на доступную папку.\n\n"
        f"Пробовали:\n{detail}"
    )


def _resolve_webview_dir() -> Path:
    override = (os.environ.get("SHEETS_HUB_WEBVIEW_DIR") or "").strip()
    if override:
        path = Path(override).expanduser()
        if _probe_dir(path):
            return path
        raise PermissionError(
            f"SHEETS_HUB_WEBVIEW_DIR недоступен для записи:\n{path}"
        )
    return _webview_data_dir()


def _show_startup_error(message: str) -> None:
    text = (message or "Неизвестная ошибка запуска").strip()
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0,
                text[:1500],
                "Sheets Hub — ошибка запуска",
                0x10,
            )
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def main() -> None:
    # Сначала каталог WebView2 и окно — тяжёлый Google-клиент подтянется из JS.
    try:
        data_dir = _resolve_webview_dir()
    except Exception as exc:
        _show_startup_error(str(exc))
        raise SystemExit(1) from exc

    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(data_dir)
    os.environ["PYWEBVIEW_STORAGE_PATH"] = str(data_dir)

    configure_tls()
    try:
        import webview
    except ImportError as exc:
        msg = (
            "Для HTML-интерфейса нужен пакет pywebview.\n"
            "pip install pywebview\n"
            f"{exc}"
        )
        _show_startup_error(msg)
        raise SystemExit(msg) from exc

    try:
        api = HelixApi()
        index = _ui_index().resolve()
        window = webview.create_window(
            "Sheets Hub",
            url=index.as_uri(),
            js_api=api,
            width=1180,
            height=860,
            min_size=(900, 640),
            background_color="#f0f4fa",
        )
        api.set_window(window)

        # Данные грузит только pywebviewready в index.html (один быстрый проход).
        webview.start(
            debug=False,
            private_mode=False,
            storage_path=str(data_dir),
        )
    except Exception as exc:
        detail = str(exc).strip() or traceback.format_exc()
        _show_startup_error(
            "Не удалось запустить окно Sheets Hub.\n\n"
            f"{detail}\n\n"
            f"Каталог WebView2:\n{data_dir}"
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
