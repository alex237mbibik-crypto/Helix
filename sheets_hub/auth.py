from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

from sheets_hub.config import ROOT

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/spreadsheets",
]
TOKEN_NAME = "token.json"
LAST_EMAIL_NAME = "last_email.txt"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_OAUTH_PORT = 8765


class AuthError(Exception):
    pass


def token_path_near(credentials_path: Path) -> Path:
    return credentials_path.parent / TOKEN_NAME


def last_email_path(credentials_path: Path | None = None) -> Path:
    base = (credentials_path or (ROOT / "credentials.json")).parent
    return base / LAST_EMAIL_NAME


def load_last_email(credentials_path: Path | None = None) -> str:
    path = last_email_path(credentials_path)
    if not path.exists():
        # Если уже входили — возьмём email из token.json
        creds = credentials_path or (ROOT / "credentials.json")
        email = credentials_email(creds) if creds.exists() else ""
        if email and "@" in email and "OAuth" not in email:
            return email
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def save_last_email(email: str, credentials_path: Path | None = None) -> None:
    text = (email or "").strip()
    if not text:
        return
    last_email_path(credentials_path).write_text(text + "\n", encoding="utf-8")


def clear_user_login(credentials_path: Path) -> None:
    token = token_path_near(credentials_path)
    try:
        token.unlink(missing_ok=True)
    except Exception:
        pass


