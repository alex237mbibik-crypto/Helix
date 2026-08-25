from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import gspread
import requests
from google.auth import jwt as google_jwt
from google.auth.transport.requests import AuthorizedSession
from google.auth.credentials import Credentials as GoogleCredentials
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sheets_hub.auth import AuthError, account_label, credential_kind, load_gspread_credentials
from sheets_hub.calendar_sheet import (
    is_calendar_matrix,
    is_lock_text,
    lock_is_fresh,
    make_lock_text,
    parse_sheet_rows,
)
from sheets_hub.config import (
    KIND_INFO,
    KIND_RECORDS,
    SheetRef,
    companion_info_titles,
    is_info_ref,
    is_info_title,
    parse_spreadsheet_id,
    prefer_sheet_title,
    requested_sheet_titles,
)
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key
from sheets_hub.ssl_setup import ca_bundle_path, configure_tls, session_verify_target

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RETRY_ATTEMPTS = 2
_HTTP_TIMEOUT = (3, 8)
_PROBE_TIMEOUT = (2, 5)
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SHEETS_VALUES_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{range}"
# Больше 38 строк календаря на практике не нужно — быстрее загрузка и легче UI.
MAX_SHEET_ROWS = 38


class SheetsError(Exception):
    pass


def _is_network_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "ssl",
        "eof",
        "max retries",
        "connection",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "10054",
        "10060",
        "unexpected_eof",
    )
    return any(marker in text for marker in markers)


def _friendly_error(exc: BaseException) -> SheetsError:
    if _is_network_error(exc):
        return SheetsError(
            "Не удалось подключиться к Google (обрыв защищённого соединения).\n"
            "Это не проблема доступа к таблице — роль «Редактор» тут ни при чём.\n"
            "Проверьте интернет, отключите VPN, в антивирусе выключите проверку HTTPS.\n"
            "В Google Cloud у проекта должен быть включён Google Sheets API."
        )
    return SheetsError(str(exc))


def _public_csv_url(spreadsheet_id: str, sheet: str = "", gid: str = "") -> str:
    # gid надёжнее имени: «АВГУСТ 2026 (ГИНЕКОЛОГ)» по sheet= часто отдаёт первый лист.
    if str(gid).strip():
        return (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
            f"?format=csv&gid={quote(str(gid).strip())}"
        )
    if sheet.strip():
        return (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
            f"?format=csv&sheet={quote(sheet.strip())}"
        )
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"


def _parse_csv_text(text: str) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [[(cell or "").replace("\u202f", " ").strip() for cell in row] for row in rows]
    return rows[:MAX_SHEET_ROWS]


def _fetch_url_curl(url: str, *, insecure: bool = False) -> str:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise OSError("curl не найден")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [curl, "-fsSL", "--max-time", "12", "--connect-timeout", "5", url]
    if insecure:
        cmd.insert(1, "-k")
    else:
        cmd.insert(1, "--ssl-no-revoke")
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=15,
        creationflags=flags if sys.platform == "win32" else 0,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise SheetsError(err or f"curl завершился с кодом {result.returncode}")
    return result.stdout.decode("utf-8-sig", errors="replace")


