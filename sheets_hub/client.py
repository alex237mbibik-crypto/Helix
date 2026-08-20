from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import gspread
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from sheets_hub.calendar_sheet import is_calendar_matrix, parse_sheet_rows
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
_HTTP_TIMEOUT = (3, 12)


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
    return [[(cell or "").replace("\u202f", " ").strip() for cell in row] for row in rows]


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


def _can_try_public_read(source: SheetRef) -> bool:
    spreadsheet_id = source.normalized_id()
    return bool(spreadsheet_id) and not spreadsheet_id.upper().startswith("PASTE_")


class SheetsClient:
    def __init__(self, credentials_path: Path) -> None:
        configure_tls()
        if not credentials_path.exists():
            raise SheetsError(
                "Нет ключа аккаунта Google.\n"
                "Ссылка на таблицу — это не вход. Нажмите «Таблицы» → «Выбрать JSON-ключ…» "
                "и укажите файл ключа сервисного аккаунта из Google Cloud."
            )
        self._credentials_path = credentials_path
        self.read_only_public = False
        self.read_notes: list[str] = []
        self._api_insecure = False
        self._connect()

    def _connect(self, *, insecure: bool = False) -> None:
        configure_tls()
        creds = Credentials.from_service_account_file(str(self._credentials_path), scopes=SCOPES)
        session = self._build_session(creds, verify=False if insecure else None)
        self._gc = gspread.authorize(None, session=session)
        self._gc.set_timeout(_HTTP_TIMEOUT)
        self.service_email = creds.service_account_email
        self._api_insecure = insecure

    def _build_session(self, creds: Credentials, *, verify: bool | str | None = None) -> AuthorizedSession:
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
        return self._call(
            lambda: worksheet.get_all_values(value_render_option="FORMATTED_VALUE")
        )

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
        if not available:
            try:
                available = self.list_sheets(source.spreadsheet_id)
            except Exception:
                available = []
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
        # CSV-чтение не запрещает запись: если API жив — пишем как обычно.
        if spreadsheet_id and self.probe_api(spreadsheet_id):
            self.read_only_public = False
            return
        self.read_only_public = True
        self.read_notes.append(
            f"{source_name}: календарь прочитан напрямую (обход SSL). "
            "Запись в ячейки может не работать, пока не починится соединение с Google API."
        )

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

        def _do_update() -> None:
            worksheet = self._call(
                lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
            )
            self._call(lambda: worksheet.update_cell(record.row, col, cell_value))

        try:
            _do_update()
        except Exception as exc:
            # Повтор с отключённой проверкой SSL — частый обход на Windows.
            if _is_network_error(exc) and not getattr(self, "_api_insecure", False):
                try:
                    self._connect(insecure=True)
                    _do_update()
                except Exception as exc2:
                    raise SheetsError(
                        "Не удалось записать в Google Таблицу.\n"
                        "Чтение может работать через CSV, а запись идёт через API.\n"
                        "Отключите VPN и проверку HTTPS в антивирусе, "
                        f"проверьте доступ Редактора для {getattr(self, 'service_email', 'сервисного аккаунта')}.\n"
                        f"Детали: {exc2}"
                    ) from exc2
            else:
                raise SheetsError(
                    "Не удалось записать в Google Таблицу.\n"
                    f"Проверьте доступ Редактора для {getattr(self, 'service_email', 'сервисного аккаунта')}.\n"
                    f"Детали: {exc}"
                ) from exc
        record.values[field] = value
        if header != field:
            record.values[header] = value
        self.read_only_public = False

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
