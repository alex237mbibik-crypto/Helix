from __future__ import annotations

from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from sheets_hub.config import SheetRef, parse_spreadsheet_id
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(Exception):
    pass


class SheetsClient:
    def __init__(self, credentials_path: Path) -> None:
        if not credentials_path.exists():
            raise SheetsError(
                "Нет ключа аккаунта Google.\n"
                "Ссылка на таблицу — это не вход. Нажмите «Таблицы» → «Выбрать JSON-ключ…» "
                "и укажите файл ключа сервисного аккаунта из Google Cloud."
            )
        creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self.service_email = creds.service_account_email

    def _open_worksheet(self, ref: SheetRef):
        spreadsheet_id = ref.normalized_id()
        if not spreadsheet_id or spreadsheet_id.upper().startswith("PASTE_"):
            raise SheetsError(f"В «{ref.name}» не указан ID таблицы")
        try:
            spreadsheet = self._gc.open_by_key(spreadsheet_id)
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

    def get_headers(self, ref: SheetRef) -> list[str]:
        worksheet = self._open_worksheet(ref)
        return [cell.strip() for cell in worksheet.row_values(1) if cell.strip()]

    def fetch_source(self, source: SheetRef) -> list[Record]:
        spreadsheet_id = source.normalized_id()
        worksheet = self._open_worksheet(source)
        rows = worksheet.get_all_values()
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
                )
            )
        return records

    def fetch_all(self, sources: list[SheetRef]) -> tuple[list[Record], list[str]]:
        records: list[Record] = []
        errors: list[str] = []
        for source in sources:
            if source.is_placeholder():
                continue
            try:
                records.extend(self.fetch_source(source))
            except Exception as exc:
                errors.append(str(exc))
        return records, errors

    def update_cell(self, record: Record, field: str, value: str) -> None:
        try:
            header = record.header_for(field)
        except KeyError as exc:
            raise SheetsError(f"В листе нет колонки «{field}»") from exc
        col = record.col_index[header]
        worksheet = self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
        worksheet.update_cell(record.row, col, value)
        record.values[field] = value
        if header != field:
            record.values[header] = value

    def append_row(self, dest: SheetRef, values: dict[str, str]) -> None:
        worksheet = self._open_worksheet(dest)
        headers = [cell.strip() for cell in worksheet.row_values(1)]
        if not any(headers):
            raise SheetsError(
                f"В «{dest.name}» нет заголовков в первой строке. "
                "Сначала подпишите колонки в Google Таблице."
            )
        row = [values.get(header, "") for header in headers]
        worksheet.append_row(row, value_input_option="USER_ENTERED")

    def delete_row(self, record: Record) -> None:
        worksheet = self._gc.open_by_key(record.spreadsheet_id).worksheet(record.sheet)
        worksheet.delete_rows(record.row)

    def list_sheets(self, url_or_id: str) -> list[str]:
        spreadsheet_id = parse_spreadsheet_id(url_or_id)
        spreadsheet = self._gc.open_by_key(spreadsheet_id)
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
