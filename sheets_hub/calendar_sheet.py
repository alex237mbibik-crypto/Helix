from __future__ import annotations

import re

from sheets_hub.config import KIND_INFO, KIND_RECORDS, SheetRef, is_info_ref
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key

_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.\-](\d{2})\s*$")
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{6,}\d)")
_DATE_MARKERS = (
    "январ",
    "феврал",
    "март",
    "апрел",
    "мая",
    "май",
    "июн",
    "июл",
    "август",
    "сентябр",
    "октябр",
    "ноябр",
    "декабр",
    "пн",
    "вт",
    "ср",
    "чт",
    "пт",
    "сб",
    "вс",
)
_CRM_MARKERS = ("клиент", "фио", "телефон", "статус", "услуг", "дата", "время", "имя")
_FREE_MARKERS = {"", "запись", "свободно", "free", "открыто"}
_BLOCKED_MARKERS = {"не записывать", "нельзя", "выходной", "закрыто", "-"}


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def is_time(text: str) -> bool:
    return bool(_TIME_RE.match((text or "").strip()))


def is_date_header(text: str) -> bool:
    raw = _norm(text)
    if not raw:
        return False
    if any(marker in raw for marker in _DATE_MARKERS):
        return True
    return bool(re.search(r"\d{1,2}[./]\d{1,2}", raw))


def looks_like_crm(headers: list[str]) -> bool:
    blob = " ".join(headers).lower()
    return sum(1 for marker in _CRM_MARKERS if marker in blob) >= 2


def is_calendar_matrix(rows: list[list[str]]) -> bool:
    if len(rows) < 3:
        return False
    header = rows[0]
    date_cols = [idx for idx, cell in enumerate(header) if idx > 0 and is_date_header(cell)]
    time_rows = [row for row in rows[1:] if row and is_time(row[0] if row else "")]
    return len(date_cols) >= 2 and len(time_rows) >= 2


def parse_corner(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) >= 2 and not re.search(r"\d", parts[-1]) and len(parts[-1]) < 48:
        return ", ".join(parts[:-1]), parts[-1]
    return raw, ""


def extract_phone(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    match = _PHONE_RE.search(raw)
    if not match:
        return raw, ""
    phone = re.sub(r"\s+", " ", match.group(1)).strip()
    name = f"{raw[: match.start()]} {raw[match.end():]}".strip(" ,;")
    return name, phone


def classify_slot(text: str) -> str:
    raw = _norm(text)
    if raw in _BLOCKED_MARKERS or raw.startswith("не запис"):
        return "Не записывать"
    if raw in _FREE_MARKERS:
        return "Свободно"
    return "Занято"


def info_tone(text: str) -> str:
    raw = _norm(text)
    if any(part in raw for part in ("не работает", "девствен", "до 18", "детям")):
        return "warn"
    if "страхов" in raw:
        return "note"
    if "врач" in raw or "категор" in raw:
        return "ok"
    return "info"


def _row_text(row: list[str]) -> str:
    parts: list[str] = []
    for cell in row:
        text = (cell or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _with_tags(values: dict[str, str], source: SheetRef) -> dict[str, str]:
    out = dict(values)
    if source.service:
        key = service_key(out) or "Тип услуги"
        if not str(out.get(key, "")).strip():
            out[key] = source.service
    if source.address:
        key = address_key(out) or "Адрес"
        if not str(out.get(key, "")).strip():
            out[key] = source.address
    return out


def _info_record(
    source: SheetRef,
    spreadsheet_id: str,
    row_number: int,
    text: str,
    col: int = 1,
) -> Record:
    values = _with_tags({"Текст": text, "_tone": info_tone(text)}, source)
    return Record(
        source_name=source.name,
        spreadsheet_id=spreadsheet_id,
        sheet=source.sheet,
        row=row_number,
        values=values,
        sheet_headers=["Текст"],
        col_index={"Текст": col},
        map=source.map,
        origin_values=dict(values),
        kind=KIND_INFO,
        layout="info",
    )


def parse_info_rows(
    rows: list[list[str]],
    source: SheetRef,
    spreadsheet_id: str,
    start_index: int = 0,
) -> list[Record]:
    records: list[Record] = []
    for offset, row in enumerate(rows[start_index:], start=start_index + 1):
        text = _row_text(row)
        if not text:
            continue
        records.append(_info_record(source, spreadsheet_id, offset, text))
    return records


def parse_calendar_rows(
    rows: list[list[str]],
    source: SheetRef,
    spreadsheet_id: str,
) -> list[Record]:
    header = [(cell or "").strip() for cell in rows[0]]
    address, service = parse_corner(header[0] if header else "")
    if source.address:
        address = source.address
    if source.service:
        service = source.service

    date_cols = [idx for idx, cell in enumerate(header) if idx > 0 and is_date_header(cell)]
    records: list[Record] = []
    last_time_index = 0

    for offset, row in enumerate(rows[1:], start=2):
        time_text = (row[0] if row else "").strip()
        if not is_time(time_text):
            continue
        last_time_index = offset - 1
        for col_idx in date_cols:
            cell = row[col_idx].strip() if col_idx < len(row) else ""
            status = classify_slot(cell)
            display = "" if status == "Свободно" else cell
            name, phone = extract_phone(display) if status == "Занято" else (display, "")
            if status == "Свободно":
                name, phone = "", ""
            values = _with_tags(
                {
                    "Дата": header[col_idx],
                    "Время": time_text,
                    "Клиент": name or display,
                    "Телефон": phone,
                    "Статус": status,
                    "Адрес": address,
                    "Тип услуги": service,
                },
                source,
            )
            records.append(
                Record(
                    source_name=source.name,
                    spreadsheet_id=spreadsheet_id,
                    sheet=source.sheet,
                    row=offset,
                    values=values,
                    sheet_headers=["Клиент"],
                    col_index={"Клиент": col_idx + 1},
                    map=source.map,
                    origin_values=dict(values),
                    kind=KIND_RECORDS,
                    layout="calendar",
                )
            )

    records.extend(parse_info_rows(rows, source, spreadsheet_id, start_index=last_time_index + 1))
    return records


def looks_like_notices(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    first = [cell.strip() for cell in rows[0] if (cell or "").strip()]
    if not first:
        return False
    if looks_like_crm(first):
        return False
    if max(len(cell) for cell in first) >= 36:
        return True
    return len(first) == 1


def parse_sheet_rows(
    rows: list[list[str]],
    source: SheetRef,
    spreadsheet_id: str,
) -> list[Record] | None:
    if not rows:
        return []
    if is_calendar_matrix(rows):
        return parse_calendar_rows(rows, source, spreadsheet_id)
    if is_info_ref(source) or looks_like_notices(rows):
        return parse_info_rows(rows, source, spreadsheet_id)
    return None
