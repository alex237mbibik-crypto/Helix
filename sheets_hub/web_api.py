"""Python API для HTML-интерфейса (pywebview) — та же логика, что у desktop-приложения."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from sheets_hub.auth import credential_kind
from sheets_hub.calendar_sheet import classify_slot, extract_phone, is_lock_text
from sheets_hub.client import SheetsClient
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
    writable_data_dir,
)
from sheets_hub.models import Record
from sheets_hub.registry import DEFAULT_REGISTRY_SHEET, tables_signature
from sheets_hub.split import explode_records, write_back_value
from sheets_hub.telegram import notify_booking_async, notify_free_async, send_message


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

    def set_window(self, window) -> None:
        self._window = window

    # --- helpers ---

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
        if self.client and after_city:
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
        matched = self._filter_tables()
        return matched or self._tables()

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

    def _slot_payload(self, record: Record) -> dict[str, Any]:
        status = record.values.get("Статус", "")
        client = record.values.get("Клиент", "").strip()
        phone = record.values.get("Телефон", "").strip()
        if status == "Не записывать":
            label, css = "не записывать", "blocked"
        elif status == "Записывают":
            label, css = "записывают…", "lock"
        elif status == "Занято":
            label = f"{client}" + (f" {phone}" if phone and phone not in client else "")
            css = "occupied"
        else:
            label, css = "запись", "free"
        return {
            "time": record.values.get("Время", ""),
            "date": record.values.get("Дата", ""),
            "label": label or "запись",
            "css": css,
            "status": status,
            "row": record.row,
            "sheet": record.sheet,
            "spreadsheet_id": record.spreadsheet_id,
            "source_name": record.source_name,
            "ask_pregnancy": self._is_gyn(record),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cal = self._calendar_records()
            dates = list(dict.fromkeys(r.values.get("Дата", "") for r in cal if r.values.get("Дата")))
            times = list(dict.fromkeys(r.values.get("Время", "") for r in cal if r.values.get("Время")))
            times.sort(key=_time_sort_key)
            cell_map = {
                f"{r.values.get('Время','')}|{r.values.get('Дата','')}": self._slot_payload(r)
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
            for record in self._info_records():
                text = str(record.values.get("Текст") or "").strip()
                if not text:
                    text = "\n".join(
                        str(v).strip()
                        for k, v in record.values.items()
                        if str(v).strip() and not str(k).startswith("_")
                    )
                if text:
                    info.append({"text": text, "tone": str(record.values.get("_tone") or "info")})
            email = ""
            if self.client:
                email = getattr(self.client, "service_email", "") or ""
            reg = (self.config.registry_spreadsheet_id or "").strip()
            if reg.upper().startswith("PASTE_"):
                reg = ""
            tg = self.config.telegram
            opts = self._cascade_options()
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

    def connect(self) -> dict[str, Any]:
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
            self.client = SheetsClient(path)
            self.config.credentials = path
            try:
                self._persist()
            except Exception:
                pass
            self.status = f"Подключено: {self.client.service_email}"
            self.reload()
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
            return self.connect()
        except Exception as exc:
            self.status = str(exc)
            return self.snapshot()

    def set_filters(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        for key in ("name", "service", "city", "address", "sheet", "search"):
            if key in data:
                self.filters[key] = str(data.get(key) or "").strip()
        # каскад: сброс зависимых при смене верхних
        return self.snapshot()

    def reload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        fast = bool(data.get("fast"))
        sync_registry = bool(data.get("sync_registry", not fast))
        if not self.client:
            self.status = "Нет подключения — укажите credentials.json"
            return self.snapshot()
        if not self._lock.acquire(blocking=False):
            return self.snapshot()
        try:
            sources = self._selected_sources()
            if sync_registry and self._registry_ready():
                try:
                    remote = self.client.pull_table_registry(
                        self.config.registry_spreadsheet_id,
                        self.config.registry_sheet or DEFAULT_REGISTRY_SHEET,
                    )
                    if remote:
                        self.config.sources = remote
                        self.config.destinations = list(remote)
                        sources = self._selected_sources()
                except Exception:
                    pass
            raw, errors = self.client.fetch_all(
                sources,
                include_colors=not fast,
                fast=fast,
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
            if errors:
                self.status = "; ".join(errors[:2])
            else:
                cal_n = len(self._calendar_records())
                booked = sum(
                    1 for r in self._calendar_records() if r.values.get("Статус") == "Занято"
                )
                self.status = (
                    f"{self.filters.get('name') or (sources[0].name if sources else 'Календарь')}: "
                    f"{cal_n} слотов, занято {booked}"
                )
        except Exception as exc:
            self.status = str(exc).split("\n")[0]
        finally:
            self._lock.release()
        return self.snapshot()

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
        client_text = str(data.get("client") or "").strip()
        pregnant = str(data.get("pregnant") or "").strip()
        freeing = bool(data.get("freeing"))
        if not freeing and self._is_gyn(record):
            if client_text and client_text.lower() != "запись" and "не запис" not in client_text.lower():
                if pregnant not in {"yes", "no"}:
                    return {"ok": False, "error": "Выберите: беременна или не беременна.", "need_pregnancy": True}

        previous = ""
        try:
            previous = self.client.read_cell(record, "Клиент")
        except Exception:
            previous = str(record.values.get("Клиент") or "")

        try:
            if freeing or not client_text or client_text.lower() == "запись":
                prev_status = classify_slot(previous)
                self.client.update_cell(record, "Клиент", "")
                if prev_status == "Занято":
                    notify_free_async(self.config.telegram, record, previous)
                self.status = "Слот освобождён"
            else:
                lock_prev, lock_text = self.client.acquire_calendar_lock(
                    record, previous_hint=previous
                )
                try:
                    to_write = write_back_value(record, "Клиент", client_text)
                    self.client.assert_calendar_lock(record, lock_text)
                    self.client.update_cell(record, "Клиент", to_write)
                    notify_booking_async(self.config.telegram, record, client_text)
                    self.status = "Запись сохранена"
                except Exception:
                    try:
                        self.client.update_cell(record, "Клиент", lock_prev, confirm=False)
                    except Exception:
                        pass
                    raise
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self.reload()
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
            else:
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
