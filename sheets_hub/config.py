from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _example_config_path() -> Path:
    next_to_app = app_root() / "config.example.yaml"
    if next_to_app.exists():
        return next_to_app
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", app_root())) / "config.example.yaml"
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parent.parent / "config.example.yaml"


ROOT = app_root()
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _example_config_path()

_ID_FROM_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def parse_spreadsheet_id(url_or_id: str) -> str:
    text = (url_or_id or "").strip()
    match = _ID_FROM_URL.search(text)
    if match:
        return match.group(1)
    if "/d/" in text:
        raise ValueError(f"Не удалось вытащить ID из ссылки: {text}")
    return text


@dataclass
class SheetRef:
    name: str
    spreadsheet_id: str
    sheet: str = "Лист1"
    map: dict[str, str] = field(default_factory=dict)
    service: str = ""
    address: str = ""

    def normalized_id(self) -> str:
        return parse_spreadsheet_id(self.spreadsheet_id)

    def is_placeholder(self) -> bool:
        text = (self.spreadsheet_id or "").strip()
        return not text or text.upper().startswith("PASTE_")

    def label(self) -> str:
        parts = [self.name]
        if self.service:
            parts.append(self.service)
        if self.address:
            parts.append(self.address)
        return " · ".join(parts)

    def dest_key(self) -> str:
        return f"{self.name}|{self.spreadsheet_id}|{self.sheet}|{self.service}|{self.address}"


Source = SheetRef


@dataclass
class AppConfig:
    credentials: Path
    sources: list[SheetRef]
    destinations: list[SheetRef]


def _load_refs(items: Any) -> list[SheetRef]:
    refs: list[SheetRef] = []
    for item in items or []:
        refs.append(
            SheetRef(
                name=str(item.get("name") or "Без имени"),
                spreadsheet_id=str(item.get("spreadsheet_id") or ""),
                sheet=str(item.get("sheet") or "Лист1"),
                map={str(k): str(v) for k, v in (item.get("map") or {}).items()},
                service=str(item.get("service") or item.get("услуга") or ""),
                address=str(item.get("address") or item.get("адрес") or ""),
            )
        )
    return refs


def _dump_ref(ref: SheetRef) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": ref.name,
        "spreadsheet_id": ref.spreadsheet_id,
        "sheet": ref.sheet,
    }
    if ref.map:
        payload["map"] = ref.map
    if ref.service:
        payload["service"] = ref.service
    if ref.address:
        payload["address"] = ref.address
    return payload


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            config_path.write_text(
                EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        else:
            raise FileNotFoundError("Нет config.yaml и config.example.yaml")

    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    creds = Path(raw.get("credentials") or "credentials.json")
    if not creds.is_absolute():
        creds = ROOT / creds

    destinations = _load_refs(raw.get("destinations") or raw.get("targets"))
    return AppConfig(
        credentials=creds,
        sources=_load_refs(raw.get("sources")),
        destinations=destinations,
    )


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config_path = path or CONFIG_PATH
    try:
        rel_creds = config.credentials.relative_to(ROOT)
        creds_value = str(rel_creds).replace("\\", "/")
    except ValueError:
        creds_value = str(config.credentials)

    payload = {
        "credentials": creds_value,
        "sources": [_dump_ref(source) for source in config.sources],
        "destinations": [_dump_ref(dest) for dest in config.destinations],
    }
    config_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
