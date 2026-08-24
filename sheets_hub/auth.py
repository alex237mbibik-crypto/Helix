from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

from sheets_hub.config import ROOT

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_NAME = "token.json"
LAST_EMAIL_NAME = "last_email.txt"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


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
    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "account_email": account_email,
    }
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
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    kwargs: dict = {
        "port": 0,
        "prompt": "select_account",
        "authorization_prompt_message": "",
        "access_type": "offline",
        "include_granted_scopes": "true",
    }
    if hint:
        kwargs["login_hint"] = hint
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
        "25",
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
        timeout=30,
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
        timeout=20,
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

    # Сначала Python refresh, потом обходы SSL.
    errors: list[str] = []
    try:
        creds.refresh(Request())
        email = ""
        try:
            raw = json.loads(dest.read_text(encoding="utf-8"))
            email = str(raw.get("account_email") or "")
        except Exception:
            pass
        _save_token(creds, dest, account_email=email)
        return creds
    except Exception as exc:
        errors.append(f"python: {exc}")

    client_id = creds.client_id or ""
    client_secret = creds.client_secret or ""
    refresh = creds.refresh_token or ""
    if not (client_id and client_secret and refresh):
        section = oauth_client_section(client_secrets_path)
        client_id = client_id or str(section.get("client_id") or "")
        client_secret = client_secret or str(section.get("client_secret") or "")

    for label, action in (
        ("curl", lambda: _refresh_via_curl(client_id, client_secret, refresh)),
        ("requests", lambda: _refresh_via_requests(client_id, client_secret, refresh)),
    ):
        try:
            token = action()
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
