from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import requests

from sheets_hub.calendar_sheet import current_operator_name, extract_phone
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


def format_booking_message(
    record: Record, client_text: str, *, operator: str = ""
) -> str:
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
    # Скрипт из «Общей информации» иногда ошибочно попадает в Адрес ячейки.
    try:
        from sheets_hub.calendar_sheet import looks_like_notice_text

        if looks_like_notice_text(address):
            address = ""
        if looks_like_notice_text(service):
            service = ""
    except Exception:
        if len(address) > 140 or address.count("\n") >= 2:
            address = ""
    if service:
        lines.append(f"Услуга: {service}")
    if address:
        lines.append(f"Адрес: {address}")
    if record.source_name:
        lines.append(f"Таблица: {record.source_name}")
    sheet = (record.sheet or "").strip()
    if sheet:
        lines.append(f"Лист: {sheet}")
    op = (operator or current_operator_name() or "").strip()
    if op:
        lines.append(f"Оператор: {op}")
    return "\n".join(lines)


def format_free_message(
    record: Record, previous_client: str = "", *, operator: str = ""
) -> str:
    date = str(record.values.get("Дата") or "").strip()
    time = str(record.values.get("Время") or "").strip()
    when = " · ".join(part for part in (date, time) if part)
    lines = ["Слот освобождён"]
    if when:
        lines.append(f"Когда: {when}")
    prev = (previous_client or "").strip()
    if prev and is_booking_value(prev) and not is_lock_text_safe(prev):
        name, phone = extract_phone(prev)
        lines.append(f"Было: {name or prev}")
        if phone:
            lines.append(f"Телефон: {phone}")
    if record.source_name:
        lines.append(f"Таблица: {record.source_name}")
    sheet = (record.sheet or "").strip()
    if sheet:
        lines.append(f"Лист: {sheet}")
    op = (operator or current_operator_name() or "").strip()
    if op:
        lines.append(f"Оператор: {op}")
    return "\n".join(lines)


def _norm_text(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def same_booking_text(previous: str, client_text: str) -> bool:
    """Ячейка уже содержит этого клиента — повторно в Telegram не пишем."""
    if not is_booking_value(previous) or not is_booking_value(client_text):
        return False
    return _norm_text(previous) == _norm_text(client_text)


# Одна и та же запись с нескольких сохранений (таймер, закрытие окна, повтор кнопки).
_DEDUPE_TTL_SEC = 10 * 60
_dedupe_guard = threading.Lock()
_dedupe_mem: dict[str, float] = {}


def _cell_prefix(kind: str, record: Record) -> str:
    try:
        col = int(record.col_index.get("Клиент") or 0)
    except Exception:
        col = 0
    return "|".join(
        (
            kind,
            (record.spreadsheet_id or "").strip(),
            (record.sheet or "").strip(),
            str(int(record.row or 0)),
            str(col),
            "",
        )
    )


def _dedupe_path() -> Path:
    from sheets_hub.config import writable_data_dir

    return writable_data_dir() / "telegram_sent.json"


@contextmanager
def _dedupe_file_lock() -> Iterator[None]:
    path = _dedupe_path().with_suffix(".lock")
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        yield
        return
    for _ in range(40):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 15:
                    path.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.025)
        except Exception:
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _read_dedupe(now: float) -> dict[str, float]:
    path = _dedupe_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    fresh: dict[str, float] = {}
    if isinstance(raw, dict):
        for key, stamp in raw.items():
            try:
                ts = float(stamp)
            except (TypeError, ValueError):
                continue
            if now - ts < _DEDUPE_TTL_SEC:
                fresh[str(key)] = ts
    return fresh


def _write_dedupe(data: dict[str, float]) -> None:
    path = _dedupe_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def claim_notification(kind: str, record: Record, text: str) -> str | None:
    """Ключ, если это уведомление ещё не уходило. None — уже отправляли."""
    key = _cell_prefix(kind, record) + _norm_text(text)
    now = time.time()
    with _dedupe_file_lock():
        with _dedupe_guard:
            global_drop = [k for k, ts in _dedupe_mem.items() if now - ts >= _DEDUPE_TTL_SEC]
            for old in global_drop:
                _dedupe_mem.pop(old, None)
            store = _read_dedupe(now)
            if key in store or key in _dedupe_mem:
                _dedupe_mem[key] = store.get(key, _dedupe_mem.get(key, now))
                return None
            if kind == "free":
                book_prefix = _cell_prefix("book", record)
                store = {k: v for k, v in store.items() if not k.startswith(book_prefix)}
                for old in [k for k in _dedupe_mem if k.startswith(book_prefix)]:
                    _dedupe_mem.pop(old, None)
            store[key] = now
            _dedupe_mem[key] = now
            _write_dedupe(store)
            return key


