from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

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

    lines = ["Новая запись"]
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
    sheet = (record.sheet or "").strip()
    if sheet:
        lines.append(f"Лист: {sheet}")
    return "\n".join(lines)


def _normalize_chat_id(chat_id: str) -> str:
    raw = (chat_id or "").strip().replace(" ", "")
    return raw


def _parse_telegram_response(raw: str | bytes) -> tuple[bool, str]:
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
        data = json.loads(text) if text.strip() else {}
    except Exception as exc:
        return False, f"Непонятный ответ Telegram: {exc}"
    if data.get("ok"):
        return True, ""
    return False, str(data.get("description") or text or "ошибка Telegram")


def _send_via_requests(url: str, payload: dict, *, timeout: float, verify: bool) -> tuple[bool, str]:
    response = requests.post(url, json=payload, timeout=timeout, verify=verify)
    try:
        data = response.json()
    except Exception:
        return False, response.text or f"HTTP {response.status_code}"
    if not response.ok or not data.get("ok"):
        return False, str(data.get("description") or response.text or "ошибка Telegram")
    return True, ""


def _send_via_curl(url: str, payload: dict, *, timeout: float = 12.0) -> tuple[bool, str]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl не найден")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tmp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="sheets_hub_tg_", suffix=".json")
        os.close(fd)
        tmp_path = Path(name)
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        cmd = [
            curl,
            "-k",
            "-sS",
            "--max-time",
            str(int(timeout)),
            "--connect-timeout",
            "5",
            "-X",
            "POST",
            url,
            "-H",
            "Content-Type: application/json; charset=utf-8",
            "--data-binary",
            f"@{tmp_path}",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=int(timeout) + 3,
            creationflags=flags if sys.platform == "win32" else 0,
        )
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"curl exit {result.returncode}")
    return _parse_telegram_response(result.stdout)


def send_message(settings: TelegramConfig, text: str, *, timeout: float = 12.0) -> tuple[bool, str]:
    token = (settings.bot_token or "").strip()
    chat_id = _normalize_chat_id(settings.chat_id)
    if not token or not chat_id:
        return False, "Укажите токен бота и chat_id"
    if ":" not in token:
        return False, "Похоже, токен бота неполный (обычно вида 123456:AA...)"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    errors: list[str] = []
    # На Windows Python SSL часто ломается — сначала curl -k, как для Google.
    if sys.platform == "win32":
        try:
            return _send_via_curl(url, payload, timeout=timeout)
        except Exception as exc:
            errors.append(f"curl: {exc}")

    for verify in (True, False):
        try:
            return _send_via_requests(url, payload, timeout=timeout, verify=verify)
        except Exception as exc:
            errors.append(str(exc))

    if sys.platform != "win32":
        try:
            return _send_via_curl(url, payload, timeout=timeout)
        except Exception as exc:
            errors.append(f"curl: {exc}")

    return False, "; ".join(errors) if errors else "Не удалось отправить в Telegram"


def notify_booking_async(
    settings: TelegramConfig,
    record: Record,
    client_text: str,
    *,
    on_done: Callable[[bool, str], None] | None = None,
) -> None:
    if not settings.bot_token.strip() or not settings.chat_id.strip():
        if on_done:
            on_done(False, "Telegram не настроен (нет токена или chat_id)")
        return
    if not settings.enabled:
        if on_done:
            on_done(False, "Telegram выключен — включите в «Таблицы»")
        return
    if not is_booking_value(client_text):
        if on_done:
            on_done(False, "")
        return

    message = format_booking_message(record, client_text)
    # Копия настроек — чтобы поток не зависел от последующих правок UI.
    snap = TelegramConfig(
        enabled=True,
        bot_token=settings.bot_token.strip(),
        chat_id=_normalize_chat_id(settings.chat_id),
    )

    def work() -> None:
        ok, err = send_message(snap, message)
        if on_done:
            try:
                on_done(ok, err)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()