def credential_kind(path: Path) -> str:
    """service_account | oauth_client | unknown"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    if data.get("type") == "service_account" and data.get("client_email"):
        return "service_account"
    if isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict):
        return "oauth_client"
    return "unknown"


def oauth_client_section(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    section = data.get("installed") or data.get("web")
    if not isinstance(section, dict):
        raise AuthError("В JSON нет блока installed/web — это не OAuth-клиент Desktop.")
    if not section.get("client_id") or not section.get("client_secret"):
        raise AuthError("В OAuth JSON нет client_id / client_secret.")
    return section


def credentials_email(path: Path) -> str:
    kind = credential_kind(path)
    if kind == "service_account":
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("client_email") or "").strip()
        except Exception:
            return ""
    if kind == "oauth_client":
        token = token_path_near(path)
        if token.exists():
            try:
                data = json.loads(token.read_text(encoding="utf-8"))
                return str(data.get("account_email") or data.get("client_id") or "Google аккаунт").strip()
            except Exception:
                return "Google аккаунт (вход выполнен)"
        return "OAuth-клиент (нужен вход)"
    return ""


def install_google_credentials(source: Path, dest: Path | None = None) -> Path:
    """Копирует service account или OAuth Desktop client JSON в credentials.json."""
    target = dest or (ROOT / "credentials.json")
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    data = json.loads(source.read_text(encoding="utf-8"))
    kind = "unknown"
    if data.get("type") == "service_account" and data.get("client_email"):
        kind = "service_account"
    elif isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict):
        kind = "oauth_client"
    if kind == "unknown":
        raise ValueError(
            "Нужен JSON из Google Cloud:\n"
            "• OAuth: APIs & Services → Credentials → OAuth client → Desktop → скачать JSON\n"
            "• или Service Account JSON (старый способ)."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if source != target.resolve():
        shutil.copy2(source, target)
    return target


def _save_token(creds: UserCredentials, token_path: Path, account_email: str = "") -> None:
    prev: dict = {}
    if token_path.exists():
        try:
            prev = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    refresh = creds.refresh_token or prev.get("refresh_token") or ""
    email = (account_email or "").strip() or str(prev.get("account_email") or "").strip()
    payload = {
        "token": creds.token,
        "refresh_token": refresh,
        "token_uri": creds.token_uri or prev.get("token_uri") or _TOKEN_URL,
        "client_id": creds.client_id or prev.get("client_id"),
        "client_secret": creds.client_secret or prev.get("client_secret"),
        "scopes": list(creds.scopes or prev.get("scopes") or SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else prev.get("expiry"),
        "account_email": email,
    }
    if not payload["refresh_token"]:
        raise AuthError(
            "Google не выдал refresh_token — повторный вход снова попросит «Разрешить».\n"
            "В аккаунте Google: https://myaccount.google.com/permissions — удалите доступ "
            "приложения и войдите ещё раз (один раз с «Разрешить»)."
        )
    # Чтобы creds.refresh не терял refresh_token.
    if not creds.refresh_token and refresh:
        creds.refresh_token = refresh
    token_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_token(token_path: Path) -> UserCredentials | None:
    if not token_path.exists():
        return None
    data = json.loads(token_path.read_text(encoding="utf-8"))
    if not data.get("refresh_token") and not data.get("token"):
        return None
    return UserCredentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri") or _TOKEN_URL,
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )


def has_saved_login(credentials_path: Path) -> bool:
    """Есть сохранённый вход с refresh_token — браузер больше не нужен."""
    token = token_path_near(credentials_path)
    if not token.exists():
        return False
    try:
        data = json.loads(token.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(data.get("refresh_token"))


def needs_consent_prompt(credentials_path: Path) -> bool:
    """consent только если ещё ни разу не получили refresh_token."""
    return not has_saved_login(credentials_path)


def _fetch_account_email(access_token: str) -> str:
    try:
        info = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
            verify=False,
        )
        if info.ok:
            return str(info.json().get("email") or "").strip()
    except Exception:
        pass
    return ""


def _make_flow(client_secrets_path: Path) -> InstalledAppFlow:
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    # Старый auth_uri из скачанного JSON иногда даёт пустую страницу.
    try:
        flow.client_config["auth_uri"] = _AUTH_URI
        flow.client_config["token_uri"] = _TOKEN_URL
    except Exception:
        pass
    return flow


def build_authorization_url(
    client_secrets_path: Path,
    *,
    login_hint: str = "",
    port: int = _OAUTH_PORT,
    force_consent: bool | None = None,
) -> tuple[InstalledAppFlow, str]:
    """Готовит flow и ссылку входа (для браузера / копирования)."""
    if credential_kind(client_secrets_path) != "oauth_client":
        raise AuthError("Для входа нужен OAuth Desktop JSON, не service account.")
    # localhost по HTTP — иначе oauthlib может ругаться при обмене кода.
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_flow(client_secrets_path)
    flow.redirect_uri = f"http://127.0.0.1:{port}/"
    # prompt=consent только при первом входе — иначе Google каждый раз снова просит «Разрешить».
    consent = needs_consent_prompt(client_secrets_path) if force_consent is None else force_consent
    kwargs: dict = {
        "access_type": "offline",
        "include_granted_scopes": "true",
    }
    if consent:
        kwargs["prompt"] = "consent"
    else:
        kwargs["prompt"] = "select_account"
    hint = (login_hint or "").strip()
    if hint:
        kwargs["login_hint"] = hint
    auth_url, _state = flow.authorization_url(**kwargs)
    return flow, auth_url


def listen_for_oauth_redirect(
    *,
    port: int = _OAUTH_PORT,
    timeout: float = 300.0,
    ready: threading.Event | None = None,
) -> str:
    """Ждёт один редирект на 127.0.0.1:port и возвращает полный URL с code=."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            holder["path"] = self.path
            body = (
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Вход выполнен</h2><p>Можно закрыть вкладку и вернуться в Sheets Hub.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise AuthError(
            f"Порт {port} занят. Закройте другие окна входа или перезапустите программу.\n{exc}"
        ) from exc
    server.timeout = 1.0
    if ready is not None:
        ready.set()
    deadline = time.time() + max(5.0, float(timeout))
    try:
        while time.time() < deadline:
            server.handle_request()
            if "path" in holder:
                break
        else:
            raise AuthError("Время ожидания входа истекло. Попробуйте ещё раз.")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    path = holder.get("path") or "/"
    return f"http://127.0.0.1:{port}{path}"


def finish_oauth_with_response_url(
    flow: InstalledAppFlow,
    response_url: str,
    *,
    client_secrets_path: Path,
    token_path: Path | None = None,
    login_hint: str = "",
) -> UserCredentials:
    """Завершает вход по адресу вида http://127.0.0.1:8765/?code=..."""
    text = (response_url or "").strip().strip('"').strip("'")
    if not text:
        raise AuthError("Пустой адрес из браузера.")
    if "code=" not in text and not text.startswith("4/"):
        raise AuthError(
            "В адресе нет code=…\n"
            "После входа в Google скопируйте весь адрес из строки браузера "
            "(начинается с http://127.0.0.1:8765/?code=…)."
        )
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    if text.startswith("4/") or (text.startswith("4%2F") and "://" not in text):
        flow.fetch_token(code=text)
    else:
        flow.fetch_token(authorization_response=text)
    creds = flow.credentials
    dest = token_path or token_path_near(client_secrets_path)
    hint = (login_hint or "").strip()
    email = _fetch_account_email(creds.token or "") or hint
    _save_token(creds, dest, account_email=email)
    if email:
        save_last_email(email, client_secrets_path)
    return creds


def run_oauth_login(
    client_secrets_path: Path,
    token_path: Path | None = None,
    *,
    login_hint: str = "",
) -> UserCredentials:
    """Открывает браузер для входа в Google и сохраняет token.json."""
    if credential_kind(client_secrets_path) != "oauth_client":
        raise AuthError("Для входа нужен OAuth Desktop JSON, не service account.")
    dest = token_path or token_path_near(client_secrets_path)
    hint = (login_hint or "").strip()
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_flow(client_secrets_path)
    consent = needs_consent_prompt(client_secrets_path)
    kwargs: dict = {
        "bind_addr": "127.0.0.1",
        "port": _OAUTH_PORT,
        "open_browser": True,
        "access_type": "offline",
        "prompt": "consent" if consent else "select_account",
        "authorization_prompt_message": (
            "Если в браузере белый экран — нажмите «Скопировать ссылку» в программе "
            "и откройте её в Edge/Firefox, либо отключите VPN/проверку HTTPS в антивирусе.\n"
        ),
        "success_message": "Вход выполнен. Можно закрыть вкладку и вернуться в Sheets Hub.",
    }
    if hint:
        kwargs["login_hint"] = hint
    try:
        creds = flow.run_local_server(**kwargs)
    except OSError:
        # Порт занят — любой свободный.
        kwargs["port"] = 0
        creds = flow.run_local_server(**kwargs)
    email = _fetch_account_email(creds.token or "") or hint
    _save_token(creds, dest, account_email=email)
    if email:
        save_last_email(email, client_secrets_path)
    return creds


def _refresh_via_curl(client_id: str, client_secret: str, refresh_token: str) -> str:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise AuthError("curl не найден")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        curl,
        "-k",
        "-sS",
        "--max-time",
        "10",
        "-X",
        "POST",
        _TOKEN_URL,
        "--data-urlencode",
        f"client_id={client_id}",
        "--data-urlencode",
        f"client_secret={client_secret}",
        "--data-urlencode",
        f"refresh_token={refresh_token}",
        "--data-urlencode",
        "grant_type=refresh_token",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=12,
        creationflags=flags if sys.platform == "win32" else 0,
    )
    text = (result.stdout or b"").decode("utf-8-sig", errors="replace").strip()
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise AuthError(err or text or "curl refresh failed")
    data = json.loads(text)
    if data.get("error"):
        raise AuthError(str(data.get("error")))
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise AuthError(f"Нет access_token: {data}")
    return token


