"""Запуск Sheets Hub с HTML-интерфейсом Helix_Front (pywebview)."""

from __future__ import annotations

import sys
from pathlib import Path

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


def main() -> None:
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

    def on_start():
        api.connect()
        try:
            window.evaluate_js("window.Helix && window.Helix.refresh && window.Helix.refresh()")
        except Exception:
            pass

    webview.start(on_start, debug=False)


if __name__ == "__main__":
    main()
