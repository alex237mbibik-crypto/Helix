from __future__ import annotations

import csv
import io
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
    expand_ref_locally,
    is_info_ref,
    parse_spreadsheet_id,
    requested_sheet_titles,
)
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key
from sheets_hub.ssl_setup import configure_tls, session_verify_target

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RETRY_ATTEMPTS = 5
_HTTP_TIMEOUT = (10, 30)


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


def _public_session() -> requests.Session:
    configure_tls()
    session = requests.Session()
    session.verify = session_verify_target()
    session.headers["User-Agent"] = "SheetsHub/1.0"
    return session


def _fetch_public_csv(spreadsheet_id: str, sheet: str = "") -> list[list[str]]:
    if sheet.strip():
        url = (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
            f"?format=csv&sheet={quote(sheet.strip())}"
        )
    else:
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"
    try:
        resp = _public_session().get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        raise SheetsError(f"Публичное чтение таблицы не удалось: {exc}") from exc
    rows = list(csv.reader(io.StringIO(text)))
    return [[(cell or "").replace("\u202f", " ").strip() for cell in row] for row in rows]


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
        self._connect()

    def _connect(self) -> None:
        configure_tls()
        creds = Credentials.from_service_account_file(str(self._credentials_path), scopes=SCOPES)
        session = self._build_session(creds)
        self._gc = gspread.authorize(None, session=session)
        self._gc.set_timeout(_HTTP_TIMEOUT)
        self.service_email = creds.service_account_email

    def _build_session(self, creds: Credentials) -> AuthorizedSession:
        session = AuthorizedSession(creds)
        session.verify = session_verify_target()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=False,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers["Connection"] = "close"
        return session

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
                    self._connect()
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
        if titles and len(titles) == 1:
            sheet = titles[0]
        rows = _fetch_public_csv(spreadsheet_id, sheet)
        part = replace(source, sheet=sheet or source.sheet or "лист 1")
        return self._records_from_rows(part, rows)

    def fetch_source(self, source: SheetRef) -> list[Record]:
        spreadsheet_id = source.normalized_id()
        worksheet = self._open_worksheet(source)
        rows = self._sheet_values(worksheet)
        return self._records_from_rows(source, rows)

    def expand_source(self, source: SheetRef) -> list[SheetRef]:
        available = self.list_sheets(source.spreadsheet_id)
        if not available:
            raise SheetsError(f"В «{source.name}» нет листов")
        requested = requested_sheet_titles(source.sheet)
        if requested is None:
            titles = available
        else:
            titles = []
            for name in requested:
                match = self._match_sheet_title(available, name)
                if match and match not in titles:
                    titles.append(match)
            if not titles:
                titles = available
        many = len(titles) > 1
        expanded: list[SheetRef] = []
        for title in titles:
            expanded.append(
                SheetRef(
                    name=f"{source.name} / {title}" if many else source.name,
                    spreadsheet_id=source.spreadsheet_id,
                    sheet=title,
                    map=source.map,
                    service=source.service,
                    address=source.address,
                    kind=source.kind,
                )
            )
        return expanded

    def _should_try_public_read(self, exc: BaseException) -> bool:
        if isinstance(exc, SheetsError):
            return "подключиться к Google" in str(exc) or "защищённого соединения" in str(exc)
        return _is_network_error(exc)

    def fetch_all(self, sources: list[SheetRef]) -> tuple[list[Record], list[str]]:
        records: list[Record] = []
        errors: list[str] = []
        self.read_only_public = False
        self.read_notes = []
        for source in sources:
            if source.is_placeholder():
                continue
            try:
                for part in self.expand_source(source):
                    records.extend(self.fetch_source(part))
            except Exception as exc:
                if _can_try_public_read(source) and self._should_try_public_read(exc):
                    try:
                        for part in expand_ref_locally(source):
                            records.extend(self._fetch_source_public(part))
                        self.read_only_public = True
                        note = (
                            f"{source.name}: Google API недоступен — календарь прочитан напрямую. "
                            "Запись в таблицу может не работать, пока не починится SSL."
                        )
                        self.read_notes.append(note)
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
        worksheet = self._call(lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet))
        cell_value = value
        if record.layout == "calendar" and field == "Клиент":
            cell_value = value.strip() or "запись"
        self._call(lambda: worksheet.update_cell(record.row, col, cell_value))
        record.values[field] = value
        if header != field:
            record.values[header] = value

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
