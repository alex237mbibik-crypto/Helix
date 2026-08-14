from __future__ import annotations

import time
from pathlib import Path

import certifi
import gspread
from google.oauth2.service_account import Credentials

from sheets_hub.config import (
    KIND_INFO,
    KIND_RECORDS,
    SheetRef,
    is_info_ref,
    parse_spreadsheet_id,
    requested_sheet_titles,
)
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key
from sheets_hub.ssl_setup import configure_tls

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RETRY_ATTEMPTS = 4


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
            "Проверьте интернет, отключите VPN, в антивирусе выключите проверку HTTPS.\n"
            "В Google Cloud у проекта должен быть включён Google Sheets API."
        )
    return SheetsError(str(exc))


class SheetsClient:
    def __init__(self, credentials_path: Path) -> None:
        configure_tls()
        if not credentials_path.exists():
            raise SheetsError(
                "Нет ключа аккаунта Google.\n"
                "Ссылка на таблицу — это не вход. Нажмите «Таблицы» → «Выбрать JSON-ключ…» "
                "и укажите файл ключа сервисного аккаунта из Google Cloud."
            )
        creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        session = getattr(getattr(self._gc, "http_client", None), "session", None)
        if session is None:
            session = getattr(self._gc, "session", None)
        if session is not None:
            session.verify = certifi.where()
        self.service_email = creds.service_account_email

    def _call(self, work):
        last: BaseException | None = None
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
                time.sleep(1.2 * (attempt + 1))
        raise last  # pragma: no cover

    def _open_worksheet(self, ref: SheetRef):
        spreadsheet_id = ref.normalized_id()
        if not spreadsheet_id or spreadsheet_id.upper().startswith("PASTE_"):
            raise SheetsError(f"В «{ref.name}» не указан ID таблицы")
        try:
            spreadsheet = self._call(lambda: self._gc.open_by_key(spreadsheet_id))
            return spreadsheet.worksheet(ref.sheet)
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetsError(
                f"Таблица «{ref.name}» не найдена. "
                f"Выдайте доступ {self.service_email} (Редактор)."
            ) from exc
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetsError(f"В «{ref.name}» нет листа «{ref.sheet}»") from exc
        except gspread.exceptions.APIError as exc:
            raise SheetsError(f"Google API: {exc}") from exc
        except Exception as exc:
            raise _friendly_error(exc) from exc

    def get_headers(self, ref: SheetRef) -> list[str]:
        worksheet = self._open_worksheet(ref)
        return [cell.strip() for cell in self._call(lambda: worksheet.row_values(1)) if cell.strip()]

    def fetch_source(self, source: SheetRef) -> list[Record]:
        spreadsheet_id = source.normalized_id()
        worksheet = self._open_worksheet(source)
        rows = self._call(worksheet.get_all_values)()
        if not rows:
            return []

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

    def expand_source(self, source: SheetRef) -> list[SheetRef]:
        titles = requested_sheet_titles(source.sheet)
        if titles is None:
            titles = self.list_sheets(source.spreadsheet_id)
        if not titles:
            raise SheetsError(f"В «{source.name}» нет листов")
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

    def fetch_all(self, sources: list[SheetRef]) -> tuple[list[Record], list[str]]:
        records: list[Record] = []
        errors: list[str] = []
        for source in sources:
            if source.is_placeholder():
                continue
            try:
                for part in self.expand_source(source):
                    records.extend(self.fetch_source(part))
            except Exception as exc:
                errors.append(f"{source.name}: {_friendly_error(exc)}")
        return records, errors

    def update_cell(self, record: Record, field: str, value: str) -> None:
        try:
            header = record.header_for(field)
        except KeyError as exc:
            raise SheetsError(f"В листе нет колонки «{field}»") from exc
        col = record.col_index[header]
        worksheet = self._call(lambda: self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet))
        self._call(lambda: worksheet.update_cell(record.row, col, value))
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
        spreadsheet_id = parse_spreadsheet_id(url_or_id)
        spreadsheet = self._call(lambda: self._gc.open_by_key(spreadsheet_id))
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
