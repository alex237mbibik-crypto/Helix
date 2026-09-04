from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Папка с правами на запись (AppData), без запросов админа."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / "SheetsHub"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "SheetsHub"
    else:
        path = Path.home() / ".config" / "SheetsHub"
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        fallback = Path.home() / "SheetsHub"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except Exception:
            return app_root()


def legacy_user_data_dir() -> Path:
    return user_data_dir()


def _dir_is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".sheets_hub_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def writable_data_dir() -> Path:
    """Рядом с exe, если можно писать; иначе тихо в AppData (без UAC)."""
    root = app_root()
    if _dir_is_writable(root):
        return root
    return user_data_dir()


_RESOLVED_CONFIG_PATH: Path | None = None


def resolve_config_path() -> Path:
    """config.yaml: рядом с exe или %LOCALAPPDATA%\\SheetsHub — без запросов прав."""
    global _RESOLVED_CONFIG_PATH
    if _RESOLVED_CONFIG_PATH is not None:
        return _RESOLVED_CONFIG_PATH

    root = app_root()
    root_cfg = root / "config.yaml"
    user_cfg = user_data_dir() / "config.yaml"

    if user_cfg.exists():
        _RESOLVED_CONFIG_PATH = user_cfg
        return _RESOLVED_CONFIG_PATH

    if _dir_is_writable(root):
        _RESOLVED_CONFIG_PATH = root_cfg
        return _RESOLVED_CONFIG_PATH

    # Program Files: читаем заглушку рядом с exe, пишем копию в AppData.
    if root_cfg.exists():
        try:
            user_cfg.write_text(root_cfg.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    _RESOLVED_CONFIG_PATH = user_cfg
    return _RESOLVED_CONFIG_PATH


def reset_resolved_config_path() -> None:
    global _RESOLVED_CONFIG_PATH
    _RESOLVED_CONFIG_PATH = None


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
# Фактический путь: рядом с exe или AppData — через resolve_config_path().
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _example_config_path()


def find_credentials_file(configured: Path | str | None = None) -> Path | None:
    """Ищет credentials рядом с программой и в AppData."""
    candidates: list[Path] = []
    data = writable_data_dir()
    legacy = user_data_dir()
    if configured:
        raw = Path(configured)
        candidates.append(raw)
        if not raw.is_absolute():
            candidates.append(data / raw)
            candidates.append(ROOT / raw)
            candidates.append(legacy / raw)
    candidates.append(data / "credentials.json")
    candidates.append(ROOT / "credentials.json")
    candidates.append(legacy / "credentials.json")
    try:
        candidates.extend(sorted(data.glob("client_secret*.json")))
        candidates.extend(sorted(data.glob("*credentials*.json")))
        candidates.extend(sorted(ROOT.glob("client_secret*.json")))
        candidates.extend(sorted(ROOT.glob("*credentials*.json")))
        if legacy != data and legacy.exists():
            candidates.extend(sorted(legacy.glob("client_secret*.json")))
            candidates.extend(sorted(legacy.glob("*credentials*.json")))
    except Exception:
        pass

    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()).lower()
        except Exception:
            key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path.resolve()
        except Exception:
            continue
    return None


_ID_FROM_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def parse_spreadsheet_id(url_or_id: str) -> str:
    text = re.sub(r"\s+", "", url_or_id or "")
    match = _ID_FROM_URL.search(text)
    if match:
        return match.group(1)
    if "/d/" in text:
        raise ValueError(f"Не удалось вытащить ID из ссылки: {text}")
    return text


KIND_RECORDS = "records"
KIND_INFO = "info"
KIND_LABELS = {
    KIND_RECORDS: "Записи",
    KIND_INFO: "Общая информация",
}
_INFO_KIND_ALIASES = {
    "info",
    "инфо",
    "информация",
    "общая информация",
    "справка",
    "прайс",
}
_INFO_SHEET_NAMES = {
    "информация",
    "инфо",
    "общее",
    "общая информация",
    "справка",
    "прайс",
    "цены",
    "условия",
    "контакты",
    "о нас",
    "примечание",
    "объявления",
    "услуги",
    "услуги врача",
    "notes",
    "info",
    "general",
    "about",
    "notice",
}


