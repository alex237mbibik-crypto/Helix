from __future__ import annotations

import re
import secrets
import time

from sheets_hub.config import KIND_INFO, KIND_RECORDS, SheetRef, is_info_ref
from sheets_hub.models import Record
from sheets_hub.split import address_key, service_key

# Маркер в ячейке, пока оператор держит диалог записи. Виден другим после обновления.
LOCK_TTL_SEC = 120
_LOCK_PREFIX = "⏳"

_TIME_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}\s+)?(\d{1,2})[:.\-](\d{2})(?:[:.\-]\d{2})?\s*(?:am|pm)?\s*$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{5,}\d)")
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
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
)
_CRM_MARKERS = ("клиент", "фио", "телефон", "статус", "услуг", "дата", "время", "имя")
_FREE_MARKERS = {"", "запись", "свободно", "free", "открыто"}
_BLOCKED_MARKERS = {"не записывать", "нельзя", "выходной", "закрыто", "-"}


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def is_time(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_TIME_RE.match(raw))


def is_date_header(text: str) -> bool:
    raw = _norm(text)
    if not raw or is_time(text):
        return False
    if any(marker in raw for marker in _DATE_MARKERS):
        return True
    if re.search(r"\d{1,2}[./]\d{1,2}", raw):
        return True
    if re.search(r"\d{4}-\d{1,2}-\d{1,2}", raw):
        return True
    return False


def looks_like_crm(headers: list[str]) -> bool:
    blob = " ".join(headers).lower()
    return sum(1 for marker in _CRM_MARKERS if marker in blob) >= 2


def _row_width(rows: list[list[str]], limit: int = 40) -> int:
    width = 0
    for row in rows[:limit]:
        width = max(width, len(row))
    return width


def _count_times(rows: list[list[str]], start: int, col: int) -> int:
    count = 0
    for row in rows[start:]:
        if col < len(row) and is_time(row[col] if row else ""):
            count += 1
    return count


def find_calendar_layout(rows: list[list[str]]) -> tuple[int, int, list[int]] | None:
    """Return (header_row_index, time_column_index, date_column_indexes) or None."""
    best: tuple[int, int, int, list[int]] | None = None
    limit = min(12, len(rows))
    width = _row_width(rows)
    for header_idx in range(limit):
        header = rows[header_idx]
        date_cols = [idx for idx, cell in enumerate(header) if is_date_header(cell or "")]
        if len(date_cols) < 1:
            continue
        first_date = min(date_cols)
        # Время обычно слева от дат; на шаблонах с боковой колонкой это не col 0.
        candidates = list(range(0, first_date)) or [0]
        for col in range(width):
            if col not in date_cols and col not in candidates:
                candidates.append(col)
        time_col = 0
        time_count = 0
        for col in candidates:
            count = _count_times(rows, header_idx + 1, col)
            if count > time_count:
                time_count = count
                time_col = col
        if time_count < 2:
            continue
        # Даты должны быть справа от колонки времени (типичная сетка).
        date_cols = [idx for idx in date_cols if idx != time_col]
        if not date_cols:
            continue
        score = len(date_cols) * 10 + time_count
        # Предпочитаем время ближе к датам (меньше боковой «дыры»).
        score -= abs(min(date_cols) - time_col)
        if best is None or score > best[0]:
            best = (score, header_idx, time_col, date_cols)
    if best is None:
        return None
    return best[1], best[2], best[3]


def is_calendar_matrix(rows: list[list[str]]) -> bool:
    return find_calendar_layout(rows) is not None


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


def make_lock_text() -> tuple[str, str]:
    """Возвращает (текст для ячейки, token)."""
    token = secrets.token_hex(3)
    return f"{_LOCK_PREFIX}|{int(time.time())}|{token}|записывает", token


def is_lock_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.startswith(_LOCK_PREFIX):
        return True
    return _norm(raw).startswith("записывает")


def lock_age_sec(text: str) -> float | None:
    """Возраст блокировки в секундах; None если это не lock."""
    raw = (text or "").strip()
    if not is_lock_text(raw):
        return None
    parts = raw.split("|")
    if len(parts) >= 2 and parts[0].startswith(_LOCK_PREFIX):
        try:
            return max(0.0, time.time() - float(parts[1]))
        except ValueError:
            return 0.0
    return 0.0


def lock_is_fresh(text: str) -> bool:
    age = lock_age_sec(text)
    if age is None:
        return False
    return age < LOCK_TTL_SEC


def classify_slot(text: str) -> str:
    raw = _norm(text)
    if is_lock_text(text):
        return "Записывают"
    if raw in _BLOCKED_MARKERS or raw.startswith("не запис"):
        return "Не записывать"
    if raw in _FREE_MARKERS:
        return "Свободно"
    return "Занято"


