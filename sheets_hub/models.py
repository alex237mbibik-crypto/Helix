from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Record:
    source_name: str
    spreadsheet_id: str
    sheet: str
    row: int
    values: dict[str, str]
    sheet_headers: list[str] = field(default_factory=list)
    col_index: dict[str, int] = field(default_factory=dict)
    map: dict[str, str] = field(default_factory=dict)
    origin_values: dict[str, str] = field(default_factory=dict)
    split_from: bool = False
    kind: str = "records"

    def display_values(self) -> dict[str, str]:
        return {"Источник": self.source_name, **self.values}

    def header_for(self, field: str) -> str:
        if field in self.col_index:
            return field
        mapped = self.map.get(field)
        if mapped and mapped in self.col_index:
            return mapped
        raise KeyError(field)