def normalize_kind(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in _INFO_KIND_ALIASES or raw == KIND_INFO:
        return KIND_INFO
    return KIND_RECORDS


def is_info_title(text: str) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    if raw in _INFO_SHEET_NAMES:
        return True
    # Только явные префиксы — иначе «услуг» внутри длинных названий даёт ложные совпадения.
    return any(raw.startswith(marker) for marker in ("информ", "прайс", "справк", "объявлен", "услуг"))


def is_info_ref(ref: "SheetRef") -> bool:
    if normalize_kind(ref.kind) == KIND_INFO:
        return True
    return is_info_title(ref.sheet)


def companion_info_titles(available: list[str], calendar_title: str = "") -> list[str]:
    """Вкладки-справки той же книги (например «УСЛУГИ врача»), кроме календаря."""
    skip = (calendar_title or "").strip().lower()
    out: list[str] = []
    for title in available:
        name = (title or "").strip()
        if not name or name.lower() == skip:
            continue
        if is_info_title(name):
            out.append(name)
    return out

@dataclass
class SheetRef:
    name: str
    spreadsheet_id: str
    sheet: str = "Лист1"
    map: dict[str, str] = field(default_factory=dict)
    service: str = ""
    city: str = ""
    address: str = ""
    kind: str = KIND_RECORDS

    def normalized_id(self) -> str:
        return parse_spreadsheet_id(self.spreadsheet_id)

    def is_placeholder(self) -> bool:
        text = (self.spreadsheet_id or "").strip()
        return not text or text.upper().startswith("PASTE_")

    def resolved_city(self) -> str:
        explicit = (self.city or "").strip()
        if explicit:
            return explicit
        return extract_city(self.address)

    def label(self) -> str:
        parts = [self.name]
        if self.service:
            parts.append(self.service)
        city = self.resolved_city()
        if city:
            parts.append(city)
        if self.address:
            parts.append(self.address)
        return " · ".join(parts)

    def dest_key(self) -> str:
        return (
            f"{self.name}|{self.spreadsheet_id}|{self.sheet}|"
            f"{self.service}|{self.city}|{self.address}"
        )


def extract_city(address: str) -> str:
    """«г. Минск, пр-т …» → Минск; «Минск, ул. …» → Минск; без города → ''."""
    raw = (address or "").strip()
    if not raw:
        return ""
    head = raw.split(",")[0].strip()
    prefixed = re.match(r"^(?:г\.|город)\s*(.+)$", head, flags=re.IGNORECASE)
    if prefixed:
        return prefixed.group(1).strip(" .")
    # «Минск, пр-т Партизанский, 56» — город без префикса.
    if "," in raw and not re.search(r"\d", head) and 1 < len(head) < 40:
        low = head.lower()
        if not low.startswith(("ул", "пр", "пер", "бул", "наб", "пл", "мкр")):
            return head
    return ""


_ALL_SHEETS = {"", "*", "все", "all", "все листы"}

_MONTH_MARKERS = (
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
)


def requested_sheet_titles(text: str) -> list[str] | None:
    """None — в конфиге стоит «все» (раньше означало все вкладки). Иначе список имён."""
    raw = (text or "").strip()
    if raw.lower() in _ALL_SHEETS:
        return None
    parts = [part.strip() for part in re.split(r"\s*[,;/|]\s*", raw) if part.strip()]
    return parts or None


def prefer_sheet_title(available: list[str], wanted: str = "") -> str:
    """Выбрать один лист: точное имя, иначе текущий месяц, иначе первый."""
    titles = [title for title in available if (title or "").strip()]
    if not titles:
        return (wanted or "").strip()
    raw = (wanted or "").strip()
    if raw and raw.lower() not in _ALL_SHEETS:
        for title in titles:
            if title.lower() == raw.lower():
                return title
        for title in titles:
            if raw.lower() in title.lower() or title.lower() in raw.lower():
                return title
    # Текущий месяц по-русски, например «август 2026»
    from datetime import datetime

    months = (
        "январ",
        "феврал",
        "март",
        "апрел",
        "ма",
        "июн",
        "июл",
        "август",
        "сентябр",
        "октябр",
        "ноябр",
        "декабр",
    )
    now = datetime.now()
    month_key = months[now.month - 1]
    year = str(now.year)
    for title in titles:
        low = title.lower()
        if month_key in low and year in low:
            return title
    for title in titles:
        if month_key in title.lower():
            return title
    # Не брать вкладки-справки, если есть календарные
    calendarish = [
        title
        for title in titles
        if any(marker in title.lower() for marker in _MONTH_MARKERS)
        or any(ch.isdigit() for ch in title)
    ]
    if calendarish:
        return calendarish[0]
    return titles[0]


def expand_ref_locally(ref: SheetRef) -> list[SheetRef]:
    """Всегда один лист: не раздуваем одну таблицу в несколько источников."""
    titles = requested_sheet_titles(ref.sheet)
    if titles:
        return [replace(ref, sheet=titles[0])]
    return [ref]


Source = SheetRef


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""

    @staticmethod
    def _is_real(value: str) -> bool:
        text = (value or "").strip()
        if not text:
            return False
        upper = text.upper()
        if upper.startswith("PASTE_") or upper.startswith("YOUR_"):
            return False
        if "TOKEN_HERE" in upper or "CHAT_ID_HERE" in upper:
            return False
        return True

    def is_configured(self) -> bool:
        return bool(
            self.enabled
            and self._is_real(self.bot_token)
            and self._is_real(self.chat_id)
        )


@dataclass
class UiConfig:
    """Защита окна «Таблицы» на рабочих ПК."""

    show_tables_button: bool = True
    tables_password: str = ""


@dataclass
class AppConfig:
    credentials: Path
    sources: list[SheetRef]
    destinations: list[SheetRef]
    registry_spreadsheet_id: str = ""
    registry_sheet: str = "SheetsHub"
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ui: UiConfig = field(default_factory=UiConfig)


def usable_refs(refs: list[SheetRef]) -> list[SheetRef]:
    return [item for item in refs if not item.is_placeholder()]


def _ref_identity(ref: SheetRef) -> tuple[str, str, str, str, str]:
    """Уникальность строки реестра: одна ссылка может быть у нескольких услуг/адресов."""
    try:
        sid = ref.normalized_id()
    except ValueError:
        sid = re.sub(r"\s+", "", ref.spreadsheet_id or "")
    return (
        sid,
        (ref.sheet or "").strip().lower(),
        (ref.service or "").strip().lower(),
        (ref.address or "").strip().lower(),
        (ref.name or "").strip().lower(),
    )


def merge_tables(sources: list[SheetRef], destinations: list[SheetRef]) -> list[SheetRef]:
    """Один список таблиц. Одинаковая ссылка + разная услуга/адрес — это разные строки."""
    merged: dict[tuple[str, str, str, str, str], SheetRef] = {}
    for ref in [*usable_refs(sources), *usable_refs(destinations)]:
        key = _ref_identity(ref)
        existing = merged.get(key)
        if existing is None or (
            (ref.service or ref.city or ref.address)
            and not (existing.service or existing.city or existing.address)
        ):
            merged[key] = ref
    return list(merged.values())


def _load_refs(items: Any) -> list[SheetRef]:
    refs: list[SheetRef] = []
    for item in items or []:
        address = str(item.get("address") or item.get("адрес") or "")
        city = str(item.get("city") or item.get("город") or "").strip()
        if not city:
            city = extract_city(address)
        refs.append(
            SheetRef(
                name=str(item.get("name") or "Без имени"),
                spreadsheet_id=str(item.get("spreadsheet_id") or ""),
                sheet=str(item.get("sheet") or ""),
                map={str(k): str(v) for k, v in (item.get("map") or {}).items()},
                service=str(item.get("service") or item.get("услуга") or ""),
                city=city,
                address=address,
                kind=normalize_kind(str(item.get("kind") or item.get("тип") or KIND_RECORDS)),
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
    if ref.city:
        payload["city"] = ref.city
    if ref.address:
        payload["address"] = ref.address
    if normalize_kind(ref.kind) == KIND_INFO:
        payload["kind"] = KIND_INFO
    return payload


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or resolve_config_path()
    text: str | None = None
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
    else:
        legacy_cfg = legacy_user_data_dir() / "config.yaml"
        if legacy_cfg.exists():
            text = legacy_cfg.read_text(encoding="utf-8")
        elif EXAMPLE_CONFIG_PATH.exists():
            example = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(example, encoding="utf-8")
                text = config_path.read_text(encoding="utf-8")
            except Exception:
                # Program Files без прав: читаем пример из памяти, запись — после UAC.
                text = example
        else:
            raise FileNotFoundError("Нет config.yaml и config.example.yaml")

    raw: dict[str, Any] = yaml.safe_load(text) or {}
    configured = Path(raw.get("credentials") or "credentials.json")
    found = find_credentials_file(configured)
    data_dir = writable_data_dir()
    creds = found or (
        configured if configured.is_absolute() else (data_dir / configured)
    )

    destinations = _load_refs(raw.get("destinations") or raw.get("targets"))
    sources = _load_refs(raw.get("sources"))
    tables = merge_tables(sources, destinations)
    if not tables:
        tables = sources or destinations
    registry = raw.get("registry") or {}
    if isinstance(registry, str):
        registry_id = registry
        registry_sheet = "SheetsHub"
    else:
        registry_id = str(
            (registry or {}).get("spreadsheet_id")
            or raw.get("registry_spreadsheet_id")
            or ""
        ).strip()
        registry_sheet = str((registry or {}).get("sheet") or "SheetsHub").strip() or "SheetsHub"
    tg_raw = raw.get("telegram") or {}
    telegram = TelegramConfig(
        enabled=bool(tg_raw.get("enabled")),
        bot_token=str(tg_raw.get("bot_token") or "").strip(),
        chat_id=str(tg_raw.get("chat_id") or "").strip(),
    )
    ui_raw = raw.get("ui") or {}
    # Поддержка плоских ключей из старых конфигов / ручного редактирования.
    show_tables = ui_raw.get("show_tables_button")
    if show_tables is None:
        show_tables = raw.get("show_tables_button")
    if show_tables is None:
        show_tables = True
    tables_password = str(
        ui_raw.get("tables_password")
        or raw.get("tables_password")
        or ""
    ).strip()
    ui = UiConfig(
        show_tables_button=bool(show_tables),
        tables_password=tables_password,
    )
    return AppConfig(
        credentials=creds,
        sources=tables,
        destinations=list(tables),
        registry_spreadsheet_id=registry_id,
        registry_sheet=registry_sheet,
        telegram=telegram,
        ui=ui,
    )


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config_path = path or resolve_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = writable_data_dir()
    try:
        rel_creds = config.credentials.relative_to(data_dir)
        creds_value = str(rel_creds).replace("\\", "/")
    except ValueError:
        try:
            rel_creds = config.credentials.relative_to(ROOT)
            creds_value = str(rel_creds).replace("\\", "/")
        except ValueError:
            creds_value = str(config.credentials)

    tables = merge_tables(config.sources, config.destinations) or config.sources
    dumped = [_dump_ref(item) for item in tables]
    payload: dict[str, Any] = {
        "credentials": creds_value,
        "sources": dumped,
        "destinations": dumped,
    }
    registry_id = (config.registry_spreadsheet_id or "").strip()
    if registry_id:
        payload["registry"] = {
            "spreadsheet_id": registry_id,
            "sheet": (config.registry_sheet or "SheetsHub").strip() or "SheetsHub",
        }
    payload["telegram"] = {
        "enabled": bool(config.telegram.enabled),
        "bot_token": config.telegram.bot_token,
        "chat_id": config.telegram.chat_id,
    }
    payload["ui"] = {
        "show_tables_button": bool(config.ui.show_tables_button),
        "tables_password": config.ui.tables_password or "",
    }
    tmp = config_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(config_path)


def credentials_email(path: Path) -> str:
    from sheets_hub.auth import credentials_email as _auth_email

    return _auth_email(path)


def install_credentials(source: Path, dest: Path | None = None) -> Path:
    from sheets_hub.auth import install_google_credentials

    return install_google_credentials(source, dest)
