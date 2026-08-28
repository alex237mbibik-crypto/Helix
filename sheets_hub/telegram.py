from __future__ import annotations

import threading
from dataclasses import dataclass

import requests

from sheets_hub.calendar_sheet import extract_phone
from sheets_hub.config import TelegramConfig
from sheets_hub.models import Record


def is_booking_value(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    low = text.lower()
    if low == "запись":
        return False
    if "не запис" in low:
        return False
    return True


def format_booking_message(record: Record, client_text: str) -> str:
    date = str(record.values.get("Дата") or "").strip()
    time = str(record.values.get("Время") or "").strip()
    when = " · ".join(part for part in (date, time) if part)
    name, phone = extract_phone(client_text)
    if not name:
        name = client_text.strip()

    lines = ["📅 Новая запись"]
    if when:
        lines.append(f"Когда: {when}")
    lines.append(f"Клиент: {name}")
    if phone:
        lines.append(f"Телефон: {phone}")

    service = str(record.values.get("Тип услуги") or record.values.get("Услуга") or "").strip()
    address = str(record.values.get("Адрес") or "").strip()
    if service:
        lines.append(f"Услуга: {service}")
    if address:
        lines.append(f"Адрес: {address}")
    if record.source_name:
        lines.append(f"Таблица: {record.source_name}")
    return "\n".join(lines)


def send_message(settings: TelegramConfig, text: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    token = (settings.bot_token or "").strip()
    chat_id = (settings.chat_id or "").strip()
    if not token or not chat_id:
        return False, "Укажите токен бота и chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
        data = response.json()
    except Exception as exc:
        return False, str(exc)
    if not response.ok or not data.get("ok"):
        desc = str(data.get("description") or response.text or "ошибка Telegram")
        return False, desc
    return True, ""


def notify_booking_async(settings: TelegramConfig, record: Record, client_text: str) -> None:
    if not settings.is_configured() or not is_booking_value(client_text):
        return
    message = format_booking_message(record, client_text)

    def work() -> None:
        try:
            send_message(settings, message)
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()