def _refresh_via_requests(client_id: str, client_secret: str, refresh_token: str) -> str:
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=8,
        verify=False,
    )
    response.raise_for_status()
    data = response.json()
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise AuthError(f"Нет access_token: {data}")
    return token


def ensure_user_credentials(
    client_secrets_path: Path,
    *,
    token_path: Path | None = None,
    interactive: bool = False,
) -> UserCredentials:
    dest = token_path or token_path_near(client_secrets_path)
    creds = _load_token(dest)
    if creds is None:
        if not interactive:
            raise AuthError(
                "Нет сохранённого входа Google.\n"
                "В «Таблицы» нажмите «Войти через Google»."
            )
        return run_oauth_login(client_secrets_path, dest)

    if creds.valid and creds.token:
        return creds

    if not creds.refresh_token:
        if not interactive:
            raise AuthError("Токен устарел. Нажмите «Войти через Google» снова.")
        return run_oauth_login(client_secrets_path, dest)

    client_id = creds.client_id or ""
    client_secret = creds.client_secret or ""
    refresh = creds.refresh_token or ""
    if not (client_id and client_secret and refresh):
        section = oauth_client_section(client_secrets_path)
        client_id = client_id or str(section.get("client_id") or "")
        client_secret = client_secret or str(section.get("client_secret") or "")

    errors: list[str] = []

    def _apply_token(token: str) -> UserCredentials:
        creds.token = token
        creds.expiry = None
        email = ""
        try:
            raw = json.loads(dest.read_text(encoding="utf-8"))
            email = str(raw.get("account_email") or "")
        except Exception:
            pass
        _save_token(creds, dest, account_email=email)
        return creds

    # На Windows Python-SSL к oauth2 часто висит минутами — сначала быстрый curl.
    refresh_steps: list[tuple[str, Callable]] = []
    if sys.platform == "win32":
        refresh_steps.extend(
            (
                ("curl", lambda: _refresh_via_curl(client_id, client_secret, refresh)),
                ("requests", lambda: _refresh_via_requests(client_id, client_secret, refresh)),
                ("python", lambda: (creds.refresh(Request()) or creds.token)),
            )
        )
    else:
        refresh_steps.extend(
            (
                ("python", lambda: (creds.refresh(Request()) or creds.token)),
                ("curl", lambda: _refresh_via_curl(client_id, client_secret, refresh)),
                ("requests", lambda: _refresh_via_requests(client_id, client_secret, refresh)),
            )
        )

    for label, action in refresh_steps:
        try:
            token = action()
            if label == "python":
                token = creds.token
            if not token:
                raise AuthError("пустой token")
            return _apply_token(str(token))
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    if interactive:
        return run_oauth_login(client_secrets_path, dest)
    raise AuthError("Не удалось обновить вход Google.\n" + "\n".join(errors[:4]))


