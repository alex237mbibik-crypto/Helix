"""Веб-сервер Sheets Hub: тот же HTML UI и HelixApi, но через браузер."""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sheets_hub.config import install_credentials, writable_data_dir
from sheets_hub.ssl_setup import configure_tls
from sheets_hub.web_api import HelixApi

# Публичные методы API, которые можно вызвать из браузера.
_API_METHODS = frozenset(
    {
        "get_state",
        "connect",
        "pick_credentials",
        "set_filters",
        "set_tutorial",
        "reload",
        "fetch_colors",
        "begin_slot",
        "cancel_slot",
        "book_slot",
        "unlock_settings",
        "save_telegram",
        "test_telegram",
        "save_registry",
        "load_cloud_tables",
        "save_cloud_tables",
    }
)

_SESSION_TTL_SEC = 12 * 3600
_sessions: dict[str, tuple[float, HelixApi]] = {}
_sessions_lock = threading.Lock()
_COOKIE = "helix_sid"


def _ui_index() -> Path:
    here = Path(__file__).resolve().parent / "webui" / "index.html"
    if here.exists():
        return here
    raise FileNotFoundError("Не найден sheets_hub/webui/index.html")


def _purge_sessions(now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    dead = [sid for sid, (stamp, _) in _sessions.items() if ts - stamp > _SESSION_TTL_SEC]
    for sid in dead:
        _sessions.pop(sid, None)


def _get_or_create_api(session_id: str) -> HelixApi:
    now = time.time()
    with _sessions_lock:
        _purge_sessions(now)
        hit = _sessions.get(session_id)
        if hit:
            api = hit[1]
            _sessions[session_id] = (now, api)
            return api
        api = HelixApi()
        _sessions[session_id] = (now, api)
        return api


def create_app() -> FastAPI:
    configure_tls()
    app = FastAPI(title="Sheets Hub", docs_url=None, redoc_url=None)
    ui_dir = _ui_index().parent
    app.mount("/static", StaticFiles(directory=str(ui_dir)), name="static")

    @app.middleware("http")
    async def ensure_session(request: Request, call_next: Callable):
        sid = request.cookies.get(_COOKIE) or ""
        if not sid or len(sid) < 16:
            sid = secrets.token_urlsafe(24)
            request.state.new_sid = sid
        else:
            request.state.new_sid = None
        request.state.helix_sid = sid
        response = await call_next(request)
        if getattr(request.state, "new_sid", None):
            response.set_cookie(
                _COOKIE,
                sid,
                httponly=True,
                samesite="lax",
                max_age=_SESSION_TTL_SEC,
            )
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_ui_index(), media_type="text/html; charset=utf-8")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/{method}")
    async def api_call(method: str, request: Request) -> JSONResponse:
        # Request импортирован на уровне модуля — иначе FastAPI видит его как query.
        name = (method or "").strip()
        if name not in _API_METHODS:
            return JSONResponse({"ok": False, "error": f"Неизвестный метод: {name}"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Ожидается JSON-объект"}, status_code=400)

        api = _get_or_create_api(str(getattr(request.state, "helix_sid", "") or ""))

        if name == "pick_credentials":
            return JSONResponse(
                {
                    "ok": False,
                    "error": "В веб-версии нажмите «JSON ключа» и выберите файл на компьютере.",
                    **api.snapshot(),
                }
            )

        handler = getattr(api, name, None)
        if handler is None:
            return JSONResponse({"ok": False, "error": f"Метод недоступен: {name}"}, status_code=404)

        def run() -> dict[str, Any]:
            try:
                if name in {"get_state", "load_cloud_tables"}:
                    result = handler()
                else:
                    result = handler(body)
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except Exception as exc:
                return {"ok": False, "error": str(exc).split("\n")[0], **api.snapshot()}

        result = await asyncio.to_thread(run)
        return JSONResponse(result)

    @app.post("/api/upload_credentials")
    async def upload_credentials(
        request: Request, file: UploadFile = File(...)
    ) -> JSONResponse:
        api = _get_or_create_api(str(getattr(request.state, "helix_sid", "") or ""))
        raw = await file.read()
        if not raw or len(raw) > 2_000_000:
            return JSONResponse(
                {"ok": False, "error": "Пустой или слишком большой файл", **api.snapshot()},
                status_code=400,
            )
        suffix = Path(file.filename or "credentials.json").suffix or ".json"

        def install() -> dict[str, Any]:
            tmp: Path | None = None
            try:
                fd, name = tempfile.mkstemp(prefix="helix_cred_", suffix=suffix)
                os.close(fd)
                tmp = Path(name)
                tmp.write_bytes(raw)
                installed = install_credentials(tmp, dest=writable_data_dir() / "credentials.json")
                api.config.credentials = installed
                api._persist()
                return api.connect({"reload": True})
            except Exception as exc:
                return {"ok": False, "error": str(exc).split("\n")[0], **api.snapshot()}
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

        result = await asyncio.to_thread(install)
        return JSONResponse(result)

    return app


def main() -> None:
    import webbrowser

    import uvicorn

    host = (os.environ.get("SHEETS_HUB_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("SHEETS_HUB_PORT") or "8787")
    open_browser = (os.environ.get("SHEETS_HUB_NO_BROWSER") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }
    url = f"http://{host}:{port}/"
    if open_browser and host in {"127.0.0.1", "localhost"}:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Sheets Hub (web): {url}")
    uvicorn.run(
        "sheets_hub.web_server:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
