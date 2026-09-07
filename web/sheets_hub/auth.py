from __future__ import annotations

import json
from pathlib import Path

from sheets_hub.config import writable_data_dir


class AuthError(Exception):
    pass


def credential_kind(path: Path) -> str:
    """nextcloud | unknown"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    kind = str(data.get("type") or "").strip().lower()
    if kind == "nextcloud":
        return "nextcloud"
    if data.get("url") and data.get("username") and (
        data.get("app_password") or data.get("password")
    ):
        return "nextcloud"
    return "unknown"


def load_nextcloud_credentials(path: Path) -> dict[str, str]:
    """Читает credentials.json для Nextcloud (url / username / app_password)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthError(f"Не удалось прочитать {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthError("credentials.json должен быть JSON-объектом.")

    url = str(data.get("url") or data.get("base_url") or "").strip().rstrip("/")
    username = str(data.get("username") or data.get("user") or "").strip()
    password = str(
        data.get("app_password") or data.get("password") or data.get("appPassword") or ""
    ).strip()

    if not url or not username or not password:
        raise AuthError(
            "В credentials.json нужны поля:\n"
            "  type: nextcloud\n"
            "  url: https://ваш-nextcloud\n"
            "  username: логин\n"
            "  app_password: пароль приложения (Настройки → Безопасность)"
        )
    if not url.startswith(("http://", "https://")):
        raise AuthError("url должен начинаться с https:// или http://")

    return {
        "type": "nextcloud",
        "url": url,
        "username": username,
        "app_password": password,
    }


def account_label(credentials_path: Path, creds: dict[str, str] | None = None) -> str:
    try:
        data = creds or load_nextcloud_credentials(credentials_path)
    except Exception:
        return ""
    user = (data.get("username") or "").strip()
    host = (data.get("url") or "").strip()
    if user and host:
        return f"{user}@{host.replace('https://', '').replace('http://', '')}"
    return user or host or "Nextcloud"


def install_nextcloud_credentials(source: Path, dest: Path | None = None) -> Path:
    """Копирует / нормализует JSON Nextcloud в credentials.json."""
    target = dest or (writable_data_dir() / "credentials.json")
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Нужен JSON-объект с полями url, username, app_password.")
    if "app_password" not in data and data.get("password"):
        data["app_password"] = data["password"]
    data["type"] = "nextcloud"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Проверка полей до записи.
    tmp = target.parent / ".credentials_check.json"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        load_nextcloud_credentials(tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    # Не храним дублирующий password, если есть app_password.
    payload = {
        "type": "nextcloud",
        "url": str(data.get("url") or data.get("base_url") or "").strip().rstrip("/"),
        "username": str(data.get("username") or data.get("user") or "").strip(),
        "app_password": str(
            data.get("app_password") or data.get("password") or ""
        ).strip(),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