def release_notification(key: str) -> None:
    if not key:
        return
    with _dedupe_file_lock():
        with _dedupe_guard:
            _dedupe_mem.pop(key, None)
            store = _read_dedupe(time.time())
            store.pop(key, None)
            _write_dedupe(store)


def is_lock_text_safe(text: str) -> bool:
    try:
        from sheets_hub.calendar_sheet import is_lock_text

        return is_lock_text(text)
    except Exception:
        return False


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
    if result.returncode in (28, 52, 56):
        # Таймаут или обрыв ответа: Telegram уже мог принять сообщение.
        raise _MaybeDelivered(f"curl exit {result.returncode}")
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"curl exit {result.returncode}")
    return _parse_telegram_response(result.stdout)


class _MaybeDelivered(Exception):
    """Запрос, скорее всего, уже ушёл — повторять sendMessage нельзя."""


def _delivery_ambiguous(exc: BaseException) -> bool:
    if isinstance(exc, _MaybeDelivered):
        return True
    try:
        if isinstance(exc, (requests.ConnectTimeout, requests.exceptions.SSLError)):
            return False
        if isinstance(exc, requests.Timeout):
            return True
        if isinstance(exc, requests.ConnectionError):
            low = str(exc).lower()
            return any(
                part in low
                for part in ("timed out", "reset", "aborted", "disconnected", "broken pipe")
            )
    except Exception:
        pass
    low = str(exc).lower()
    return "timed out" in low or "timeout" in low or "curl exit 28" in low


def send_message(settings: TelegramConfig, text: str, *, timeout: float = 12.0) -> tuple[bool, str]:
    token = (settings.bot_token or "").strip()
    chat_id = _normalize_chat_id(settings.chat_id)
    if not TelegramConfig._is_real(token) or not TelegramConfig._is_real(chat_id):
        return False, "Замените PASTE_BOT_TOKEN_HERE / PASTE_CHAT_ID_HERE в config.yaml на свои значения"
    if ":" not in token:
        return False, "Похоже, токен бота неполный (обычно вида 123456:AA...)"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    # Следующий канал — только если запрос точно не ушёл.
    # Таймаут после отправки раньше давал второй и третий одинаковый алерт.
    attempts: list[tuple[str, Callable[[], tuple[bool, str]]]] = []
    if sys.platform == "win32":
        attempts.append(("curl", lambda: _send_via_curl(url, payload, timeout=timeout)))
    attempts.append(
        ("requests", lambda: _send_via_requests(url, payload, timeout=timeout, verify=True))
    )
    attempts.append(
        (
            "requests-insecure",
            lambda: _send_via_requests(url, payload, timeout=timeout, verify=False),
        )
    )
    if sys.platform != "win32":
        attempts.append(("curl", lambda: _send_via_curl(url, payload, timeout=timeout)))

    errors: list[str] = []
    for label, attempt in attempts:
        try:
            return attempt()
        except Exception as exc:
            if _delivery_ambiguous(exc):
                return True, ""
            errors.append(f"{label}: {exc}")

    return False, "; ".join(errors) if errors else "Не удалось отправить в Telegram"


def notify_booking_async(
    settings: TelegramConfig,
    record: Record,
    client_text: str,
    *,
    on_done: Callable[[bool, str], None] | None = None,
) -> None:
    if not TelegramConfig._is_real(settings.bot_token) or not TelegramConfig._is_real(
        settings.chat_id
    ):
        if on_done:
            on_done(False, "Telegram не настроен (замените заглушки в config.yaml)")
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
    key = claim_notification("book", record, client_text)
    if key is None:
        if on_done:
            on_done(True, "")
        return
    _notify_async(settings, message, on_done=on_done, dedupe_key=key)


def notify_free_async(
    settings: TelegramConfig,
    record: Record,
    previous_client: str = "",
    *,
    on_done: Callable[[bool, str], None] | None = None,
) -> None:
    if not TelegramConfig._is_real(settings.bot_token) or not TelegramConfig._is_real(
        settings.chat_id
    ):
        if on_done:
            on_done(False, "Telegram не настроен (замените заглушки в config.yaml)")
        return
    if not settings.enabled:
        if on_done:
            on_done(False, "Telegram выключен — включите в «Таблицы»")
        return

    message = format_free_message(record, previous_client)
    key = claim_notification("free", record, previous_client or message)
    if key is None:
        if on_done:
            on_done(True, "")
        return
    _notify_async(settings, message, on_done=on_done, dedupe_key=key)


def _notify_async(
    settings: TelegramConfig,
    message: str,
    *,
    on_done: Callable[[bool, str], None] | None = None,
    dedupe_key: str = "",
) -> None:
    snap = TelegramConfig(
        enabled=True,
        bot_token=settings.bot_token.strip(),
        chat_id=_normalize_chat_id(settings.chat_id),
    )

    def work() -> None:
        ok, err = send_message(snap, message)
        if not ok and dedupe_key:
            release_notification(dedupe_key)
        if on_done:
            try:
                on_done(ok, err)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()
