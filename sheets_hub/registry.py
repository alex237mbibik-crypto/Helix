from __future__ import annotations

import json
from typing import Any

from sheets_hub.config import (
    KIND_INFO,
    KIND_RECORDS,
    SheetRef,
    extract_city,
    normalize_kind,
    usable_refs,
)

REGISTRY_HEADERS = [
    "name",
    "spreadsheet_id",
    "sheet",
    "service",
    "city",
    "address",
    "kind",
    "map_json",
]
DEFAULT_REGISTRY_SHEET = "SheetsHub"


def tables_signature(refs: list[SheetRef]) -> tuple:
    rows: list[tuple] = []
    for ref in usable_refs(refs):
        try:
            sid = ref.normalized_id()
        except ValueError:
            sid = (ref.spreadsheet_id or "").strip()
        rows.append(
            (
                (ref.name or "").strip(),
                sid,
                (ref.sheet or "").strip().lower(),
                (ref.service or "").strip(),
                (ref.city or "").strip() or ref.resolved_city(),
                (ref.address or "").strip(),
                normalize_kind(ref.kind),
                tuple(sorted((str(k), str(v)) for k, v in (ref.map or {}).items())),
            )
        )
    return tuple(sorted(rows))


def refs_from_registry_rows(rows: list[list[Any]]) -> list[SheetRef]:
    if not rows:
        return []
    header = [str(cell or "").strip().lower() for cell in rows[0]]
    index = {name: idx for idx, name in enumerate(header)}
    # Поддержка без заголовка: первая строка уже данные.
    start = 1
    if "spreadsheet_id" not in index and "name" not in index:
        index = {name: idx for idx, name in enumerate(REGISTRY_HEADERS)}
        start = 0

    def cell(row: list[Any], key: str) -> str:
        idx = index.get(key)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx] or "").strip()

    out: list[SheetRef] = []
    for row in rows[start:]:
        if not row or not any(str(c or "").strip() for c in row):
            continue
        sid = cell(row, "spreadsheet_id") or cell(row, "ссылка") or cell(row, "url")
        if not sid:
            continue
        map_raw = cell(row, "map_json") or cell(row, "map")
        mapping: dict[str, str] = {}
        if map_raw:
            try:
                parsed = json.loads(map_raw)
                if isinstance(parsed, dict):
                    mapping = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                mapping = {}
        address = cell(row, "address") or cell(row, "адрес")
        city = cell(row, "city") or cell(row, "город") or extract_city(address)
        out.append(
            SheetRef(
                name=cell(row, "name") or cell(row, "название") or "Таблица",
                spreadsheet_id=sid,
                sheet=cell(row, "sheet") or cell(row, "лист") or "все",
                service=cell(row, "service") or cell(row, "услуга"),
                city=city,
                address=address,
                kind=normalize_kind(cell(row, "kind") or cell(row, "тип") or KIND_RECORDS),
                map=mapping,
            )
        )
    return usable_refs(out)


def refs_to_registry_rows(refs: list[SheetRef]) -> list[list[str]]:
    rows: list[list[str]] = [list(REGISTRY_HEADERS)]
    for ref in usable_refs(refs):
        map_json = json.dumps(ref.map, ensure_ascii=False) if ref.map else ""
        kind = normalize_kind(ref.kind)
        try:
            sid = ref.normalized_id()
        except ValueError:
            sid = (ref.spreadsheet_id or "").strip()
        rows.append(
            [
                ref.name or "",
                sid,
                ref.sheet or "все",
                ref.service or "",
                ref.city or ref.resolved_city() or "",
                ref.address or "",
                kind if kind == KIND_INFO else "",
                map_json,
            ]
        )
    return rows