def info_tone(text: str) -> str:
    raw = _norm(text)
    if any(part in raw for part in ("не работает", "девствен", "до 18", "детям", "паспорт", "не осуществ")):
        return "warn"
    if "страхов" in raw or "гарантий" in raw:
        return "note"
    if "врач" in raw or "категор" in raw or "услуг" in raw:
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
    end_index: int | None = None,
    skip_times: bool = False,
    time_col: int = 0,
) -> list[Record]:
    records: list[Record] = []
    stop = len(rows) if end_index is None else end_index
    for offset, row in enumerate(rows[start_index:stop], start=start_index + 1):
        if skip_times and row and time_col < len(row) and is_time(row[time_col]):
            continue
        text = _row_text(row)
        if not text:
            continue
        records.append(_info_record(source, spreadsheet_id, offset, text))
    return records


def _sidebar_cols(time_col: int, date_cols: list[int], width: int) -> list[int]:
    blocked = {time_col, *date_cols}
    return [idx for idx in range(width) if idx not in blocked]


def _extract_sidebar_info(
    rows: list[list[str]],
    source: SheetRef,
    spreadsheet_id: str,
    *,
    header_idx: int,
    last_time_index: int,
    time_col: int,
    date_cols: list[int],
) -> list[Record]:
    """Текст слева/рядом с сеткой (не колонка времени и не даты)."""
    width = _row_width(rows, last_time_index + 2)
    side_cols = _sidebar_cols(time_col, date_cols, width)
    if not side_cols:
        return []
    seen: set[str] = set()
    records: list[Record] = []
    start = max(0, header_idx)
    end = min(len(rows), last_time_index + 1)
    for row_idx in range(start, end):
        row = rows[row_idx]
        for col in side_cols:
            if col >= len(row):
                continue
            text = (row[col] or "").strip()
            if not text or is_time(text) or is_date_header(text):
                continue
            key = _norm(text)
            if key in seen:
                continue
            seen.add(key)
            records.append(_info_record(source, spreadsheet_id, row_idx + 1, text, col=col + 1))
    return records


def parse_calendar_rows(
    rows: list[list[str]],
    source: SheetRef,
    spreadsheet_id: str,
) -> list[Record]:
    layout = find_calendar_layout(rows)
    if layout is None:
        return []
    header_idx, time_col, date_cols = layout
    header = [(cell or "").strip() for cell in rows[header_idx]]
    corner = ""
    for row in rows[: header_idx + 1]:
        if time_col < len(row) and (row[time_col] or "").strip() and not is_time(row[time_col]):
            corner = row[time_col].strip()
            break
        if row and (row[0] or "").strip() and not is_time(row[0]):
            corner = row[0].strip()
            break
    address, service = parse_corner(corner)
    if source.address:
        address = source.address
    if source.service:
        service = source.service

    records: list[Record] = parse_info_rows(
        rows,
        source,
        spreadsheet_id,
        start_index=0,
        end_index=header_idx,
        skip_times=True,
        time_col=time_col,
    )

    last_time_index = header_idx
    for offset, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        time_text = (row[time_col] if time_col < len(row) else "").strip()
        if not is_time(time_text):
            continue
        last_time_index = offset - 1
        for col_idx in date_cols:
            cell = row[col_idx].strip() if col_idx < len(row) else ""
            status = classify_slot(cell)
            display = "" if status in {"Свободно", "Записывают"} else cell
            name, phone = extract_phone(display) if status == "Занято" else (display, "")
            if status in {"Свободно", "Записывают"}:
                name, phone = "", ""
            values = _with_tags(
                {
                    "Дата": header[col_idx] if col_idx < len(header) else "",
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

    records.extend(
        _extract_sidebar_info(
            rows,
            source,
            spreadsheet_id,
            header_idx=header_idx,
            last_time_index=last_time_index,
            time_col=time_col,
            date_cols=date_cols,
        )
    )
    records.extend(
        parse_info_rows(
            rows,
            source,
            spreadsheet_id,
            start_index=last_time_index + 1,
            skip_times=True,
            time_col=time_col,
        )
    )
    return records


def looks_like_notices(rows: list[list[str]]) -> bool:
    if not rows or find_calendar_layout(rows):
        return False
    first = [cell.strip() for cell in rows[0] if (cell or "").strip()]
    if not first:
        # Пустая первая строка — смотрим дальше (как на листе «УСЛУГИ врача»).
        for row in rows[:6]:
            first = [cell.strip() for cell in row if (cell or "").strip()]
            if first:
                break
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
