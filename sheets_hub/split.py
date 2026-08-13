from __future__ import annotations

import re
from dataclasses import replace
from itertools import product

from sheets_hub.models import Record

SERVICE_ALIASES = ("тип услуги", "услуга", "услуги", "вид услуги", "service")
ADDRESS_ALIASES = ("адрес", "address", "филиал", "точка", "локация")

_SERVICE_SPLIT = re.compile(r"\s*[,;/|]\s*|\n+")
_ADDRESS_SPLIT = re.compile(r"\s*[;|]\s*|\n+| / ")


def _norm(name: str) -> str:
    return name.strip().lower()


def is_service_col(name: str) -> bool:
    key = _norm(name)
    return key in SERVICE_ALIASES or "услуг" in key or key == "service"


def is_address_col(name: str) -> bool:
    key = _norm(name)
    return key in ADDRESS_ALIASES or "адрес" in key or key == "address"


def find_key(values: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    exact = {_norm(key): key for key in values}
    for alias in aliases:
        if alias in exact:
            return exact[alias]
    for key in values:
        nk = _norm(key)
        for alias in aliases:
            if alias in nk:
                return key
    return None


def service_key(values: dict[str, str]) -> str | None:
    return find_key(values, SERVICE_ALIASES)


def address_key(values: dict[str, str]) -> str | None:
    return find_key(values, ADDRESS_ALIASES)


def service_value(values: dict[str, str]) -> str:
    key = service_key(values)
    return values.get(key, "").strip() if key else ""


def address_value(values: dict[str, str]) -> str:
    key = address_key(values)
    return values.get(key, "").strip() if key else ""


def split_services(text: str) -> list[str]:
    if not (text or "").strip():
        return [""]
    parts = [part.strip() for part in _SERVICE_SPLIT.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


def split_addresses(text: str) -> list[str]:
    if not (text or "").strip():
        return [""]
    parts = [part.strip() for part in _ADDRESS_SPLIT.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


def explode_values(values: dict[str, str]) -> list[dict[str, str]]:
    s_key = service_key(values)
    a_key = address_key(values)
    services = split_services(values.get(s_key, "")) if s_key else [""]
    addresses = split_addresses(values.get(a_key, "")) if a_key else [""]
    if len(services) <= 1 and len(addresses) <= 1:
        return [dict(values)]

    rows: list[dict[str, str]] = []
    for service, address in product(services, addresses):
        row = dict(values)
        if s_key:
            row[s_key] = service
        if a_key:
            row[a_key] = address
        rows.append(row)
    return rows


def explode_record(record: Record) -> list[Record]:
    origin = dict(record.origin_values or record.values)
    rows = explode_values(origin)
    if len(rows) == 1 and rows[0] == origin:
        if not record.origin_values:
            record.origin_values = origin
        return [record]

    exploded: list[Record] = []
    for row in rows:
        exploded.append(
            replace(
                record,
                values=row,
                origin_values=origin,
                split_from=True,
            )
        )
    return exploded


def explode_records(records: list[Record]) -> list[Record]:
    out: list[Record] = []
    for record in records:
        out.extend(explode_record(record))
    return out


def write_back_value(record: Record, field: str, new_value: str) -> str:
    original = record.origin_values.get(field, record.values.get(field, ""))
    if not record.split_from:
        return new_value
    token = record.values.get(field, "")
    if is_service_col(field):
        parts = split_services(original)
        joiner = ", "
    elif is_address_col(field):
        parts = split_addresses(original)
        joiner = "; "
    else:
        return new_value
    replaced = False
    updated: list[str] = []
    for part in parts:
        if not replaced and part == token:
            updated.append(new_value)
            replaced = True
        else:
            updated.append(part)
    if not replaced:
        updated.append(new_value)
    return joiner.join(item for item in updated if item)


def match_destination(service: str, address: str, destinations: list, fallback=None):
    service_n = _norm(service)
    address_n = _norm(address)
    scored = []
    for dest in destinations:
        dest_service = _norm(getattr(dest, "service", "") or "")
        dest_address = _norm(getattr(dest, "address", "") or "")
        if dest_service and dest_service != service_n:
            continue
        if dest_address and dest_address != address_n:
            continue
        score = (2 if dest_service else 0) + (2 if dest_address else 0)
        scored.append((score, dest))
    if not scored:
        return fallback
    scored.sort(key=lambda item: -item[0])
    return scored[0][1]
