"""Запуск Sheets Hub с HTML-интерфейсом Helix_Front (pywebview)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sheets_hub.config import user_data_dir
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


def _webview_data_dir() -> Path:
    """Каталог Edge WebView2 — только в AppData (Program Files / Temp недоступны)."""
    path = user_data_dir() / "webview"
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        raise PermissionError(
            f"Нет прав на запись в каталог WebView2:\n{path}\n{exc}"
        ) from exc
    return path


def main() -> None:
    # Сначала каталог WebView2 и окно — тяжёлый Google-клиент подтянется из JS.
    data_dir = _webview_data_dir()
    os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(data_dir)
    os.environ.setdefault("PYWEBVIEW_STORAGE_PATH", str(data_dir))

    configure_tls()
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "Для HTML-интерфейса нужен пакет pywebview.\n"
            "pip install pywebview\n"
            f"{exc}"
        ) from exc

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


if __name__ == "__main__":
    main()