def _fetch_url_powershell(url: str) -> str:
    if sys.platform != "win32":
        raise OSError("PowerShell доступен только в Windows")
    safe_url = url.replace("'", "''")
    # Короткий таймаут, чтобы не висеть минутами при SSL-блоке.
    command = (
        "$ProgressPreference='SilentlyContinue'; "
        f"(Invoke-WebRequest -Uri '{safe_url}' -UseBasicParsing -TimeoutSec 12).Content"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        timeout=16,
        creationflags=flags,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise SheetsError(err or f"PowerShell завершился с кодом {result.returncode}")
    return result.stdout.decode("utf-8-sig", errors="replace")


def _fetch_url_requests(url: str, *, verify: bool | str) -> str:
    if verify is False:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    session = requests.Session()
    session.verify = verify
    session.headers["User-Agent"] = "SheetsHub/1.0"
    resp = session.get(url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig", errors="replace")


def _fetch_public_csv(spreadsheet_id: str, sheet: str = "", gid: str = "") -> list[list[str]]:
    resolved_gid = str(gid or "").strip() or _lookup_sheet_gid(spreadsheet_id, sheet)
    url = _public_csv_url(spreadsheet_id, sheet=sheet if not resolved_gid else "", gid=resolved_gid)
    # Сначала самые быстрые варианты для Windows с битым SSL.
    attempts: list[tuple[str, object]] = []
    if sys.platform == "win32":
        attempts.extend(
            [
                ("curl без проверки SSL", lambda: _fetch_url_curl(url, insecure=True)),
                ("curl (Windows)", lambda: _fetch_url_curl(url)),
                ("Python без проверки SSL", lambda: _fetch_url_requests(url, verify=False)),
                ("PowerShell", lambda: _fetch_url_powershell(url)),
            ]
        )
    else:
        attempts.extend(
            [
                ("curl", lambda: _fetch_url_curl(url)),
                ("Python", lambda: _fetch_url_requests(url, verify=session_verify_target())),
            ]
        )
    configure_tls()
    ca = ca_bundle_path()
    if ca:
        attempts.append(("Python + cacert.pem", lambda c=ca: _fetch_url_requests(url, verify=c)))

    errors: list[str] = []
    for label, fetch in attempts:
        try:
            text = fetch()
            rows = _parse_csv_text(text)
            if not rows:
                raise SheetsError("пустой ответ")
            return rows
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    raise SheetsError(
        "Не удалось скачать таблицу ни одним способом.\n"
        + "\n".join(errors[-3:])
        + "\n\nОтключите VPN и проверку HTTPS в антивирусе."
    )


# spreadsheet_id -> {sheet_title_lower: gid}
_SHEET_GID_CACHE: dict[str, dict[str, str]] = {}


def _lookup_sheet_gid(spreadsheet_id: str, sheet: str) -> str:
    raw = (sheet or "").strip()
    if not raw:
        return ""
    mapping = _SHEET_GID_CACHE.get(spreadsheet_id) or {}
    if raw in mapping:
        return mapping[raw]
    low = raw.lower()
    for title, gid in mapping.items():
        if title.lower() == low:
            return gid
    for title, gid in mapping.items():
        if low in title.lower() or title.lower() in low:
            return gid
    return ""


def _fetch_url_text(url: str) -> str:
    if sys.platform == "win32":
        try:
            return _fetch_url_curl(url, insecure=True)
        except Exception:
            pass
        try:
            return _fetch_url_powershell(url)
        except Exception:
            pass
    try:
        return _fetch_url_curl(url)
    except Exception:
        return _fetch_url_requests(url, verify=session_verify_target())


def _list_public_sheets(spreadsheet_id: str) -> list[tuple[str, str]]:
    """[(title, gid), ...] из публичной htmlview — без Sheets API."""
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/htmlview"
    try:
        text = _fetch_url_text(url)
    except Exception:
        return []
    pairs = re.findall(
        r'items\.push\(\{\s*name:\s*"([^"]+)"\s*,\s*pageUrl:\s*"([^"]+)"',
        text,
    )
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    gid_map: dict[str, str] = {}
    for name, page_url in pairs:
        title = (name or "").strip()
        if not title or title.lower() in seen:
            continue
        gid_match = re.search(r"gid=(\d+)", page_url.replace("\\/", "/"))
        gid = gid_match.group(1) if gid_match else ""
        seen.add(title.lower())
        out.append((title, gid))
        if gid:
            gid_map[title] = gid
    if gid_map:
        _SHEET_GID_CACHE[spreadsheet_id] = gid_map
    return out


def _list_public_sheet_titles(spreadsheet_id: str) -> list[str]:
    return [title for title, _gid in _list_public_sheets(spreadsheet_id)]


def _parts_for_source(source: SheetRef, available: list[str] | None = None) -> list[SheetRef]:
    """Календарный лист + соседние вкладки-справки той же книги."""
    requested = requested_sheet_titles(source.sheet)
    if requested is not None and len(requested) == 1 and not is_info_title(requested[0]):
        calendar = replace(source, sheet=requested[0])
        parts = [calendar]
        if available:
            for title in companion_info_titles(available, requested[0]):
                parts.append(replace(source, sheet=title, kind=KIND_INFO))
        return parts
    if requested is not None and len(requested) > 1:
        return [replace(source, sheet=title) for title in requested]

    titles = available or []
    if not titles:
        return [source]
    calendar_title = prefer_sheet_title(titles, source.sheet)
    # Не брать справку как «календарь», если есть месячный лист.
    if is_info_title(calendar_title):
        non_info = [title for title in titles if not is_info_title(title)]
        if non_info:
            calendar_title = prefer_sheet_title(non_info, source.sheet)
    parts = [replace(source, sheet=calendar_title)]
    for title in companion_info_titles(titles, calendar_title):
        parts.append(replace(source, sheet=title, kind=KIND_INFO))
    return parts


def _col_to_a1(col: int) -> str:
    """1-based column index → A, B, … AA."""
    if col < 1:
        raise ValueError("col must be >= 1")
    letters: list[str] = []
    n = col
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _a1_range(sheet: str, row: int, col: int) -> str:
    cell = f"{_col_to_a1(col)}{row}"
    safe = (sheet or "Sheet1").replace("'", "''")
    return f"'{safe}'!{cell}"


def _value_input_option(value: str) -> str:
    """«+7900…» как USER_ENTERED Sheets читает формулой → ошибка в ячейке и номер пропадает."""
    text = str(value or "")
    if text.startswith(("=", "+", "@")):
        return "RAW"
    return "USER_ENTERED"


def _curl_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    form: dict[str, str] | None = None,
) -> dict:
    """HTTP JSON через curl -k — обход битого SSL в Python на Windows."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SheetsError("curl не найден — запись через обход SSL недоступна")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        curl,
        "-k",
        "-sS",
        "--max-time",
        "12",
        "--connect-timeout",
        "5",
        "-X",
        method.upper(),
        url,
    ]
    for key, value in (headers or {}).items():
        cmd.extend(["-H", f"{key}: {value}"])
    tmp_path: Path | None = None
    try:
        if body is not None:
            cmd.extend(["-H", "Content-Type: application/json; charset=utf-8"])
            # Через файл — надёжнее для кириллицы и длинных тел на Windows.
            fd, name = tempfile.mkstemp(prefix="sheets_hub_", suffix=".json")
            os.close(fd)
            tmp_path = Path(name)
            tmp_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            cmd.extend(["--data-binary", f"@{tmp_path}"])
        if form is not None:
            for key, value in form.items():
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
            creationflags=flags if sys.platform == "win32" else 0,
        )
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
    text = (result.stdout or b"").decode("utf-8-sig", errors="replace").strip()
    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise SheetsError(err or text or f"curl {method} код {result.returncode}")
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SheetsError(f"Неожиданный ответ API: {text[:300]}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise SheetsError(str(data.get("error")))
    return data if isinstance(data, dict) else {}


def _access_token_via_curl(credentials_path: Path) -> str:
    creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
    now = int(time.time())
    payload = {
        "iss": creds.service_account_email,
        "sub": creds.service_account_email,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
        "scope": " ".join(SCOPES),
    }
    assertion = google_jwt.encode(creds.signer, payload)
    if isinstance(assertion, bytes):
        assertion = assertion.decode("ascii")
    data = _curl_json(
        "POST",
        _TOKEN_URL,
        form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise SheetsError(f"Не получили access_token: {data}")
    return token


def _access_token_via_requests_insecure(credentials_path: Path) -> str:
    """Запрос токена через requests verify=False, если curl/PowerShell недоступны."""
    creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
    now = int(time.time())
    payload = {
        "iss": creds.service_account_email,
        "sub": creds.service_account_email,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
        "scope": " ".join(SCOPES),
    }
    assertion = google_jwt.encode(creds.signer, payload)
    if isinstance(assertion, bytes):
        assertion = assertion.decode("ascii")
    response = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=20,
        verify=False,
    )
    response.raise_for_status()
    data = response.json()
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise SheetsError(f"Не получили access_token: {data}")
    return token


def _powershell_trust_preamble(class_name: str) -> str:
    # catch должен иметь тело — иначе MissingCatchOrFinally в Windows PowerShell.
    return (
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
        "try { "
        f"if (-not ([System.Management.Automation.PSTypeName]'{class_name}').Type) {{ "
        "Add-Type @'\n"
        "using System.Net;\n"
        "using System.Security.Cryptography.X509Certificates;\n"
        f"public class {class_name} {{ public static bool Ok(object s,X509Certificate c,X509Chain ch,SslPolicyErrors e){{return true;}} }}\n"
        "'@ "
        "}} "
        f"[Net.ServicePointManager]::ServerCertificateValidationCallback = [{class_name}]::Ok "
        "} catch { $null } "
        "try { "
        "if (Get-Command Invoke-RestMethod -ErrorAction SilentlyContinue) { "
        "$PSDefaultParameterValues['Invoke-RestMethod:SkipCertificateCheck'] = $true "
        "} } catch { $null } "
    )


def _access_token(credentials_path: Path) -> str:
    """Bearer-токен: OAuth пользователя или JWT service account."""
    kind = credential_kind(credentials_path)
    if kind == "oauth_client":
        try:
            from sheets_hub.auth import access_token_for_path

            return access_token_for_path(credentials_path)
        except AuthError as exc:
            raise SheetsError(str(exc)) from exc

    errors: list[str] = []
    for label, action in (
        ("curl", lambda: _access_token_via_curl(credentials_path)),
        ("powershell", lambda: _access_token_via_powershell(credentials_path)),
        ("requests", lambda: _access_token_via_requests_insecure(credentials_path)),
    ):
        try:
            return action()
        except Exception as exc:
            if label == "powershell" and sys.platform != "win32":
                continue
            errors.append(f"{label}: {exc}")
    raise SheetsError("Не получили access_token.\n" + "\n".join(errors[:4]))


def _access_token_via_powershell(credentials_path: Path) -> str:
    """Токен через PowerShell — если curl к oauth2 тоже режется."""
    if sys.platform != "win32":
        raise SheetsError("PowerShell доступен только в Windows")
    creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
    now = int(time.time())
    payload = {
        "iss": creds.service_account_email,
        "sub": creds.service_account_email,
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
        "scope": " ".join(SCOPES),
    }
    assertion = google_jwt.encode(creds.signer, payload)
    if isinstance(assertion, bytes):
        assertion = assertion.decode("ascii")
    safe_assertion = assertion.replace("'", "''")
    command = (
        _powershell_trust_preamble("SheetsHubTrust")
        + "$body = @{ grant_type = 'urn:ietf:params:oauth:grant-type:jwt-bearer'; "
        + f"assertion = '{safe_assertion}' }}; "
        + f"$r = Invoke-RestMethod -Uri '{_TOKEN_URL}' -Method Post -Body $body; "
        + "$r.access_token"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        timeout=18,
        creationflags=flags,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise SheetsError(err or "PowerShell token failed")
    token = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not token or " " in token or len(token) < 20:
        raise SheetsError(f"Не получили access_token через PowerShell: {token[:120]}")
    return token


def _update_cell_via_powershell(
    credentials_path: Path,
    spreadsheet_id: str,
    sheet: str,
    row: int,
    col: int,
    value: str,
) -> None:
    token = _access_token(credentials_path)
    cell_range = _a1_range(sheet, row, col)
    option = _value_input_option(value)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(cell_range, safe='')}?valueInputOption={option}"
    )
    payload = {"range": cell_range, "majorDimension": "ROWS", "values": [[value]]}
    fd, name = tempfile.mkstemp(prefix="sheets_hub_ps_", suffix=".json")
    os.close(fd)
    tmp_path = Path(name)
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        safe_url = url.replace("'", "''")
        safe_token = token.replace("'", "''")
        # Windows path → одинарные кавычки PowerShell.
        safe_file = str(tmp_path).replace("'", "''")
        command = (
            _powershell_trust_preamble("SheetsHubTrust2")
            + f"$headers = @{{ Authorization = 'Bearer {safe_token}' }}; "
            + f"$raw = [System.IO.File]::ReadAllText('{safe_file}', [System.Text.Encoding]::UTF8); "
            + f"Invoke-RestMethod -Uri '{safe_url}' -Method Put -Headers $headers "
            + "-ContentType 'application/json; charset=utf-8' "
            + "-Body ([Text.Encoding]::UTF8.GetBytes($raw)) | Out-Null"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=18,
            creationflags=flags,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
            raise SheetsError(err or "PowerShell write failed")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _update_cell_via_curl(
    credentials_path: Path,
    spreadsheet_id: str,
    sheet: str,
    row: int,
    col: int,
    value: str,
) -> None:
    token = _access_token(credentials_path)
    cell_range = _a1_range(sheet, row, col)
    option = _value_input_option(value)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(cell_range, safe='')}?valueInputOption={option}"
    )
    _curl_json(
        "PUT",
        url,
        headers={"Authorization": f"Bearer {token}"},
        body={"range": cell_range, "majorDimension": "ROWS", "values": [[value]]},
    )


def _read_cell_via_curl(
    credentials_path: Path,
    spreadsheet_id: str,
    sheet: str,
    row: int,
    col: int,
) -> str:
    token = _access_token(credentials_path)
    cell_range = _a1_range(sheet, row, col)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(cell_range, safe='')}"
    )
    data = _curl_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    values = data.get("values") or []
    if not values or not values[0]:
        return ""
    return str(values[0][0] or "").strip()


def _update_cell_via_requests_insecure(
    credentials_path: Path,
    spreadsheet_id: str,
    sheet: str,
    row: int,
    col: int,
    value: str,
) -> None:
    token = _access_token(credentials_path)
    cell_range = _a1_range(sheet, row, col)
    option = _value_input_option(value)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(cell_range, safe='')}?valueInputOption={option}"
    )
    response = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"range": cell_range, "majorDimension": "ROWS", "values": [[value]]},
        timeout=20,
        verify=False,
    )
    response.raise_for_status()


def _read_cell_via_requests_insecure(
    credentials_path: Path,
    spreadsheet_id: str,
    sheet: str,
    row: int,
    col: int,
) -> str:
    token = _access_token(credentials_path)
    cell_range = _a1_range(sheet, row, col)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(cell_range, safe='')}"
    )
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
        verify=False,
    )
    response.raise_for_status()
    data = response.json()
    values = data.get("values") or []
    if not values or not values[0]:
        return ""
    return str(values[0][0] or "").strip()


def _can_try_public_read(source: SheetRef) -> bool:
    spreadsheet_id = source.normalized_id()
    return bool(spreadsheet_id) and not spreadsheet_id.upper().startswith("PASTE_")


class SheetsClient:
    def __init__(self, credentials_path: Path, *, interactive: bool = False) -> None:
        configure_tls()
        if not credentials_path.exists():
            raise SheetsError(
                "Нет файла входа Google.\n"
                "Нажмите «Таблицы» → «JSON…» и укажите JSON сервисного аккаунта "
                "из Google Cloud (type: service_account)."
            )
        self._credentials_path = credentials_path
        self.read_only_public = False
        self.read_notes: list[str] = []
        self._api_insecure = False
        self.auth_kind = credential_kind(credentials_path)
        self._connect(interactive=interactive)

    def _connect(self, *, insecure: bool = False, interactive: bool = False) -> None:
        configure_tls()
        try:
            creds = load_gspread_credentials(self._credentials_path, interactive=interactive)
        except AuthError as exc:
            raise SheetsError(str(exc)) from exc
        session = self._build_session(creds, verify=False if insecure else None)
        self._gc = gspread.authorize(None, session=session)
        self._gc.set_timeout(_HTTP_TIMEOUT)
        self.auth_kind = credential_kind(self._credentials_path)
        self.service_email = account_label(self._credentials_path, creds)
        self._api_insecure = insecure

    def _build_session(self, creds: GoogleCredentials, *, verify: bool | str | None = None) -> AuthorizedSession:
        session = AuthorizedSession(creds)
        session.verify = session_verify_target() if verify is None else verify
        if verify is False:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        retry = Retry(
            total=1,
            connect=1,
            read=1,
            status=1,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=False,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers["Connection"] = "close"
        return session

    def probe_api(self, spreadsheet_id: str = "") -> bool:
        """Проверка, что Sheets API доступен (нужен для записи)."""
        if not spreadsheet_id:
            return False
        cached = getattr(self, "_api_ok", None)
        if cached is True:
            return True
        try:
            self._call(lambda: self._gc.open_by_key(spreadsheet_id).id)
            self._api_ok = True
            return True
        except Exception:
            if not getattr(self, "_api_insecure", False):
                try:
                    self._connect(insecure=True)
                    self._call(lambda: self._gc.open_by_key(spreadsheet_id).id)
                    self._api_ok = True
                    return True
                except Exception:
                    self._api_ok = False
                    return False
            self._api_ok = False
            return False

    def _call(self, work):
        last: BaseException | None = None
        rebuilt = False
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return work()
            except gspread.exceptions.SpreadsheetNotFound:
                raise
            except gspread.exceptions.WorksheetNotFound:
                raise
            except Exception as exc:
                last = exc
                if not _is_network_error(exc) or attempt == _RETRY_ATTEMPTS - 1:
                    raise
                if not rebuilt:
                    self._connect(insecure=getattr(self, "_api_insecure", False))
                    rebuilt = True
                time.sleep(1.2 * (attempt + 1))
        raise last  # pragma: no cover

    def _open_spreadsheet(self, url_or_id: str):
        spreadsheet_id = parse_spreadsheet_id(url_or_id)
        if not spreadsheet_id or spreadsheet_id.upper().startswith("PASTE_"):
            raise SheetsError("Не указан ID таблицы")
        try:
            return self._call(lambda: self._gc.open_by_key(spreadsheet_id))
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetsError(
                "Таблица не найдена. "
                f"Выдайте доступ {self.service_email} (Редактор)."
            ) from exc
        except gspread.exceptions.APIError as exc:
            raise SheetsError(f"Google API: {exc}") from exc
        except Exception as exc:
            raise _friendly_error(exc) from exc

    def _match_sheet_title(self, available: list[str], wanted: str) -> str | None:
        raw = (wanted or "").strip()
        if not raw:
            return None
        for title in available:
            if title.lower() == raw.lower():
                return title
        for title in available:
            if raw.lower() in title.lower() or title.lower() in raw.lower():
                return title
        return None

    def _open_worksheet(self, ref: SheetRef):
        spreadsheet_id = ref.normalized_id()
        if not spreadsheet_id or spreadsheet_id.upper().startswith("PASTE_"):
            raise SheetsError(f"В «{ref.name}» не указан ID таблицы")
        spreadsheet = self._open_spreadsheet(ref.spreadsheet_id)
        available = [ws.title for ws in spreadsheet.worksheets()]
        if not available:
            raise SheetsError(f"В «{ref.name}» нет листов")
        match = self._match_sheet_title(available, ref.sheet)
        if match is None and len(available) == 1:
            match = available[0]
        if match is None:
            raise SheetsError(
                f"В «{ref.name}» нет листа «{ref.sheet}». "
                f"В файле есть: {', '.join(available)}. "
                "В поле «Лист» напишите все или точное имя вкладки внизу таблицы."
            )
        return spreadsheet.worksheet(match)

    def _sheet_values(self, worksheet) -> list[list[str]]:
        # Читаем только первые MAX_SHEET_ROWS строк — меньше трафика и быстрее UI.
        def _read() -> list[list[str]]:
            try:
                values = worksheet.get(
                    f"A1:ZZ{MAX_SHEET_ROWS}",
                    value_render_option="FORMATTED_VALUE",
                )
            except TypeError:
                values = worksheet.get(f"A1:ZZ{MAX_SHEET_ROWS}")
            if not values:
                return []
            return [[(cell or "").strip() if isinstance(cell, str) else str(cell or "") for cell in row] for row in values][
                :MAX_SHEET_ROWS
            ]

        return self._call(_read)

    def get_headers(self, ref: SheetRef) -> list[str]:
        worksheet = self._open_worksheet(ref)
        rows = self._sheet_values(worksheet)
        if not rows:
            return []
        if is_calendar_matrix(rows):
            return []
        return [cell.strip() for cell in rows[0] if cell.strip()]

    def _records_from_rows(self, source: SheetRef, rows: list[list[str]]) -> list[Record]:
        spreadsheet_id = source.normalized_id()
        if not rows:
            return []
        rows = rows[:MAX_SHEET_ROWS]

        parsed = parse_sheet_rows(rows, source, spreadsheet_id)
        if parsed is not None:
            return parsed

        headers = [cell.strip() for cell in rows[0]]
        col_index = {header: idx + 1 for idx, header in enumerate(headers) if header}

        records: list[Record] = []
        for offset, row in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in row):
                continue
            raw = {
                headers[i]: row[i] if i < len(row) else ""
                for i in range(len(headers))
                if headers[i]
            }
            values = _apply_map(raw, source.map)
            if source.service:
                key = service_key(values) or "Тип услуги"
                if not str(values.get(key, "")).strip():
                    values[key] = source.service
            if source.address:
                key = address_key(values) or "Адрес"
                if not str(values.get(key, "")).strip():
                    values[key] = source.address
            records.append(
                Record(
                    source_name=source.name,
                    spreadsheet_id=spreadsheet_id,
                    sheet=source.sheet,
                    row=offset,
                    values=values,
                    sheet_headers=headers,
                    col_index=col_index,
                    map=source.map,
                    origin_values=dict(values),
                    kind=KIND_INFO if is_info_ref(source) else KIND_RECORDS,
                )
            )
        return records

    def _fetch_source_public(self, source: SheetRef) -> list[Record]:
        spreadsheet_id = source.normalized_id()
        sheet = (source.sheet or "").strip()
        titles = requested_sheet_titles(source.sheet)
        if titles:
            sheet = titles[0]
        elif sheet.lower() in {"", "*", "все", "all", "все листы"}:
            sheet = ""
        rows = _fetch_public_csv(spreadsheet_id, sheet)
        part = replace(source, sheet=sheet or source.sheet or "лист 1")
        return self._records_from_rows(part, rows)

    def fetch_source(self, source: SheetRef) -> list[Record]:
        worksheet = self._open_worksheet(source)
        rows = self._sheet_values(worksheet)
        return self._records_from_rows(source, rows)

    def expand_source(self, source: SheetRef) -> list[SheetRef]:
        available = self.list_sheets(source.spreadsheet_id)
        parts = _parts_for_source(source, available)
        if not parts:
            raise SheetsError(f"В «{source.name}» нет листов")
        return parts

    def _should_try_public_read(self, exc: BaseException) -> bool:
        if isinstance(exc, SheetsError):
            text = str(exc)
            return (
                "подключиться к Google" in text
                or "защищённого соединения" in text
                or "Публичное чтение" in text
                or "Не удалось скачать" in text
            )
        return _is_network_error(exc)

    def _load_source_public(self, source: SheetRef) -> list[Record]:
        sid = source.normalized_id()
        available: list[str] = []
        try:
            available = _list_public_sheet_titles(sid)
        except Exception:
            available = []
        # Не зовём Sheets API list_sheets здесь — на Windows это часто зависает
        # и весь UI остаётся на «Обновляю…».
        parts = _parts_for_source(source, available or None)
        loaded: list[Record] = []
        errors: list[Exception] = []
        for part in parts:
            try:
                loaded.extend(self._fetch_source_public(part))
            except Exception as exc:
                if is_info_ref(part) or is_info_title(part.sheet):
                    continue
                errors.append(exc)

        has_calendar = any(item.layout == "calendar" for item in loaded)
        # Если взяли только «УСЛУГИ», а календарь на другой вкладке — догружаем.
        if not has_calendar and available:
            for title in available:
                if is_info_title(title):
                    continue
                if any(part.sheet == title for part in parts):
                    continue
                try:
                    extra = self._fetch_source_public(replace(source, sheet=title))
                except Exception:
                    continue
                if any(item.layout == "calendar" for item in extra):
                    loaded.extend(extra)
                    has_calendar = True
                    for info_title in companion_info_titles(available, title):
                        if any(part.sheet == info_title for part in parts):
                            continue
                        try:
                            loaded.extend(
                                self._fetch_source_public(
                                    replace(source, sheet=info_title, kind=KIND_INFO)
                                )
                            )
                        except Exception:
                            pass
                    break

        if loaded:
            return loaded
        if errors:
            raise errors[0]
        raise SheetsError(f"Не удалось прочитать «{source.name}»")

    def _mark_public_read(self, source_name: str, spreadsheet_id: str = "") -> None:
        # Не вызываем probe_api здесь: на Windows SSL-запрос может зависнуть
        # и UI навсегда останется на «Обновляю… / Читаю…».
        self.read_only_public = True
        note = (
            f"{source_name}: календарь прочитан напрямую (CSV). "
            "Запись попробуем через API при сохранении ячейки."
        )
        if note not in self.read_notes:
            self.read_notes.append(note)

    def fetch_all(self, sources: list[SheetRef]) -> tuple[list[Record], list[str]]:
        records: list[Record] = []
        errors: list[str] = []
        self.read_only_public = False
        self.read_notes = []
        for source in sources:
            if source.is_placeholder():
                continue

            # Всегда сначала один лист через CSV — быстрее и без SSL-зависаний.
            if _can_try_public_read(source):
                try:
                    records.extend(self._load_source_public(source))
                    self._mark_public_read(source.name, source.normalized_id())
                    continue
                except Exception:
                    pass

            try:
                for part in self.expand_source(source):
                    records.extend(self.fetch_source(part))
            except Exception as exc:
                if _can_try_public_read(source) and self._should_try_public_read(exc):
                    try:
                        records.extend(self._load_source_public(source))
                        self._mark_public_read(source.name, source.normalized_id())
                    except Exception as pub_exc:
                        msg = str(pub_exc) if isinstance(pub_exc, SheetsError) else str(_friendly_error(pub_exc))
                        errors.append(f"{source.name}: {msg}")
                else:
                    msg = str(exc) if isinstance(exc, SheetsError) else str(_friendly_error(exc))
                    errors.append(f"{source.name}: {msg}")
        return records, errors

    def update_cell(self, record: Record, field: str, value: str) -> None:
        try:
            header = record.header_for(field)
        except KeyError as exc:
            raise SheetsError(f"В листе нет колонки «{field}»") from exc
        col = record.col_index[header]
        cell_value = value
        if record.layout == "calendar" and field == "Клиент":
            cell_value = value.strip() or "запись"
        input_option = _value_input_option(cell_value)

        def _do_gspread() -> None:
            worksheet = self._call(
                lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
            )
            a1 = f"{_col_to_a1(col)}{record.row}"

            def _write():
                worksheet.update(
                    a1,
                    [[cell_value]],
                    value_input_option=input_option,
                )

            self._call(_write)

        def _do_curl() -> None:
            _update_cell_via_curl(
                self._credentials_path,
                record.spreadsheet_id,
                record.sheet,
                record.row,
                col,
                cell_value,
            )

        def _do_powershell() -> None:
            _update_cell_via_powershell(
                self._credentials_path,
                record.spreadsheet_id,
                record.sheet,
                record.row,
                col,
                cell_value,
            )

        def _do_requests_insecure() -> None:
            _update_cell_via_requests_insecure(
                self._credentials_path,
                record.spreadsheet_id,
                record.sheet,
                record.row,
                col,
                cell_value,
            )

        errors: list[str] = []
        # На Windows Python-SSL к oauth2 часто мёртв — curl/PowerShell с обходом SSL.
        attempts: list[tuple[str, Callable]] = []

        def _do_insecure() -> None:
            self._connect(insecure=True)
            _do_gspread()

        def _read_gspread() -> str:
            worksheet = self._call(
                lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
            )
            return str(self._call(lambda: worksheet.cell(record.row, col).value) or "").strip()

        def _read_insecure() -> str:
            self._connect(insecure=True)
            return _read_gspread()

        def _read_requests_insecure() -> str:
            return _read_cell_via_requests_insecure(
                self._credentials_path,
                record.spreadsheet_id,
                record.sheet,
                record.row,
                col,
            )

        if sys.platform == "win32":
            attempts.append(("curl", _do_curl))
            attempts.append(("powershell", _do_powershell))
            attempts.append(("requests insecure", _do_requests_insecure))
            attempts.append(("gspread insecure", _do_insecure))
        else:
            attempts.append(("gspread", _do_gspread))
            attempts.append(("curl", _do_curl))
            attempts.append(("requests insecure", _do_requests_insecure))
            attempts.append(("gspread insecure", _do_insecure))

        ok = False
        for _label, action in attempts:
            try:
                action()
                ok = True
                break
            except Exception as exc:
                errors.append(f"{_label}: {exc}")
                msg = str(exc).lower()
                # Права / 403 — остальные каналы тот же токен, не жечь минуты.
                if "403" in msg or "permission" in msg or "access_denied" in msg:
                    break

        if not ok:
            account = getattr(self, "service_email", "") or ""
            tip = (
                f"Сервисный аккаунт: {account}\n"
                "1) Выдайте ему роль «Редактор» на эту таблицу.\n"
                "2) Отключите VPN и проверку HTTPS в антивирусе."
            )
            raise SheetsError(
                "Не удалось записать в Google Таблицу.\n"
                f"{tip}\n"
                f"Детали:\n- " + "\n- ".join(errors[:6] or ["нет деталей"])
            )

        expected = str(cell_value or "").strip()
        read_attempts: list[Callable[[], str]] = []
        if sys.platform == "win32":
            read_attempts.append(lambda: _read_cell_via_curl(self._credentials_path, record.spreadsheet_id, record.sheet, record.row, col))
            read_attempts.append(_read_requests_insecure)
            read_attempts.append(_read_insecure)
        else:
            read_attempts.append(_read_gspread)
            read_attempts.append(lambda: _read_cell_via_curl(self._credentials_path, record.spreadsheet_id, record.sheet, record.row, col))
            read_attempts.append(_read_requests_insecure)
            read_attempts.append(_read_insecure)

        last_read_error = ""
        confirmed = False
        for read_back in read_attempts:
            try:
                actual = str(read_back() or "").strip()
                confirmed = actual == expected
                if confirmed:
                    break
                last_read_error = f"после записи в ячейке осталось «{actual}»"
            except Exception as exc:
                last_read_error = str(exc)
        if not confirmed:
            raise SheetsError(
                "Google не подтвердил сохранение в ячейке.\n"
                f"Ожидали: «{expected or '(пусто)'}».\n"
                f"Детали: {last_read_error or 'значение не подтвердилось'}"
            )
        record.values[field] = value
        if header != field:
            record.values[header] = value
        self.read_only_public = False

    def read_cell(self, record: Record, field: str) -> str:
        try:
            header = record.header_for(field)
        except KeyError as exc:
            raise SheetsError(f"В листе нет колонки «{field}»") from exc
        col = record.col_index[header]

        def _read_gspread() -> str:
            worksheet = self._call(
                lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
            )
            return str(self._call(lambda: worksheet.cell(record.row, col).value) or "").strip()

        def _read_insecure() -> str:
            self._connect(insecure=True)
            return _read_gspread()

        def _read_requests_insecure() -> str:
            return _read_cell_via_requests_insecure(
                self._credentials_path,
                record.spreadsheet_id,
                record.sheet,
                record.row,
                col,
            )

        attempts: list[Callable[[], str]] = []
        if sys.platform == "win32":
            attempts.append(
                lambda: _read_cell_via_curl(
                    self._credentials_path, record.spreadsheet_id, record.sheet, record.row, col
                )
            )
            attempts.append(_read_requests_insecure)
            attempts.append(_read_insecure)
        else:
            attempts.append(_read_gspread)
            attempts.append(
                lambda: _read_cell_via_curl(
                    self._credentials_path, record.spreadsheet_id, record.sheet, record.row, col
                )
            )
            attempts.append(_read_requests_insecure)
            attempts.append(_read_insecure)

        errors: list[str] = []
        for action in attempts:
            try:
                return str(action() or "").strip()
            except Exception as exc:
                errors.append(str(exc))
                msg = str(exc).lower()
                if "403" in msg or "permission" in msg or "access_denied" in msg:
                    break
        raise SheetsError(
            "Не удалось прочитать ячейку из Google Таблицы.\n"
            + "\n".join(errors[:4] or ["нет деталей"])
        )

    def acquire_calendar_lock(self, record: Record) -> tuple[str, str]:
        """Ставит маркер «записывают» в слот. Возвращает (прежнее значение, текст блокировки)."""
        current = self.read_cell(record, "Клиент")
        if is_lock_text(current) and lock_is_fresh(current):
            raise SheetsError(
                "Этот слот сейчас заполняет другой оператор.\n"
                "Подождите немного или нажмите «Обновить»."
            )
        lock_text, _token = make_lock_text()
        self.update_cell(record, "Клиент", lock_text)
        actual = self.read_cell(record, "Клиент")
        if actual != lock_text:
            raise SheetsError(
                "Не удалось закрепить слот — его уже занял другой оператор.\n"
                "Обновите календарь и выберите другой слот."
            )
        return current, lock_text

    def assert_calendar_lock(self, record: Record, lock_text: str) -> None:
        """Перед сохранением: слот всё ещё наш."""
        current = self.read_cell(record, "Клиент")
        if current == lock_text:
            return
        if is_lock_text(current) and lock_is_fresh(current):
            raise SheetsError(
                "Слот перехватил другой оператор. Ваша запись не сохранена.\n"
                "Обновите календарь."
            )
        raise SheetsError(
            "Ячейка изменилась, пока вы её редактировали.\n"
            f"Сейчас в таблице: «{current or '(пусто)'}».\n"
            "Сохранение отменено — обновите календарь."
        )

    def append_row(self, dest: SheetRef, values: dict[str, str]) -> None:
        worksheet = self._open_worksheet(dest)
        headers = [cell.strip() for cell in self._call(lambda: worksheet.row_values(1))]
        if not any(headers):
            raise SheetsError(
                f"В «{dest.name}» нет заголовков в первой строке. "
                "Сначала подпишите колонки в Google Таблице."
            )
        row = [values.get(header, "") for header in headers]
        self._call(lambda: worksheet.append_row(row, value_input_option="USER_ENTERED"))

    def delete_row(self, record: Record) -> None:
        worksheet = self._call(lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet))
        self._call(lambda: worksheet.delete_rows(record.row))

    def list_sheets(self, url_or_id: str) -> list[str]:
        spreadsheet = self._open_spreadsheet(url_or_id)
        return [ws.title for ws in spreadsheet.worksheets()]


def values_for_destination(record: Record, headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    reverse_map = {sheet_col: internal for internal, sheet_col in record.map.items()}
    for header in headers:
        if header in record.values:
            out[header] = record.values.get(header, "")
            continue
        internal = reverse_map.get(header)
        if internal and internal in record.values:
            out[header] = record.values.get(internal, "")
    return out


def _apply_map(raw: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    if not mapping:
        return dict(raw)
    values = dict(raw)
    for internal, sheet_col in mapping.items():
        if sheet_col in raw:
            values[internal] = raw[sheet_col]
    return values