def access_token_for_path(credentials_path: Path) -> str:
    """Актуальный access_token для записи (OAuth или service account JWT не здесь)."""
    kind = credential_kind(credentials_path)
    if kind == "oauth_client":
        creds = ensure_user_credentials(credentials_path, interactive=False)
        if not creds.token:
            raise AuthError("Пустой access_token после обновления.")
        return creds.token
    if kind == "service_account":
        # JWT-токен получают в client.py через curl/SA helpers.
        raise AuthError("Для service account используйте JWT-хелперы client.py")
    raise AuthError("Неизвестный тип credentials.json")


def load_gspread_credentials(credentials_path: Path, *, interactive: bool = False):
    kind = credential_kind(credentials_path)
    if kind == "oauth_client":
        return ensure_user_credentials(credentials_path, interactive=interactive)
    if kind == "service_account":
        return ServiceAccountCredentials.from_service_account_file(
            str(credentials_path), scopes=SCOPES
        )
    raise AuthError(
        "Нет подходящего credentials.json.\n"
        "Скачайте OAuth Desktop JSON в Google Cloud и выберите его в «Таблицы»."
    )


def account_label(credentials_path: Path, creds=None) -> str:
    if creds is not None and isinstance(creds, ServiceAccountCredentials):
        return creds.service_account_email
    kind = credential_kind(credentials_path)
    if kind == "oauth_client":
        token = token_path_near(credentials_path)
        if token.exists():
            try:
                data = json.loads(token.read_text(encoding="utf-8"))
                email = str(data.get("account_email") or "").strip()
                if email:
                    return email
            except Exception:
                pass
            return "ваш Google-аккаунт"
        return "OAuth (нужен вход)"
    if kind == "service_account":
        return credentials_email(credentials_path) or "service account"
    return ""
