"""Python API для HTML-интерфейса (pywebview) — та же логика, что у desktop-приложения."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sheets_hub.auth import credential_kind
from sheets_hub.calendar_sheet import classify_slot, extract_phone, is_lock_text
from sheets_hub.config import (
    KIND_INFO,
    SheetRef,
    TelegramConfig,
    find_credentials_file,
    install_credentials,
    is_info_title,
    load_config,
    merge_tables,
    resolve_config_path,
    save_config,
    usable_refs,
    user_data_dir,
    writable_data_dir,
)
from sheets_hub.models import Record
from sheets_hub.registry import DEFAULT_REGISTRY_SHEET, tables_signature
from sheets_hub.split import explode_records, write_back_value

if TYPE_CHECKING:
    from sheets_hub.client import SheetsClient


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _short_time(text: str) -> str:
    match = re.search(r"(\d{1,2})[:.\-](\d{2})", text or "")
    if not match:
        return (text or "").strip()
    return f"{int(match.group(1))}:{match.group(2)}"


def _time_sort_key(text: str) -> tuple[int, int]:
    match = re.search(r"(\d{1,2})[:.\-](\d{2})", text or "")
    if not match:
        return (99, 99)
    return int(match.group(1)), int(match.group(2))


def _lighten_hex(bg_hex: str, amount: float = 0.2) -> str:
    """Смешать цвет с белым — ячейки врача чуть ярче на экране."""
    raw = (bg_hex or "").lstrip("#")
    if len(raw) != 6:
        return bg_hex or ""
    try:
        r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return bg_hex
    amount = max(0.0, min(0.6, float(amount)))
    r = int(round(r + (255 - r) * amount))
    g = int(round(g + (255 - g) * amount))
    b = int(round(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def _mark_color(key: str) -> tuple[str, str]:
    """Быстрый стабильный цвет смены — без Google Sheets API."""
    from sheets_hub.client import contrast_fg

    raw = " ".join((key or "").strip().lower().split())
    if not raw:
        return "", ""
    digest = hashlib.md5(raw.encode("utf-8")).digest()
    channels = [
        150 + digest[0] % 85,
        150 + digest[1] % 85,
        150 + digest[2] % 85,
    ]
    channels[digest[3] % 3] = 110 + digest[4] % 55
    bg = _lighten_hex(f"#{channels[0]:02x}{channels[1]:02x}{channels[2]:02x}", 0.12)
    return bg, contrast_fg(bg)


def _doctor_key(name: str) -> str:
    """Один ключ для одного врача — по фамилии (первое слово)."""
    raw = " ".join((name or "").strip().lower().replace("ё", "е").split())
    if not raw:
        return ""
    return raw.split()[0]


def _doctor_names_in_text(text: str) -> list[str]:
    """ФИО из текста справки — строки с заглавной буквы, без телефонов."""
    names: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 80:
            continue
        low = line.lower()
        if any(x in low for x in ("тел", "запись", "прием", "приём", "кабинет", "услуг")):
            continue
        if re.search(r"\+?\d[\d\s\-()]{5,}", line):
            # есть телефон — берём часть до запятой/цифры как ФИО
            chunk = re.split(r"[,+]", line)[0].strip()
            if len(chunk.split()) >= 2:
                names.append(chunk)
            continue
        if len(line.split()) >= 2 and line[0].isupper():
            names.append(line)
    return names


def _info_title_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return (text or "").strip()


def _ui_cache_path() -> Path:
    return user_data_dir() / "ui_cache.json"


def _load_ui_cache() -> dict[str, Any]:
    path = _ui_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ui_cache(data: dict[str, Any]) -> None:
    path = _ui_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:
        pass


def _ref_to_dict(ref: SheetRef) -> dict[str, str]:
    return {
        "name": ref.name or "",
        "spreadsheet_id": ref.spreadsheet_id or "",
        "sheet": ref.sheet or "все",
        "service": ref.service or "",
        "city": ref.city or "",
        "address": ref.address or "",
        "kind": ref.kind or "records",
    }


def _dict_to_ref(item: dict[str, Any]) -> SheetRef:
    return SheetRef(
        name=str(item.get("name") or "").strip() or "Таблица",
        spreadsheet_id=str(item.get("spreadsheet_id") or item.get("url") or "").strip(),
        sheet=str(item.get("sheet") or "все").strip() or "все",
        service=str(item.get("service") or "").strip(),
        city=str(item.get("city") or "").strip(),
        address=str(item.get("address") or "").strip(),
        kind=str(item.get("kind") or "records"),
    )


class HelixApi:
    """Методы вызываются из JS через pywebview.api.*"""

    def __init__(self) -> None:
        self.config = load_config()
        self.client: SheetsClient | None = None
        self.records: list[Record] = []
        self.status = "Загрузка…"
        self.filters = {
            "name": "",
            "service": "",
            "city": "",
            "address": "",
            "sheet": "",
            "search": "",
        }
        self._window = None
        self._lock = threading.RLock()
        self._auto_tick = 0
        self._preferred_cache: dict[str, str] = {}
        self._active_lock: dict[str, Any] | None = None
        self._restore_ui_cache()

    def set_window(self, window) -> None:
        self._window = window

    def _restore_ui_cache(self) -> None:
        cache = _load_ui_cache()
        prefs = cache.get("preferred_sheets")
        if isinstance(prefs, dict):
            self._preferred_cache = {
                str(k): str(v).strip() for k, v in prefs.items() if str(k).strip() and str(v).strip()
            }
        saved = cache.get("filters")
        if isinstance(saved, dict):
            for key in ("name", "service", "city", "address", "sheet"):
                val = str(saved.get(key) or "").strip()
                if val:
                    self.filters[key] = val

    def _persist_ui_cache(self) -> None:
        _save_ui_cache(
            {
                "preferred_sheets": dict(self._preferred_cache),
                "filters": {
                    k: self.filters.get(k, "")
                    for k in ("name", "service", "city", "address", "sheet")
                },
            }
        )

    def _apply_cached_preferred(self) -> None:
        if not self.client or not self._preferred_cache:
            return
        for sid, title in self._preferred_cache.items():
            try:
                self.client.set_preferred_calendar_sheet(sid, title)
            except Exception:
                pass

    def _tables(self) -> list[SheetRef]:
        tables = merge_tables(self.config.sources, self.config.destinations)
        return tables or usable_refs(self.config.sources) or usable_refs(self.config.destinations)

    def _persist(self) -> str:
        save_config(self.config)
        return str(resolve_config_path())

    def _is_gyn(self, record: Record) -> bool:
        parts = [
            record.source_name,
            str(record.values.get("Тип услуги") or ""),
            str(record.values.get("Услуга") or ""),
            self.filters.get("service", ""),
            self.filters.get("name", ""),
        ]
        for ref in self._tables():
            if ref.spreadsheet_id == record.spreadsheet_id:
                parts.extend([ref.name, ref.service])
        return "гинекол" in " ".join(parts).lower().replace("ё", "е")

    def _filter_tables(self) -> list[SheetRef]:
        items = [ref for ref in self._tables() if not ref.is_placeholder()]
        name = _norm(self.filters["name"])
        service = _norm(self.filters["service"])
        city = _norm(self.filters["city"])
        address = _norm(self.filters["address"])
        out: list[SheetRef] = []
        for ref in items:
            if name and _norm(ref.name) != name:
                continue
            if service and _norm(ref.service) != service:
                continue
            if city and _norm(ref.resolved_city()) != city:
                continue
            if address and _norm(ref.address) != address:
                continue
            out.append(ref)
        return out

    def _cascade_options(self) -> dict[str, list[str]]:
        items = [ref for ref in self._tables() if not ref.is_placeholder()]
        name = _norm(self.filters["name"])
        after_name = [r for r in items if not name or _norm(r.name) == name]
        services = sorted({r.service.strip() for r in after_name if r.service.strip()})
        service = _norm(self.filters["service"])
        after_service = [r for r in after_name if not service or _norm(r.service) == service]
        cities = sorted({r.resolved_city() for r in after_service if r.resolved_city()})
        city = _norm(self.filters["city"])
        after_city = [
            r for r in after_service if not city or _norm(r.resolved_city()) == city
        ]
        addresses = sorted({r.address.strip() for r in after_city if r.address.strip()})
        names = sorted({r.name.strip() for r in items if r.name.strip()})
        sheets: list[str] = []
        if after_city:
            # Сначала из уже загруженных слотов — без лишнего htmlview при каждом snapshot.
            seen: list[str] = []
            for record in self.records:
                if record.layout == "calendar" and record.sheet and record.sheet not in seen:
                    seen.append(record.sheet)
            sheets = seen
            if not sheets and self.client:
                sid = after_city[0].spreadsheet_id
                try:
                    sheets = self.client.list_calendar_sheet_titles(sid)
                except Exception:
                    sheets = []
        return {
            "names": names,
            "services": services,
            "cities": cities,
            "addresses": addresses,
            "sheets": sheets,
        }

    def _selected_sources(self) -> list[SheetRef]:
        # Как в CTk: одна активная таблица, иначе клиент тянет всё подряд.
        matched = self._filter_tables()
        if matched:
            return [matched[0]]
        tables = [ref for ref in self._tables() if not ref.is_placeholder()]
        return [tables[0]] if tables else []

    def _active_spreadsheet_id(self) -> str:
        sources = self._selected_sources()
        if not sources:
            return ""
        try:
            return sources[0].normalized_id()
        except ValueError:
            return ""

    def _apply_preferred_sheet(self) -> None:
        """Лист месяца из фильтра → preferred в клиенте (иначе CSV тянет не тот лист)."""
        if not self.client:
            return
        sid = self._active_spreadsheet_id()
        title = (self.filters.get("sheet") or "").strip()
        if sid and title and title.lower() not in {"лист", "—", "-"}:
            self.client.set_preferred_calendar_sheet(sid, title)
            self._preferred_cache[sid] = title

    def _sync_sheet_filter(self, titles: list[str] | None = None) -> None:
        """После загрузки выставить лист месяца (preferred / первый календарный)."""
        sid = self._active_spreadsheet_id()
        values = list(titles or [])
        if not values and self.client and sid:
            # Не ходим в сеть повторно на fast-старте — только из уже загруженных записей.
            for item in self.records:
                if item.layout == "calendar" and item.sheet and item.sheet not in values:
                    values.append(item.sheet)
        if not values:
            for item in self.records:
                if item.layout == "calendar" and item.sheet:
                    values = [item.sheet]
                    break
        preferred = ""
        if self.client and sid:
            preferred = self.client.preferred_calendar_sheet(sid) or self._preferred_cache.get(sid, "")
        current = (self.filters.get("sheet") or "").strip()
        if current and values and current not in values:
            current = ""
        if not current:
            current = preferred or (values[0] if values else "")
        if current:
            self.filters["sheet"] = current
            if self.client and sid:
                self.client.set_preferred_calendar_sheet(sid, current)
                self._preferred_cache[sid] = current
        self._persist_ui_cache()

    def _calendar_records(self) -> list[Record]:
        sheet = (self.filters.get("sheet") or "").strip()
        query = _norm(self.filters.get("search") or "")
        out: list[Record] = []
        for record in self.records:
            if record.layout != "calendar":
                continue
            if sheet and record.sheet != sheet:
                continue
            if query:
                blob = " ".join(
                    [
                        record.source_name,
                        record.values.get("Клиент", ""),
                        record.values.get("Телефон", ""),
                        record.values.get("Дата", ""),
                        record.values.get("Время", ""),
                    ]
                ).lower()
                if query not in blob:
                    continue
            out.append(record)
        return out

    def _info_records(self) -> list[Record]:
        out: list[Record] = []
        seen: set[str] = set()
        for record in self.records:
            if record.kind != KIND_INFO and not is_info_title(record.sheet):
                continue
            text = str(record.values.get("Текст") or "").strip()
            if not text:
                text = "\n".join(
                    str(v).strip()
                    for k, v in record.values.items()
                    if str(v).strip() and not str(k).startswith("_")
                )
            key = " ".join(text.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(record)
        return out

    def _slot_payload(
        self,
        record: Record,
        *,
        day_colors: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        from sheets_hub.client import contrast_fg, soften_fill

        status = record.values.get("Статус", "")
        client = record.values.get("Клиент", "").strip()
        phone = record.values.get("Телефон", "").strip()
        if status == "Не записывать":
            label, css = "не записывать", "blocked"
            bg, fg = "#eef2f6", "#5a6f84"
        elif status == "Записывают":
            label, css = "записывают…", "lock"
            bg, fg = "#fff3cd", "#3e2723"
        elif status == "Занято":
            label = f"{client}" + (f" {phone}" if phone and phone not in client else "")
            css = "occupied"
            bg, fg = "", ""
        else:
            label, css = "запись", "free"
            bg, fg = "", ""
        if css not in {"blocked", "lock"}:
            sheet_bg = str(record.values.get("_bg") or "").strip()
            if sheet_bg:
                soft = soften_fill(sheet_bg, fallback="#2e7d32")
                bg = _lighten_hex(soft, 0.22)
                fg = contrast_fg(bg)
            else:
                date = str(record.values.get("Дата") or "").strip()
                mark = (day_colors or {}).get(date)
                if mark and mark[0]:
                    bg, fg = mark
        return {
            "time": record.values.get("Время", ""),
            "date": record.values.get("Дата", ""),
            "label": label or "запись",
            "css": css,
            "status": status,
            "bg": bg,
            "fg": fg,
            "row": record.row,
            "sheet": record.sheet,
            "spreadsheet_id": record.spreadsheet_id,
            "source_name": record.source_name,
            "ask_pregnancy": self._is_gyn(record),
            "editable": css in {"free", "occupied"},
        }

    def _shift_mark_colors(
        self, cal: list[Record], info_records: list[Record]
    ) -> tuple[dict[str, tuple[str, str]], dict[int, tuple[str, str]]]:
        """
        Цвета смены: сначала заливка из Google Sheets (_bg), иначе стабильный цвет врача.
        """
        from sheets_hub.client import contrast_fg, soften_fill

        dates = list(
            dict.fromkeys(str(r.values.get("Дата") or "").strip() for r in cal if r.values.get("Дата"))
        )
        date_doctor: dict[str, str] = {}
        date_bg_counts: dict[str, dict[str, int]] = {}
        for record in cal:
            date = str(record.values.get("Дата") or "").strip()
            if not date:
                continue
            doc = str(record.values.get("_doctor") or "").strip()
            if doc and date not in date_doctor:
                date_doctor[date] = doc
            raw_bg = str(record.values.get("_bg") or "").strip()
            if raw_bg:
                counts = date_bg_counts.setdefault(date, {})
                counts[raw_bg] = counts.get(raw_bg, 0) + 1

        weekday_tokens = (
            ("понедельник", "пн"),
            ("вторник", "вт"),
            ("среда", "ср"),
            ("четверг", "чт"),
            ("пятница", "пт"),
            ("суббота", "сб"),
            ("воскресенье", "вс"),
        )

        def _dates_mentioned(text: str) -> list[str]:
            low = (text or "").lower().replace("ё", "е")
            found: list[str] = []
            for date in dates:
                dlow = date.lower().replace("ё", "е")
                hit = False
                for full, short in weekday_tokens:
                    if (full in dlow or short in dlow.split()) and (
                        full in low or re.search(rf"(^|\W){re.escape(short)}(\W|$)", low)
                    ):
                        hit = True
                        break
                if not hit:
                    nums = re.findall(r"\b0*([1-9]\d?)\b", dlow)
                    for num in nums:
                        if re.search(rf"(^|\W)0*{re.escape(num)}(\W|$)", low):
                            hit = True
                            break
                if hit:
                    found.append(date)
            return found

        def _soft_pair(raw: str) -> tuple[str, str]:
            soft = soften_fill(raw, fallback="#2e7d32")
            bg = _lighten_hex(soft, 0.22)
            return bg, contrast_fg(bg)

        # canonical name by surname key
        key_to_name: dict[str, str] = {}
        for doc in date_doctor.values():
            key = _doctor_key(doc)
            if key and key not in key_to_name:
                key_to_name[key] = doc

        info_meta: list[tuple[Record, str, list[str]]] = []
        for record in info_records:
            text = str(record.values.get("Текст") or "").strip()
            if not text:
                text = "\n".join(
                    str(v).strip()
                    for k, v in record.values.items()
                    if str(v).strip() and not str(k).startswith("_")
                )
            if not text:
                continue
            names = _doctor_names_in_text(text)
            if not names:
                title = _info_title_line(text)
                if title and len(title.split()) >= 2:
                    names = [title]
            info_meta.append((record, text, names))
            for name in names:
                key = _doctor_key(name)
                if key and key not in key_to_name:
                    key_to_name[key] = name

        # Fallback: один цвет на врача, если Google ещё не отдал заливку.
        doctor_colors: dict[str, tuple[str, str]] = {}
        for key, name in key_to_name.items():
            doctor_colors[key] = _mark_color(name)

        day_colors: dict[str, tuple[str, str]] = {}
        info_colors: dict[int, tuple[str, str]] = {}

        for date, doc in date_doctor.items():
            key = _doctor_key(doc)
            if key in doctor_colors:
                day_colors[date] = doctor_colors[key]

        # Google Sheets — главный источник цвета дня.
        for date, counts in date_bg_counts.items():
            if not counts:
                continue
            raw = max(counts.items(), key=lambda item: item[1])[0]
            day_colors[date] = _soft_pair(raw)

        for record, text, names in info_meta:
            sheet_bg = str(record.values.get("_bg") or "").strip()
            if sheet_bg:
                info_colors[id(record)] = _soft_pair(sheet_bg)
                continue

            blob = text.lower().replace("ё", "е")
            linked: list[str] = []
            chosen_key = ""

            for name in names:
                key = _doctor_key(name)
                if not key:
                    continue
                for date, doc in date_doctor.items():
                    if _doctor_key(doc) == key:
                        linked.append(date)
                if not linked and key in blob:
                    linked = _dates_mentioned(text)
                if linked:
                    chosen_key = key
                    break

            if not linked:
                for key, doc_name in key_to_name.items():
                    if key and key in blob:
                        linked = [
                            date for date, d in date_doctor.items() if _doctor_key(d) == key
                        ]
                        if not linked:
                            linked = _dates_mentioned(text)
                        if linked:
                            chosen_key = key
                            break

            if chosen_key and chosen_key in doctor_colors:
                color = doctor_colors[chosen_key]
                # Если у связанных дней уже есть Google-цвет — берём его для блока.
                for date in linked:
                    if date in day_colors and date in date_bg_counts:
                        color = day_colors[date]
                        break
                info_colors[id(record)] = color
                for date in linked:
                    if date not in date_bg_counts:
                        day_colors[date] = color

        return day_colors, info_colors

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cal = self._calendar_records()
            dates = list(dict.fromkeys(r.values.get("Дата", "") for r in cal if r.values.get("Дата")))
            times = list(dict.fromkeys(r.values.get("Время", "") for r in cal if r.values.get("Время")))
            times.sort(key=_time_sort_key)
            infos = self._info_records()
            day_colors, info_colors = self._shift_mark_colors(cal, infos)
            cell_map = {
                f"{r.values.get('Время','')}|{r.values.get('Дата','')}": self._slot_payload(
                    r, day_colors=day_colors
                )
                for r in cal
            }
            sample = cal[0] if cal else None
            title_parts = []
            if sample:
                title_parts = [
                    sample.source_name,
                    sample.values.get("Адрес", ""),
                    sample.values.get("Тип услуги", ""),
                ]
            title = " · ".join(p for p in title_parts if p)
            booked = sum(1 for r in cal if r.values.get("Статус") == "Занято")
            info = []
            for record in infos:
                text = str(record.values.get("Текст") or "").strip()
                if not text:
                    text = "\n".join(
                        str(v).strip()
                        for k, v in record.values.items()
                        if str(v).strip() and not str(k).startswith("_")
                    )
                if not text:
                    continue
                bg, fg = info_colors.get(id(record), ("", ""))
                info.append(
                    {
                        "text": text,
                        "tone": str(record.values.get("_tone") or "info"),
                        "bg": bg,
                        "fg": fg,
                    }
                )
            email = ""
            if self.client:
                email = getattr(self.client, "service_email", "") or ""
            reg = (self.config.registry_spreadsheet_id or "").strip()
            if reg.upper().startswith("PASTE_"):
                reg = ""
            tg = self.config.telegram
            opts = self._cascade_options()
            day_doctors = [
                {
                    "date": date,
                    "doctor": "",
                    "bg": day_colors.get(date, ("", ""))[0],
                    "fg": day_colors.get(date, ("", ""))[1],
                }
                for date in dates
                if day_colors.get(date, ("", ""))[0]
            ]
            return {
                "ok": True,
                "status": self.status,
                "email": email,
                "filters": dict(self.filters),
                "options": opts,
                "calendar": {
                    "title": title,
                    "dates": dates,
                    "times": [_short_time(t) for t in times],
                    "times_raw": times,
                    "cells": cell_map,
                    "slots": len(cal),
                    "booked": booked,
                    "day_doctors": day_doctors,
                },
                "info": info,
                "registry": {
                    "spreadsheet_id": reg,
                    "sheet": self.config.registry_sheet or DEFAULT_REGISTRY_SHEET,
                },
                "telegram": {
                    "enabled": bool(tg.enabled),
                    "bot_token": tg.bot_token or "",
                    "chat_id": tg.chat_id or "",
                    "configured": tg.is_configured(),
                },
                "tables": [_ref_to_dict(r) for r in usable_refs(self._tables())],
                "config_path": str(resolve_config_path()),
                "ui": {
                    "has_password": bool((self.config.ui.tables_password or "").strip()),
                },
            }

    # --- public API for JS ---

    def get_state(self) -> dict[str, Any]:
        return self.snapshot()

    def connect(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        do_reload = bool(data.get("reload"))
        path = find_credentials_file(self.config.credentials)
        if path is None:
            self.client = None
            self.status = f"Нет credentials.json — нужен файл: {writable_data_dir() / 'credentials.json'}"
            return self.snapshot()
        if credential_kind(path) != "service_account":
            self.client = None
            self.status = "Нужен JSON сервисного аккаунта (type: service_account)."
            return self.snapshot()
        try:
            from sheets_hub.client import SheetsClient

            self.client = SheetsClient(path)
            self.config.credentials = path
            self._apply_cached_preferred()
            try:
                self._persist()
            except Exception:
                pass
            self.status = f"Подключено: {self.client.service_email}"
            if do_reload:
                return self.reload({"fast": True, "sync_registry": False})
        except Exception as exc:
            self.client = None
            self.status = str(exc).split("\n")[0]
        return self.snapshot()

    def pick_credentials(self) -> dict[str, Any]:
        import webview

        if self._window is None:
            return {"ok": False, "error": "Окно не готово"}
        dialog = getattr(webview, "FileDialog", None)
        open_mode = getattr(dialog, "OPEN", None) if dialog is not None else None
        if open_mode is None:
            open_mode = getattr(webview, "OPEN_DIALOG", 10)
        paths = self._window.create_file_dialog(
            open_mode,
            allow_multiple=False,
            file_types=("JSON Files (*.json)",),
        )
        if not paths:
            return self.snapshot()
        try:
            installed = install_credentials(Path(paths[0]))
            self.config.credentials = installed
            self._persist()
            return self.connect({"reload": True})
        except Exception as exc:
            self.status = str(exc)
            return self.snapshot()

    def set_filters(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        prev_top = {
            k: (self.filters.get(k) or "") for k in ("name", "service", "city", "address")
        }
        for key in ("name", "service", "city", "address", "sheet", "search"):
            if key in data:
                self.filters[key] = str(data.get(key) or "").strip()
        top_changed = any(
            (self.filters.get(k) or "") != prev_top[k] for k in prev_top
        )
        # Смена таблицы — сбрасываем лист, reload выставит месяц заново.
        if top_changed:
            self.filters["sheet"] = ""
        self._apply_preferred_sheet()
        self._persist_ui_cache()
        return self.snapshot()

    def reload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        fast = bool(data.get("fast", True))
        sync_registry = bool(data.get("sync_registry", False))
        # cache — мгновенно из памяти; fetch — узкий запрос к Google после CSV;
        # skip — без цветов.
        colors_mode = str(data.get("colors") or "cache").strip().lower()
        if colors_mode not in {"skip", "cache", "fetch"}:
            colors_mode = "cache"
        refresh_sheets = bool(data.get("refresh_sheets", False))
        if not self.client:
            self.status = "Нет подключения — укажите credentials.json"
            return self.snapshot()
        if data.get("background"):
            got_lock = self._lock.acquire(blocking=False)
        else:
            wait = 3.0 if fast else 20.0
            got_lock = self._lock.acquire(blocking=True, timeout=wait)
        if not got_lock:
            if data.get("background"):
                return self.snapshot()
            self.status = "Обновление ещё идёт…"
            return self.snapshot()
        try:
            sources = self._selected_sources()
            if not sources:
                self.records = []
                self.status = "Нет таблиц. Откройте настройки и загрузите список из облака."
                return self.snapshot()
            if sync_registry and self._registry_ready():
                try:
                    remote = self.client.pull_table_registry(
                        self.config.registry_spreadsheet_id,
                        self.config.registry_sheet or DEFAULT_REGISTRY_SHEET,
                    )
                    if remote:
                        self.config.sources = remote
                        self.config.destinations = list(remote)
                        sources = self._selected_sources() or sources
                except Exception:
                    pass
            self._apply_preferred_sheet()
            sheet_titles: list[str] = []
            sid = ""
            try:
                sid = sources[0].normalized_id()
            except ValueError:
                sid = ""
            known_sheet = (self.filters.get("sheet") or "").strip()
            if not known_sheet and sid:
                known_sheet = (
                    self.client.preferred_calendar_sheet(sid)
                    or self._preferred_cache.get(sid, "")
                ).strip()
                if known_sheet:
                    self.filters["sheet"] = known_sheet
                    self._apply_preferred_sheet()
            if (not known_sheet) or refresh_sheets:
                try:
                    sheet_titles = self.client.list_calendar_sheet_titles(
                        sid or sources[0].spreadsheet_id
                    )
                except Exception:
                    sheet_titles = []
                if not known_sheet and sheet_titles:
                    self.filters["sheet"] = sheet_titles[0]
                    self._apply_preferred_sheet()
            # CSV сразу; цвета Google — отдельно (узкий диапазон), чтобы UI не ждал.
            raw, errors = self.client.fetch_all(
                sources,
                include_colors=False,
                fast=True,
            )
            booking = [item for item in raw if item.kind != KIND_INFO]
            self.records = explode_records(booking)
            infos = [i for i in raw if i.kind == KIND_INFO or is_info_title(i.sheet)]
            seen = {(r.spreadsheet_id, r.sheet, r.row) for r in self.records}
            for item in infos:
                key = (item.spreadsheet_id, item.sheet, item.row)
                if key not in seen:
                    self.records.append(item)
                    seen.add(key)
            if colors_mode == "cache":
                try:
                    self.client.apply_cached_colors(self.records)
                except Exception:
                    pass
            elif colors_mode == "fetch":
                try:
                    self.client.apply_cached_colors(self.records)
                except Exception:
                    pass
                try:
                    self.client._attach_sheet_colors(self.records, force=True)
                except Exception:
                    pass
            self._sync_sheet_filter(sheet_titles)
            if errors:
                self.status = "; ".join(errors[:2])
            else:
                cal_n = len(self._calendar_records())
                booked = sum(
                    1 for r in self._calendar_records() if r.values.get("Статус") == "Занято"
                )
                sheet_note = self.filters.get("sheet") or ""
                self.status = (
                    f"{self.filters.get('name') or sources[0].name}"
                    f"{(' · ' + sheet_note) if sheet_note else ''}: "
                    f"{cal_n} слотов, занято {booked}"
                )
        except Exception as exc:
            self.status = str(exc).split("\n")[0]
        finally:
            self._lock.release()
        return self.snapshot()

    def fetch_colors(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Догрузить заливки Google по узкому диапазону — после быстрого CSV."""
        data = payload if isinstance(payload, dict) else {}
        force = bool(data.get("force", True))
        if not self.client or not self.records:
            return self.snapshot()
        got_lock = self._lock.acquire(blocking=False)
        if not got_lock:
            return self.snapshot()
        try:
            self.client._attach_sheet_colors(self.records, force=force)
        except Exception:
            pass
        finally:
            self._lock.release()
        return self.snapshot()

    def begin_slot(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Открытие слота: защита «не записывать» + маркер «записывают» для чужих ПК."""
        data = payload or {}
        if not self.client:
            return {"ok": False, "error": "Нет подключения к Google"}
        record = self._find_record(
            str(data.get("spreadsheet_id") or ""),
            str(data.get("sheet") or ""),
            int(data.get("row") or 0),
        )
        if record is None:
            return {"ok": False, "error": "Слот не найден — обновите календарь"}
        status = record.values.get("Статус", "")
        if status == "Не записывать":
            return {
                "ok": False,
                "blocked": True,
                "error": "В эту ячейку нельзя записывать.",
            }
        if status == "Записывают":
            return {
                "ok": False,
                "locked": True,
                "error": "Этот слот сейчас заполняет другой оператор.\n"
                "Подождите или нажмите «Обновить».",
            }
        if status == "Занято":
            return {
                "ok": True,
                "mode": "edit",
                "lock_text": "",
                "lock_prev": str(record.values.get("Клиент") or ""),
                "ask_pregnancy": self._is_gyn(record),
            }

        previous = str(record.values.get("Клиент") or "")
        try:
            previous = self.client.read_cell(record, "Клиент")
        except Exception:
            pass
        from sheets_hub.calendar_sheet import classify_slot

        live = classify_slot(previous)
        if live == "Не записывать":
            record.values["Статус"] = "Не записывать"
            return {
                "ok": False,
                "blocked": True,
                "error": "В эту ячейку нельзя записывать.",
            }
        if live == "Записывают":
            record.values["Статус"] = "Записывают"
            return {
                "ok": False,
                "locked": True,
                "error": "Этот слот сейчас заполняет другой оператор.\n"
                "Подождите или нажмите «Обновить».",
            }
        if live == "Занято":
            record.values["Статус"] = "Занято"
            record.values["Клиент"] = previous
            return {
                "ok": True,
                "mode": "edit",
                "lock_text": "",
                "lock_prev": previous,
                "ask_pregnancy": self._is_gyn(record),
            }
        try:
            lock_prev, lock_text = self.client.acquire_calendar_lock(
                record, previous_hint=previous
            )
        except Exception as exc:
            return {"ok": False, "locked": True, "error": str(exc)}
        record.values["Статус"] = "Записывают"
        record.values["Клиент"] = lock_text
        self._active_lock = {
            "spreadsheet_id": record.spreadsheet_id,
            "sheet": record.sheet,
            "row": record.row,
            "lock_text": lock_text,
            "lock_prev": lock_prev,
        }
        return {
            "ok": True,
            "mode": "book",
            "lock_text": lock_text,
            "lock_prev": lock_prev,
            "ask_pregnancy": self._is_gyn(record),
        }

    def cancel_slot(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Снять маркер «записывают», если окно закрыли без сохранения."""
        data = payload or {}
        lock_text = str(data.get("lock_text") or "").strip()
        lock_prev = data.get("lock_prev")
        if lock_prev is None and self._active_lock:
            lock_prev = self._active_lock.get("lock_prev", "")
        if not lock_text and self._active_lock:
            lock_text = str(self._active_lock.get("lock_text") or "")
        record = self._find_record(
            str(data.get("spreadsheet_id") or (self._active_lock or {}).get("spreadsheet_id") or ""),
            str(data.get("sheet") or (self._active_lock or {}).get("sheet") or ""),
            int(data.get("row") or (self._active_lock or {}).get("row") or 0),
        )
        self._active_lock = None
        if not self.client or record is None or not lock_text:
            return {"ok": True, **self.snapshot()}
        try:
            current = self.client.read_cell(record, "Клиент")
            if current == lock_text:
                self.client.update_cell(
                    record, "Клиент", str(lock_prev or ""), confirm=False
                )
        except Exception:
            pass
        return {"ok": True, **self.reload({"fast": True, "sync_registry": False, "colors": "cache"})}

    def unlock_settings(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        expected = (self.config.ui.tables_password or "").strip()
        got = str(data.get("password") or "").strip()
        if expected and got != expected:
            return {"ok": False, "error": "Неверный пароль"}
        return {"ok": True}

    def _registry_ready(self) -> bool:
        text = (self.config.registry_spreadsheet_id or "").strip()
        return bool(text) and not text.upper().startswith("PASTE_")

    def _find_record(self, spreadsheet_id: str, sheet: str, row: int) -> Record | None:
        for record in self.records:
            if (
                record.spreadsheet_id == spreadsheet_id
                and record.sheet == sheet
                and record.row == int(row)
            ):
                return record
        return None

    def book_slot(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        if not self.client:
            return {"ok": False, "error": "Нет подключения к Google"}
        record = self._find_record(
            str(data.get("spreadsheet_id") or ""),
            str(data.get("sheet") or ""),
            int(data.get("row") or 0),
        )
        if record is None:
            return {"ok": False, "error": "Слот не найден — обновите календарь"}
        status = record.values.get("Статус", "")
        if status == "Не записывать":
            return {"ok": False, "error": "В эту ячейку нельзя записывать.", "blocked": True}
        client_text = str(data.get("client") or "").strip()
        pregnant = str(data.get("pregnant") or "").strip()
        freeing = bool(data.get("freeing"))
        lock_text = str(data.get("lock_text") or "").strip()
        lock_prev = data.get("lock_prev")
        if not freeing and self._is_gyn(record):
            if client_text and client_text.lower() != "запись" and "не запис" not in client_text.lower():
                if pregnant not in {"yes", "no"}:
                    return {"ok": False, "error": "Выберите: беременна или не беременна.", "need_pregnancy": True}

        previous = ""
        try:
            previous = self.client.read_cell(record, "Клиент")
        except Exception:
            previous = str(record.values.get("Клиент") or "")

        live = classify_slot(previous)
        if not freeing and live == "Не записывать":
            return {"ok": False, "error": "В эту ячейку нельзя записывать.", "blocked": True}

        try:
            if freeing or not client_text or client_text.lower() == "запись":
                from sheets_hub.telegram import notify_free_async

                prev_status = classify_slot(previous)
                # Снять свой lock или очистить занятый слот.
                if lock_text and previous == lock_text:
                    self.client.update_cell(record, "Клиент", str(lock_prev or ""), confirm=False)
                else:
                    self.client.update_cell(record, "Клиент", "")
                if prev_status == "Занято":
                    notify_free_async(self.config.telegram, record, previous)
                self.status = "Слот освобождён"
            else:
                from sheets_hub.telegram import notify_booking_async

                if lock_text:
                    try:
                        self.client.assert_calendar_lock(record, lock_text)
                    except Exception as exc:
                        return {"ok": False, "error": str(exc), "locked": True}
                    to_write = write_back_value(record, "Клиент", client_text)
                    self.client.update_cell(record, "Клиент", to_write)
                else:
                    acquired_prev, acquired_lock = self.client.acquire_calendar_lock(
                        record, previous_hint=previous
                    )
                    try:
                        to_write = write_back_value(record, "Клиент", client_text)
                        self.client.assert_calendar_lock(record, acquired_lock)
                        self.client.update_cell(record, "Клиент", to_write)
                    except Exception:
                        try:
                            self.client.update_cell(
                                record, "Клиент", acquired_prev, confirm=False
                            )
                        except Exception:
                            pass
                        raise
                notify_booking_async(self.config.telegram, record, client_text)
                self.status = "Запись сохранена"
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._active_lock = None
        self.reload({"fast": True, "sync_registry": False, "colors": "cache"})
        return {"ok": True, **self.snapshot()}

    def save_telegram(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        token = str(data.get("bot_token") or "").strip()
        chat = str(data.get("chat_id") or "").strip()
        if not token and (self.config.telegram.bot_token or "").strip():
            token = self.config.telegram.bot_token.strip()
        if not chat and (self.config.telegram.chat_id or "").strip():
            chat = self.config.telegram.chat_id.strip()
        if "enabled" in data:
            self.config.telegram.enabled = bool(data.get("enabled"))
        self.config.telegram.bot_token = token
        self.config.telegram.chat_id = chat
        try:
            path = self._persist()
            self.status = f"Telegram сохранён: {path}"
            return {"ok": True, **self.snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self.snapshot()}

    def test_telegram(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from sheets_hub.telegram import send_message

        self.save_telegram({**(payload or {}), "enabled": True})
        settings = TelegramConfig(
            enabled=True,
            bot_token=self.config.telegram.bot_token,
            chat_id=self.config.telegram.chat_id,
        )
        if not TelegramConfig._is_real(settings.bot_token) or not TelegramConfig._is_real(
            settings.chat_id
        ):
            return {"ok": False, "error": "Замените заглушки токена/chat_id"}
        ok, err = send_message(settings, "Тест Sheets Hub — уведомления работают.")
        if ok:
            self.status = "Тест Telegram отправлен"
            return {"ok": True, **self.snapshot()}
        return {"ok": False, "error": err or "Не удалось отправить", **self.snapshot()}

    def save_registry(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        reg = str(data.get("spreadsheet_id") or "").strip()
        sheet = str(data.get("sheet") or "").strip() or DEFAULT_REGISTRY_SHEET
        saved = (self.config.registry_spreadsheet_id or "").strip()
        if (not reg or reg.upper().startswith("PASTE_")) and saved:
            reg = saved
        if reg and not reg.upper().startswith("PASTE_"):
            self.config.registry_spreadsheet_id = reg
        self.config.registry_sheet = sheet
        try:
            self._persist()
            return {"ok": True, **self.snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self.snapshot()}

    def load_cloud_tables(self) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "error": "Нет подключения"}
        if not self._registry_ready():
            return {"ok": False, "error": "Укажите ссылку на служебную таблицу"}
        try:
            refs = self.client.pull_table_registry(
                self.config.registry_spreadsheet_id,
                self.config.registry_sheet or DEFAULT_REGISTRY_SHEET,
            )
            if refs:
                self.config.sources = refs
                self.config.destinations = list(refs)
                self._persist()
                self.status = f"Загружено из облака: {len(refs)} таблиц"
                return {"ok": True, **self.reload({"fast": False, "sync_registry": False})}
            self.status = "В облаке пока пусто"
            return {"ok": True, **self.snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self.snapshot()}

    def save_cloud_tables(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        self.save_registry(data.get("registry") or data)
        raw_tables = data.get("tables")
        if isinstance(raw_tables, list):
            refs = [_dict_to_ref(item) for item in raw_tables if str(item.get("spreadsheet_id") or item.get("url") or "").strip()]
            if refs:
                self.config.sources = refs
                self.config.destinations = list(refs)
        refs = usable_refs(self._tables())
        if not self.client:
            return {"ok": False, "error": "Нет подключения"}
        if not self._registry_ready():
            return {"ok": False, "error": "Укажите ссылку на служебную таблицу"}
        if not refs:
            return {"ok": False, "error": "Добавьте хотя бы одну таблицу со ссылкой"}
        try:
            self._persist()
            written = self.client.push_table_registry(
                self.config.registry_spreadsheet_id,
                refs,
                self.config.registry_sheet or DEFAULT_REGISTRY_SHEET,
            )
            if written:
                self.config.registry_sheet = written
                self._persist()
            self.status = f"Сохранено для всех ПК: {len(refs)} таблиц"
            return {"ok": True, "signature": tables_signature(refs), **self.snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self.snapshot()}
