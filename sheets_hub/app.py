from __future__ import annotations

import csv
import re
import sys
import threading
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import customtkinter as ctk
import tkinter as tk

from sheets_hub.calendar_sheet import info_tone
from sheets_hub.client import SheetsClient, SheetsError, values_for_destination
from sheets_hub.config import (
    KIND_INFO,
    KIND_LABELS,
    KIND_RECORDS,
    AppConfig,
    SheetRef,
    credentials_email,
    expand_ref_locally,
    install_credentials,
    load_config,
    requested_sheet_titles,
    save_config,
)
from sheets_hub.models import Record
from sheets_hub.ssl_setup import configure_tls
from sheets_hub.split import (
    address_value,
    explode_records,
    explode_values,
    is_address_col,
    is_service_col,
    match_destination,
    service_value,
    write_back_value,
)

HIDDEN = {"_sid", "_sheet", "_row", "_tone"}
STATUS_OPTIONS = ("Ожидание", "Подтверждено", "Отменено")

GREEN = "#188038"
GREEN_HOVER = "#137333"
GREEN_SOFT = "#e6f4ea"
SLOT_GREEN = "#7cb342"
SLOT_BOOKED = "#558b2f"
SLOT_BLOCKED = "#ffffff"
SLOT_TIME = "#f1f3f4"
INFO_TONES = {
    "warn": ("#f8d7c4", "#c5221f"),
    "ok": ("#7cb342", "#202124"),
    "note": ("#fff59d", "#202124"),
    "info": ("#b3e5fc", "#202124"),
}
BG = "#f8f9fa"
CARD = "#ffffff"
BORDER = "#5f6368"
LINE = "#e8eaed"
TEXT = "#202124"
MUTED = "#5f6368"
HINT = "#80868b"
DANGER = "#d93025"
DANGER_HOVER = "#b3261e"
ZEBRA = "#fafafa"
HOVER = "#f1f3f4"
SELECT = "#ceead6"


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            from ctypes import windll

            windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _ui_font(size: int = 13, bold: bool = False):
    family = "Segoe UI" if sys.platform == "win32" else "Helvetica Neue"
    return (family, size, "bold") if bold else (family, size)


def _norm(name: str) -> str:
    return name.strip().lower()


def _is_date_col(name: str) -> bool:
    key = _norm(name)
    return key in {"дата", "date", "день"} or "дат" in key


def _is_status_col(name: str) -> bool:
    key = _norm(name)
    return key in {"статус", "status", "состояние"} or "статус" in key


def _time_sort_key(text: str) -> tuple[int, int]:
    match = re.search(r"(\d{1,2})[:.\-](\d{2})", text or "")
    if not match:
        return (99, 99)
    return int(match.group(1)), int(match.group(2))


def _column_prompt(header: str) -> tuple[str, str]:
    key = _norm(header)
    if _is_date_col(header):
        return "2026-08-13", "дата в формате ГГГГ-ММ-ДД"
    if _is_status_col(header):
        return "Ожидание", "статус: Ожидание, Подтверждено или Отменено"
    if is_service_col(header):
        return "Стрижка", "тип услуги, несколько через запятую"
    if is_address_col(header):
        return "ул. Ленина, д. 10", "адрес филиала, несколько через ;"
    if "телефон" in key or "phone" in key or "тел" in key:
        return "+7 999 123-45-67", "телефон клиента"
    if "клиент" in key or "имя" in key or "фио" in key or key in {"name", "client"}:
        return "Иванова А.", "имя клиента"
    if "время" in key or key == "time":
        return "10:00", "время визита"
    if "сумм" in key or "amount" in key or "цена" in key:
        return "0", "число без знака валюты"
    if "коммент" in key or "примечан" in key or "note" in key:
        return "комментарий", "любой текст"
    return f"Введите «{header}»", f"значение колонки «{header}»"


def _labeled_field(parent, title: str, hint: str = "") -> ctk.CTkFrame:
    box = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkLabel(
        box,
        text=title,
        font=ctk.CTkFont(size=11, weight="bold"),
        text_color=TEXT,
        anchor="w",
    ).pack(fill="x")
    if hint:
        ctk.CTkLabel(
            box,
            text=hint,
            font=ctk.CTkFont(size=10),
            text_color=HINT,
            anchor="w",
            justify="left",
        ).pack(fill="x")
    return box


def _input_box(parent, title: str, hint: str) -> ctk.CTkFrame:
    box = ctk.CTkFrame(
        parent,
        fg_color=CARD,
        corner_radius=8,
        border_width=2,
        border_color=BORDER,
    )
    ctk.CTkLabel(
        box,
        text=title,
        font=ctk.CTkFont(size=12, weight="bold"),
        text_color=TEXT,
        anchor="w",
    ).pack(fill="x", padx=10, pady=(8, 0))
    ctk.CTkLabel(
        box,
        text=hint,
        font=ctk.CTkFont(size=11),
        text_color=HINT,
        anchor="w",
        wraplength=220,
        justify="left",
    ).pack(fill="x", padx=10)
    return box


def _styled_entry(parent, placeholder: str, **kwargs) -> ctk.CTkEntry:
    return ctk.CTkEntry(
        parent,
        height=36,
        corner_radius=6,
        border_width=2,
        border_color=BORDER,
        fg_color=CARD,
        text_color=TEXT,
        placeholder_text=placeholder,
        placeholder_text_color="#9aa0a6",
        **kwargs,
    )


def _status_tag(value: str) -> str | None:
    key = _norm(value)
    if any(part in key for part in ("подтверж", "оплач", "готов", "да", "done", "ok")):
        return "status-ok"
    if any(part in key for part in ("ожид", "нов", "wait", "new")):
        return "status-wait"
    if any(part in key for part in ("отмен", "нет", "cancel", "fail")):
        return "status-no"
    return None


class SheetsHubApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sheets Hub")
        self.configure(fg_color=BG)
        self.minsize(960, 580)
        self._fit_to_screen()

        self.config_data: AppConfig = load_config()
        self.client: SheetsClient | None = None
        self.records: list[Record] = []
        self.info_records: list[Record] = []
        self._visible: list[Record] = []
        self._jobs = 0
        self._last_tree_width = 0
        self._sort_desc = False
        self._add_fields: dict[str, ctk.CTkEntry | ctk.CTkComboBox] = {}
        self._dest_headers: dict[str, list[str]] = {}
        self._dest_by_label: dict[str, SheetRef] = {}
        self._picked: Record | None = None

        self._build()
        self.after(200, self._try_connect)

    def _fit_to_screen(self) -> None:
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = max(960, screen_w - 48)
        height = max(580, screen_h - 96)
        x = max(0, (screen_w - width) // 2)
        y = max(24, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        container = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, border_width=0)
        container.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(4, weight=1)

        self._build_header(container)
        self._build_add_form(container)
        self._build_filter(container)
        self._build_status(container)
        self._build_table(container)
        self._build_info_panel(container)
        self._build_footer(container)
        self._refresh_dest_menu()

    def _outline_button(self, parent, text: str, command, width: int = 140) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=36,
            corner_radius=8,
            fg_color=CARD,
            hover_color=HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(size=13),
        )

    def _primary_button(self, parent, text: str, command, width: int = 140) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=36,
            corner_radius=8,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
        )

    def _build_header(self, parent: ctk.CTkFrame) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 8))
        header.grid_columnconfigure(1, weight=1)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_box,
            text="Sheets Hub",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")

        self.count_label = ctk.CTkLabel(
            title_box,
            text="0",
            fg_color=LINE,
            text_color=MUTED,
            corner_radius=12,
            width=40,
            height=26,
            font=ctk.CTkFont(size=13),
        )
        self.count_label.pack(side="left", padx=(10, 0))

        toolbar = ctk.CTkFrame(header, fg_color="transparent")
        toolbar.grid(row=0, column=2, sticky="e")

        self.refresh_btn = self._primary_button(toolbar, "Обновить всё", self.reload_all, 130)
        self.refresh_btn.pack(side="left", padx=(0, 8))
        self._outline_button(toolbar, "Таблицы", self._open_tables_dialog, 110).pack(side="left", padx=(0, 8))
        self._outline_button(toolbar, "Экспорт CSV", self._export_csv, 120).pack(side="left")

    def _build_add_form(self, parent: ctk.CTkFrame) -> None:
        self.add_form = ctk.CTkFrame(
            parent,
            fg_color=BG,
            corner_radius=10,
            border_width=1,
            border_color=LINE,
        )
        self.add_form.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 8))
        self.add_form.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self.add_form, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        top.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            top,
            text="Новая строка в",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")
        dest_box = ctk.CTkFrame(top, fg_color=CARD, corner_radius=8, border_width=2, border_color=BORDER)
        dest_box.pack(side="left", padx=(8, 0), fill="x", expand=True)
        ctk.CTkLabel(
            dest_box,
            text="куда записать: выберите таблицу-назначение",
            font=ctk.CTkFont(size=11),
            text_color=HINT,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(6, 0))

        self.dest_var = tk.StringVar(value="")
        self.dest_menu = ctk.CTkOptionMenu(
            dest_box,
            variable=self.dest_var,
            values=["—"],
            width=280,
            height=32,
            fg_color=CARD,
            button_color=GREEN,
            button_hover_color=GREEN_HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_text_color=TEXT,
            command=lambda _value: self._load_dest_headers(),
        )
        self.dest_menu.pack(padx=8, pady=(4, 8), anchor="w")

        self.add_fields_wrap = ctk.CTkFrame(self.add_form, fg_color="transparent")
        self.add_fields_wrap.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 12))

        self._rebuild_add_fields([])

    def _rebuild_add_fields(self, headers: list[str]) -> None:
        for child in self.add_fields_wrap.winfo_children():
            child.destroy()
        self._add_fields = {}

        if not headers:
            dest = self._selected_destination()
            text = (
                "Это сетка записи, как в Google Таблице. Нажмите ячейку в календаре, чтобы вписать имя."
                if dest
                else "Укажите таблицу-назначение в «Таблицы» — сюда будут попадать новые строки."
            )
            ctk.CTkLabel(
                self.add_fields_wrap,
                text=text,
                text_color=MUTED,
                anchor="w",
                wraplength=900,
                justify="left",
            ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return

        cols = 4
        for i, header in enumerate(headers):
            row, col = divmod(i, cols)
            placeholder, hint = _column_prompt(header)
            field = _input_box(self.add_fields_wrap, header, hint)
            field.grid(row=row, column=col, sticky="ew", padx=6, pady=4)
            self.add_fields_wrap.grid_columnconfigure(col, weight=1)
            if _is_status_col(header):
                widget = ctk.CTkComboBox(
                    field,
                    values=list(STATUS_OPTIONS),
                    height=36,
                    corner_radius=6,
                    border_width=2,
                    border_color=BORDER,
                    fg_color=CARD,
                    button_color=GREEN,
                    button_hover_color=GREEN_HOVER,
                )
                widget.set(STATUS_OPTIONS[0])
            else:
                widget = _styled_entry(field, placeholder)
                if _is_date_col(header):
                    widget.insert(0, date.today().isoformat())
            widget.pack(fill="x", padx=10, pady=(4, 10))
            self._add_fields[header] = widget

        last_row = (len(headers) - 1) // cols
        btn_wrap = ctk.CTkFrame(self.add_fields_wrap, fg_color="transparent")
        btn_wrap.grid(row=last_row + 1, column=0, sticky="w", padx=6, pady=(8, 0))
        self._primary_button(btn_wrap, "Добавить", self._add_to_destination, 140).pack(side="left")

    def _build_filter(self, parent: ctk.CTkFrame) -> None:
        filter_area = ctk.CTkFrame(parent, fg_color="transparent")
        filter_area.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 4))
        filter_area.grid_columnconfigure(1, weight=1)

        search_box = _input_box(filter_area, "Поиск", "имя, телефон, услуга или любой текст из таблицы")
        search_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=6, padx=(0, 12))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_views())
        _styled_entry(
            search_box, "начните вводить текст для фильтра", textvariable=self.search_var
        ).pack(fill="x", padx=10, pady=(4, 10))

        date_box = _input_box(filter_area, "Дата", "оставьте пустым или введите ГГГГ-ММ-ДД")
        date_box.grid(row=0, column=2, columnspan=2, sticky="ew", pady=6, padx=(0, 8))
        self.date_var = tk.StringVar()
        self.date_var.trace_add("write", lambda *_: self._render_table())
        _styled_entry(date_box, "2026-08-13", width=160, textvariable=self.date_var).pack(
            fill="x", padx=10, pady=(4, 10)
        )

        service_box = _input_box(filter_area, "Услуга", "показывать пункты только этой услуги")
        service_box.grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        self.service_filter = tk.StringVar(value="Все")
        self.service_filter.trace_add("write", lambda *_: self._render_views())
        self.service_menu = ctk.CTkOptionMenu(
            service_box,
            variable=self.service_filter,
            values=["Все"],
            width=180,
            height=32,
            fg_color=CARD,
            button_color=GREEN,
            button_hover_color=GREEN_HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_text_color=TEXT,
        )
        self.service_menu.pack(padx=10, pady=(4, 10), anchor="w")

        address_box = _input_box(filter_area, "Адрес", "показывать пункты только этого адреса")
        address_box.grid(row=1, column=2, columnspan=2, sticky="w", pady=6, padx=(0, 8))
        self.address_filter = tk.StringVar(value="Все")
        self.address_filter.trace_add("write", lambda *_: self._render_views())
        self.address_menu = ctk.CTkOptionMenu(
            address_box,
            variable=self.address_filter,
            values=["Все"],
            width=180,
            height=32,
            fg_color=CARD,
            button_color=GREEN,
            button_hover_color=GREEN_HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_text_color=TEXT,
        )
        self.address_menu.pack(padx=10, pady=(4, 10), anchor="w")

        ctk.CTkButton(
            filter_area,
            text="✕ Сбросить",
            command=self._clear_filters,
            width=110,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            hover_color=HOVER,
            text_color=GREEN,
            font=ctk.CTkFont(size=13),
        ).grid(row=1, column=4, pady=6, padx=(12, 12))

        self._outline_button(filter_area, "Внести в назначение", self._copy_to_destination, 170).grid(
            row=1, column=5, pady=6, padx=(0, 8)
        )
        ctk.CTkButton(
            filter_area,
            text="Удалить строку",
            command=self._delete_row,
            width=130,
            height=36,
            corner_radius=8,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=1, column=6, pady=6)

    def _build_status(self, parent: ctk.CTkFrame) -> None:
        self.status = ctk.CTkLabel(
            parent,
            text="Загрузка…",
            anchor="w",
            text_color=MUTED,
            font=ctk.CTkFont(size=13),
        )
        self.status.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 6))

    def _build_table(self, parent: ctk.CTkFrame) -> None:
        table_wrap = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=10,
            border_width=1,
            border_color="#e0e0e0",
        )
        table_wrap.grid(row=4, column=0, sticky="nsew", padx=20, pady=(0, 8))
        table_wrap.grid_columnconfigure(0, weight=1)
        table_wrap.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            table_wrap,
            text="Календарь записи",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        inner = tk.Frame(table_wrap, bg=CARD, highlightthickness=0)
        inner.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(inner, show="headings", selectmode="browse")
        self.vsb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<Delete>", lambda *_: self._delete_row())
        self._style_treeview()

        self.cal_host = tk.Frame(inner, bg=CARD, highlightthickness=0)

    def _build_info_panel(self, parent: ctk.CTkFrame) -> None:
        self.info_wrap = ctk.CTkFrame(
            parent,
            fg_color=GREEN_SOFT,
            corner_radius=10,
            border_width=1,
            border_color="#ceead6",
        )
        self.info_wrap.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 8))
        self.info_wrap.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.info_wrap,
            text="Общая информация",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            self.info_wrap,
            text="листы с типом «Общая информация» или цветные блоки под сеткой записи в той же таблице",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 4))

        self.info_body = ctk.CTkFrame(self.info_wrap, fg_color="transparent")
        self.info_body.pack(fill="x", padx=8, pady=(0, 10))
        self.info_body.grid_columnconfigure(0, weight=1)
        self._info_empty = ctk.CTkLabel(
            self.info_body,
            text="Пока пусто. В «Таблицы» у источника выберите тип «Общая информация» "
            "или назовите вкладку «Информация».",
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self._info_empty.grid(row=0, column=0, sticky="w", padx=6, pady=6)

    def _build_footer(self, parent: ctk.CTkFrame) -> None:
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.grid(row=6, column=0, sticky="ew", padx=22, pady=(4, 14))
        footer.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            footer,
            text="Зелёная ячейка «запись» — свободно, имя — занято, «не записывать» — слот закрыт. Общая информация — цветные блоки под календарём.",
            text_color=MUTED,
            anchor="w",
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w")

        legend = ctk.CTkFrame(footer, fg_color="transparent")
        legend.grid(row=0, column=2, sticky="e")
        ctk.CTkLabel(legend, text="Подтверждено", text_color="#1e8e3e", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 12)
        )
        ctk.CTkLabel(legend, text="Ожидание", text_color="#e37400", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 12)
        )
        ctk.CTkLabel(legend, text="Отменено", text_color=DANGER, font=ctk.CTkFont(size=12)).pack(side="left")

    def _style_treeview(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        font = _ui_font(12)
        heading_font = _ui_font(12, bold=True)
        style.configure(
            "Treeview",
            background=CARD,
            foreground=TEXT,
            fieldbackground=CARD,
            rowheight=34,
            borderwidth=0,
            relief="flat",
            font=font,
        )
        style.configure(
            "Treeview.Heading",
            background=GREEN,
            foreground="#ffffff",
            relief="flat",
            borderwidth=0,
            padding=(10, 10),
            font=heading_font,
        )
        style.map(
            "Treeview",
            background=[("selected", SELECT)],
            foreground=[("selected", GREEN_HOVER)],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", GREEN_HOVER)],
            foreground=[("active", "#ffffff")],
        )
        self.tree.tag_configure("oddrow", background=CARD)
        self.tree.tag_configure("evenrow", background=ZEBRA)
        self.tree.tag_configure("status-ok", background="#e6f4ea")
        self.tree.tag_configure("status-wait", background="#fef7e0")
        self.tree.tag_configure("status-no", background="#fce8e6")

    def _clear_filters(self) -> None:
        self.search_var.set("")
        self.date_var.set("")
        self.service_filter.set("Все")
        self.address_filter.set("Все")

    def _on_tree_configure(self, event=None) -> None:
        if event is not None and event.widget is not self.tree:
            return
        self._fit_columns()

    def _fit_columns(self) -> None:
        columns = self.tree["columns"]
        if not columns:
            return
        width = self.tree.winfo_width()
        if width < 80:
            return
        if abs(width - self._last_tree_width) < 4:
            return
        self._last_tree_width = width
        n = len(columns)
        usable = max(80, width - 2)
        base = usable // n
        remainder = usable % n
        for i, col in enumerate(columns):
            self.tree.column(col, width=base + (1 if i < remainder else 0), stretch=True, minwidth=80)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _valid_sources(self) -> list[SheetRef]:
        return [item for item in self.config_data.sources if not item.is_placeholder()]

    def _valid_destinations(self) -> list[SheetRef]:
        return [item for item in self.config_data.destinations if not item.is_placeholder()]

    def _destination_candidates(self) -> list[SheetRef]:
        if self._dest_by_label:
            return list(self._dest_by_label.values())
        return self._valid_destinations()

    def _expand_destinations(self) -> list[SheetRef]:
        dests = self._valid_destinations()
        expanded: list[SheetRef] = []
        for dest in dests:
            titles = requested_sheet_titles(dest.sheet)
            if titles is None and self.client:
                try:
                    expanded.extend(self.client.expand_source(dest))
                    continue
                except Exception:
                    expanded.append(dest)
                    continue
            expanded.extend(expand_ref_locally(dest))
        return expanded

    def _selected_destination(self) -> SheetRef | None:
        label = self.dest_var.get().strip()
        if label in self._dest_by_label:
            return self._dest_by_label[label]
        return next((item for item in self._destination_candidates() if item.label() == label), None)

    def _destination_for(self, values: dict[str, str], fallback: SheetRef | None = None) -> SheetRef | None:
        dests = self._destination_candidates()
        tagged = [item for item in dests if item.service or item.address]
        chosen = fallback or self._selected_destination()
        if tagged:
            return match_destination(service_value(values), address_value(values), tagged, chosen)
        return chosen

    def _refresh_dest_menu(self) -> None:
        dests = self._expand_destinations()
        self._dest_by_label = {}
        labels: list[str] = []
        for dest in dests:
            label = dest.label()
            base = label
            n = 2
            while label in self._dest_by_label:
                label = f"{base} ({n})"
                n += 1
            self._dest_by_label[label] = dest
            labels.append(label)
        if not labels:
            self.dest_menu.configure(values=["—"])
            self.dest_var.set("—")
            self._rebuild_add_fields([])
            return
        current = self.dest_var.get()
        self.dest_menu.configure(values=labels)
        self.dest_var.set(current if current in labels else labels[0])
        self._load_dest_headers()

    def _try_connect(self, prompt_key: bool = True) -> None:
        if not self.config_data.credentials.exists():
            self.client = None
            self._set_status("Нет ключа аккаунта. Ссылка на таблицу сама по себе доступ не даёт.")
            if prompt_key:
                self.after(200, self._ask_for_credentials)
            return
        try:
            self.client = SheetsClient(self.config_data.credentials)
            self._set_status(f"Вход: {self.client.service_email}")
            self._refresh_dest_menu()
            if self._valid_sources():
                self.reload_all()
            else:
                self._set_status("Укажите таблицы-источники в «Таблицы» — откуда брать данные.")
        except SheetsError as exc:
            self._set_status(str(exc).split("\n")[0])
            messagebox.showwarning("Нет доступа к Google", str(exc))
        except Exception as exc:
            self._set_status(f"Ошибка входа: {exc}")
            messagebox.showerror("Ошибка", str(exc))

    def _ask_for_credentials(self) -> None:
        go = messagebox.askokcancel(
            "Нужен аккаунт Google",
            "Ссылка на таблицу уже сохранена, но программа ещё не знает, "
            "через какой аккаунт к ней ходить.\n\n"
            "Сейчас откроется выбор файла. Укажите JSON-ключ сервисного аккаунта "
            "из Google Cloud (не саму таблицу).",
        )
        if not go:
            return
        if self._pick_credentials_file():
            save_config(self.config_data)
            self._try_connect(prompt_key=False)

    def _pick_credentials_file(self, parent=None) -> bool:
        chosen = filedialog.askopenfilename(
            parent=parent or self,
            title="JSON-ключ сервисного аккаунта Google",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if not chosen:
            return False
        try:
            installed = install_credentials(Path(chosen))
        except Exception as exc:
            messagebox.showerror("Неверный ключ", str(exc), parent=parent or self)
            return False
        self.config_data.credentials = installed
        return True

    def _run_bg(self, work: Callable, done: Callable, *, alert: bool = True) -> None:
        self._jobs += 1
        self.refresh_btn.configure(state="disabled")

        def runner() -> None:
            error = None
            result = None
            try:
                result = work()
            except Exception as exc:
                error = exc
            self.after(0, lambda: self._finish_bg(done, result, error, alert=alert))

        threading.Thread(target=runner, daemon=True).start()

    def _finish_bg(self, done: Callable, result, error, alert: bool = True) -> None:
        self._jobs = max(0, self._jobs - 1)
        if self._jobs == 0:
            self.refresh_btn.configure(state="normal")
        if error:
            self._set_status(f"Ошибка: {error}")
            if alert:
                messagebox.showerror("Ошибка", str(error))
            return
        done(result)

    def reload_all(self) -> None:
        if not self.client:
            self._try_connect()
            if not self.client:
                return
        sources = self._valid_sources()
        if not sources:
            self.records = []
            self.info_records = []
            self._render_table()
            self._render_info()
            self._set_status("Нет таблиц-источников. Откройте «Таблицы» и вставьте ссылки.")
            return
        self._set_status("Читаю таблицы…")

        def work():
            return self.client.fetch_all(sources)

        def done(result):
            records, errors = result
            booking = [item for item in records if item.kind != KIND_INFO]
            self.info_records = [item for item in records if item.kind == KIND_INFO]
            raw_count = len(booking)
            self.records = explode_records(booking)
            self._refresh_split_filters()
            self._render_table()
            self._render_info()
            extra = f" · {len(errors)} ошибок" if errors else ""
            booked = sum(1 for item in self.records if item.layout == "calendar" and item.values.get("Статус") == "Занято")
            slots = sum(1 for item in self.records if item.layout == "calendar")
            info_note = f" · справка: {len(self.info_records)}" if self.info_records else ""
            if slots:
                self._set_status(f"Календарь: {slots} слотов, занято {booked}{info_note}{extra}")
            else:
                split_note = ""
                if len(self.records) > raw_count:
                    split_note = f" · разбито на {len(self.records)} пунктов"
                self._set_status(
                    f"Строк: {raw_count} из {len(sources)} источников{split_note}{info_note}{extra}"
                )
            if errors:
                messagebox.showwarning("Часть таблиц не загрузилась", "\n\n".join(errors[:8]))
                if any("подключиться к Google" in item for item in errors):
                    return
            self._load_dest_headers()

        self._run_bg(work, done)

    def _load_dest_headers(self) -> None:
        dest = self._selected_destination()
        if not dest or not self.client:
            if not dest:
                self._rebuild_add_fields([])
            return
        cached = self._dest_headers.get(dest.dest_key())
        if cached is not None:
            if list(self._add_fields.keys()) != cached:
                self._rebuild_add_fields(cached)
            return

        def work():
            return self.client.get_headers(dest)

        def done(headers):
            self._dest_headers[dest.dest_key()] = headers
            selected = self._selected_destination()
            if selected and selected.dest_key() == dest.dest_key():
                self._rebuild_add_fields(headers)

        self._run_bg(work, done, alert=False)

    def _date_value(self, record: Record) -> str:
        for key, value in record.values.items():
            if _is_date_col(key):
                return value
        return ""

    def _status_value(self, record: Record) -> str:
        for key, value in record.values.items():
            if _is_status_col(key):
                return value
        return ""

    def _filtered_records(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        date_query = self.date_var.get().strip().lower()
        service_query = self.service_filter.get().strip()
        address_query = self.address_filter.get().strip()
        out = []
        for record in self.records:
            blob = " ".join([record.source_name, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            if date_query:
                date_value = self._date_value(record).lower()
                if date_query not in date_value and date_query not in blob:
                    continue
            if service_query and service_query != "Все":
                if service_value(record.values) != service_query:
                    continue
            if address_query and address_query != "Все":
                if address_value(record.values) != address_query:
                    continue
            out.append(record)
        return out

    def _refresh_split_filters(self) -> None:
        services = sorted({service_value(record.values) for record in self.records if service_value(record.values)})
        addresses = sorted({address_value(record.values) for record in self.records if address_value(record.values)})
        current_service = self.service_filter.get()
        current_address = self.address_filter.get()
        self.service_menu.configure(values=["Все", *services] if services else ["Все"])
        self.address_menu.configure(values=["Все", *addresses] if addresses else ["Все"])
        if current_service not in ["Все", *services]:
            self.service_filter.set("Все")
        if current_address not in ["Все", *addresses]:
            self.address_filter.set("Все")

    def _columns(self, records: list[Record]) -> list[str]:
        seen: list[str] = []
        for record in records:
            for key in record.values:
                if key not in seen and key not in HIDDEN:
                    seen.append(key)
        return ["Источник", *seen]

    def _render_table(self) -> None:
        records = self._filtered_records()
        calendar = [item for item in records if item.layout == "calendar"]
        if calendar:
            self._show_calendar(True)
            self._draw_calendars(calendar)
            booked = sum(1 for item in calendar if item.values.get("Статус") == "Занято")
            self._visible = calendar
            self.count_label.configure(text=str(booked))
            return

        self._show_calendar(False)
        columns = self._columns(records)
        self.tree.configure(columns=columns)
        self._last_tree_width = 0
        for col in columns:
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=140, minwidth=80, stretch=True)

        self.tree.delete(*self.tree.get_children())
        for idx, record in enumerate(records):
            display = record.display_values()
            values = [display.get(col, "") for col in columns]
            tag = _status_tag(self._status_value(record)) or ("evenrow" if idx % 2 else "oddrow")
            self.tree.insert("", "end", iid=str(idx), values=values, tags=(tag,))

        self._visible = records
        self.count_label.configure(text=str(len(records)))
        self.after_idle(self._fit_columns)

    def _show_calendar(self, on: bool) -> None:
        if on:
            self.tree.grid_remove()
            self.vsb.grid_remove()
            self.cal_host.grid(row=0, column=0, sticky="nsew")
        else:
            self.cal_host.grid_remove()
            self.tree.grid(row=0, column=0, sticky="nsew")
            self.vsb.grid(row=0, column=1, sticky="ns")

    def _draw_calendars(self, records: list[Record]) -> None:
        for child in self.cal_host.winfo_children():
            child.destroy()
        self.cal_host.grid_columnconfigure(0, weight=1)
        groups: dict[tuple[str, str, str], list[Record]] = {}
        for record in records:
            groups.setdefault((record.spreadsheet_id, record.sheet, record.source_name), []).append(record)
        for index, ((_, _, name), items) in enumerate(groups.items()):
            block = tk.Frame(self.cal_host, bg=CARD, highlightthickness=0)
            block.grid(row=index, column=0, sticky="nsew", pady=(0, 10))
            self._draw_one_calendar(block, name, items)

    def _draw_one_calendar(self, block: tk.Frame, name: str, items: list[Record]) -> None:
        dates = list(dict.fromkeys(item.values.get("Дата", "") for item in items if item.values.get("Дата")))
        times = list(dict.fromkeys(item.values.get("Время", "") for item in items if item.values.get("Время")))
        times.sort(key=_time_sort_key)
        cell_map = {(item.values.get("Время", ""), item.values.get("Дата", "")): item for item in items}
        sample = items[0].values
        title = " · ".join(part for part in (name, sample.get("Адрес", ""), sample.get("Тип услуги", "")) if part)

        block.grid_columnconfigure(0, weight=0)
        for col, _date in enumerate(dates, start=1):
            block.grid_columnconfigure(col, weight=1)

        tk.Label(
            block,
            text=title,
            bg=CARD,
            fg=TEXT,
            font=_ui_font(13, bold=True),
            anchor="w",
        ).grid(row=0, column=0, columnspan=len(dates) + 1, sticky="w", padx=4, pady=(0, 6))

        tk.Label(
            block,
            text="Время",
            bg=GREEN,
            fg="#ffffff",
            font=_ui_font(11, bold=True),
            padx=8,
            pady=8,
        ).grid(row=1, column=0, sticky="nsew", padx=1, pady=1)

        for col, date in enumerate(dates, start=1):
            weekend = any(part in date.lower() for part in ("сб", "вс"))
            tk.Label(
                block,
                text=date,
                bg=GREEN,
                fg="#fce8e6" if weekend else "#ffffff",
                font=_ui_font(11, bold=True),
                padx=6,
                pady=8,
                wraplength=140,
            ).grid(row=1, column=col, sticky="nsew", padx=1, pady=1)

        for row, time in enumerate(times, start=2):
            tk.Label(
                block,
                text=time,
                bg=SLOT_TIME,
                fg=TEXT,
                font=_ui_font(12, bold=True),
                padx=8,
                pady=8,
            ).grid(row=row, column=0, sticky="nsew", padx=1, pady=1)
            for col, date in enumerate(dates, start=1):
                record = cell_map.get((time, date))
                self._draw_slot(block, record, row, col)

    def _draw_slot(self, parent: tk.Frame, record: Record | None, row: int, col: int) -> None:
        if record is None:
            tk.Label(parent, text="", bg="#eeeeee").grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
            return
        status = record.values.get("Статус", "")
        if status == "Не записывать":
            bg, fg, text = SLOT_BLOCKED, MUTED, "не записывать"
        elif status == "Занято":
            name = record.values.get("Клиент", "").strip()
            phone = record.values.get("Телефон", "").strip()
            text = f"{name}\n{phone}".strip() if phone else name
            bg, fg = SLOT_GREEN, TEXT
        else:
            bg, fg, text = SLOT_GREEN, "#1b5e20", "запись"
        label = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=_ui_font(11, bold=status == "Занято"),
            wraplength=150,
            justify="left",
            padx=6,
            pady=8,
            anchor="nw",
        )
        label.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
        label.bind("<Button-1>", lambda _event, item=record: self._pick_record(item))
        label.bind("<Double-Button-1>", lambda _event, item=record: self._edit_calendar_cell(item))

    def _pick_record(self, record: Record) -> None:
        self._picked = record
        status = record.values.get("Статус", "")
        who = record.values.get("Клиент") or status
        self._set_status(
            f"{record.values.get('Дата', '')} {record.values.get('Время', '')} · {who}"
        )

    def _edit_calendar_cell(self, record: Record) -> None:
        self._picked = record
        self._edit_cell(record, "Клиент")

    def _render_views(self) -> None:
        self._render_table()
        self._render_info()

    def _filtered_info(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        service_query = self.service_filter.get().strip()
        address_query = self.address_filter.get().strip()
        out: list[Record] = []
        for record in self.info_records:
            blob = " ".join([record.source_name, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            rec_service = service_value(record.values)
            rec_address = address_value(record.values)
            if service_query and service_query != "Все" and rec_service and rec_service != service_query:
                continue
            if address_query and address_query != "Все" and rec_address and rec_address != address_query:
                continue
            out.append(record)
        return out

    def _render_info(self) -> None:
        for child in self.info_body.winfo_children():
            child.destroy()

        records = self._filtered_info()
        if not records:
            text = (
                "Цветные блоки из таблицы появятся здесь — они читаются с того же листа под сеткой записи."
                if not self.info_records
                else "Нет строк справки под текущий фильтр."
            )
            ctk.CTkLabel(
                self.info_body,
                text=text,
                text_color=MUTED,
                anchor="w",
                justify="left",
                wraplength=900,
            ).grid(row=0, column=0, sticky="w", padx=6, pady=6)
            return

        for idx, record in enumerate(records):
            text = str(record.values.get("Текст") or "").strip()
            if not text:
                text = "\n".join(
                    str(value).strip()
                    for key, value in record.values.items()
                    if str(value).strip() and key not in HIDDEN
                )
            if not text:
                continue
            tone = record.values.get("_tone") or info_tone(text)
            bg, fg = INFO_TONES.get(tone, INFO_TONES["info"])
            card = tk.Label(
                self.info_body,
                text=text,
                bg=bg,
                fg=fg,
                font=_ui_font(13, bold=tone in {"warn", "ok"}),
                wraplength=980,
                justify="left",
                anchor="w",
                padx=14,
                pady=10,
            )
            card.grid(row=idx, column=0, sticky="ew", padx=4, pady=4)
        self.info_body.grid_columnconfigure(0, weight=1)

    def _sort_by(self, column: str) -> None:
        reverse = getattr(self, "_sort_desc", False)
        key = column if column != "Источник" else None

        def sort_key(record: Record) -> str:
            if key is None:
                return record.source_name.lower()
            return str(record.values.get(key, "")).lower()

        self.records.sort(key=sort_key, reverse=reverse)
        self._sort_desc = not reverse
        self._render_table()

    def _selected_record(self) -> Record | None:
        if self._picked and self._picked in getattr(self, "_visible", []):
            return self._picked
        selection = self.tree.selection()
        if not selection:
            return None
        idx = int(selection[0])
        visible = getattr(self, "_visible", [])
        if 0 <= idx < len(visible):
            return visible[idx]
        return None

    def _on_double_click(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        record = self._selected_record()
        if not record:
            return
        col_id = self.tree.identify_column(event.x)
        col_index = int(col_id.replace("#", "")) - 1
        columns = self.tree["columns"]
        if col_index < 0 or col_index >= len(columns):
            return
        field = columns[col_index]
        if field == "Источник":
            return
        try:
            record.header_for(field)
        except KeyError:
            messagebox.showinfo(
                "Нельзя править здесь",
                "Это поле задано в настройках таблицы (услуга/адрес) и в исходном листе его нет.",
            )
            return
        self._edit_cell(record, field)

    def _dialog(self, title: str, size: str) -> ctk.CTkToplevel:
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry(size)
        dialog.configure(fg_color=CARD)
        dialog.transient(self)
        dialog.grab_set()
        return dialog

    def _edit_cell(self, record: Record, field: str) -> None:
        dialog = self._dialog("Изменить ячейку", "480x280")
        ctk.CTkLabel(dialog, text=f"{record.source_name} · строка {record.row}", text_color=MUTED).pack(
            padx=16, pady=(16, 4), anchor="w"
        )
        placeholder, hint = _column_prompt(field)
        box = _input_box(dialog, field, hint)
        box.pack(fill="x", padx=16, pady=8)
        entry = _styled_entry(box, placeholder)
        entry.pack(fill="x", padx=10, pady=(4, 10))
        entry.insert(0, record.values.get(field, ""))
        if record.layout == "calendar" and field == "Клиент" and record.values.get("Статус") == "Свободно":
            entry.delete(0, "end")
        entry.focus()

        def save() -> None:
            value = entry.get()
            if not self.client:
                messagebox.showerror("Нет подключения", "Сначала настройте credentials.json")
                return

            def work():
                to_write = write_back_value(record, field, value)
                self.client.update_cell(record, field, to_write)
                record.values[field] = value
                if record.origin_values:
                    record.origin_values[field] = to_write
                return to_write

            def done(_value):
                if record.layout == "calendar" and field == "Клиент":
                    typed = value.strip()
                    if not typed or typed.lower() == "запись":
                        record.values["Клиент"] = ""
                        record.values["Телефон"] = ""
                        record.values["Статус"] = "Свободно"
                    elif "не запис" in typed.lower():
                        record.values["Клиент"] = typed
                        record.values["Статус"] = "Не записывать"
                    else:
                        record.values["Статус"] = "Занято"
                self._render_table()
                self._set_status(f"Записано в источник: {record.source_name} / {field}")
                dialog.destroy()

            self._run_bg(work, done)

        self._primary_button(dialog, "Сохранить в таблицу", save, 200).pack(padx=16, pady=16)
        dialog.bind("<Return>", lambda *_: save())

    def _add_to_destination(self) -> None:
        fallback = self._selected_destination()
        if not fallback and not any(item.service or item.address for item in self._valid_destinations()):
            messagebox.showinfo("Нет назначения", "В «Таблицы» укажите, куда вносить данные.")
            return
        if not self._add_fields:
            messagebox.showinfo(
                "Календарь записи",
                "Новую запись вносят в зелёную ячейку календаря: двойной клик → имя клиента.",
            )
            return
        values = {key: widget.get().strip() for key, widget in self._add_fields.items()}
        if not any(values.values()):
            messagebox.showinfo("Пустая строка", "Заполните хотя бы одно поле.")
            return
        if not self.client:
            return
        rows = explode_values(values)
        dests = self._valid_destinations()

        def work():
            written = []
            for row in rows:
                dest = self._destination_for(row, fallback)
                if not dest:
                    raise SheetsError("Не найдено назначение для этой услуги и адреса.")
                self.client.append_row(dest, row)
                written.append(dest.label())
            return written

        def done(written):
            unique = ", ".join(dict.fromkeys(written))
            self._set_status(f"Добавлено пунктов: {len(written)} → {unique}")
            for key, widget in self._add_fields.items():
                if _is_date_col(key) or _is_status_col(key):
                    continue
                if isinstance(widget, ctk.CTkEntry):
                    widget.delete(0, "end")
            dest_names = {item.name for item in dests}
            if dest_names & {item.name for item in self._valid_sources()}:
                self.reload_all()

        self._run_bg(work, done)

    def _copy_to_destination(self) -> None:
        record = self._selected_record()
        if not record:
            messagebox.showinfo("Нет строки", "Выберите строку, которую нужно внести в назначение.")
            return
        if record.layout == "calendar":
            messagebox.showinfo(
                "Календарь записи",
                "Запись уже в этой таблице. Двойной клик по ячейке — изменить имя, «Удалить строку» — освободить слот.",
            )
            return
        fallback = self._selected_destination()
        dest = self._destination_for(record.values, fallback)
        if not dest:
            messagebox.showinfo("Нет назначения", "В «Таблицы» укажите, куда вносить данные.")
            return
        if not self.client:
            return

        def work():
            headers = self.client.get_headers(dest)
            values = values_for_destination(record, headers)
            self.client.append_row(dest, values)
            return dest.label()

        def done(label):
            self._set_status(f"Пункт из «{record.source_name}» записан в «{label}»")
            if dest.name in {item.name for item in self._valid_sources()}:
                self.reload_all()

        self._run_bg(work, done)

    def _delete_row(self) -> None:
        record = self._selected_record()
        if not record:
            messagebox.showinfo("Нет ячейки", "Выберите слот в календаре или строку в таблице.")
            return
        if record.layout == "calendar":
            if not messagebox.askyesno("Освободить слот", "Заменить запись в этой ячейке на «запись» (свободно)?"):
                return
            if not self.client:
                return

            def work():
                self.client.update_cell(record, "Клиент", "")
                return True

            def done(_):
                record.values["Клиент"] = ""
                record.values["Телефон"] = ""
                record.values["Статус"] = "Свободно"
                self._render_table()
                self._set_status("Слот освобождён")

            self._run_bg(work, done)
            return
        if record.split_from:
            message = (
                f"Эта строка — часть исходного пункта. Удалить целиком строку {record.row} "
                f"из «{record.source_name}» (все услуги и адреса)?"
            )
        else:
            message = f"Удалить строку {record.row} из «{record.source_name}»?"
        if not messagebox.askyesno("Удалить строку", message):
            return
        if not self.client:
            return

        def work():
            self.client.delete_row(record)
            return True

        def done(_):
            self.reload_all()

        self._run_bg(work, done)

    def _export_csv(self) -> None:
        records = self._filtered_records()
        if not records:
            messagebox.showinfo("Нет данных", "Нечего экспортировать.")
            return
        columns = self._columns(records)
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"sheets_hub_{date.today().isoformat()}.csv",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for record in records:
                display = record.display_values()
                writer.writerow([display.get(col, "") for col in columns])
        self._set_status(f"Экспортировано: {path}")

    def _open_tables_dialog(self) -> None:
        self.update_idletasks()
        width = max(1040, self.winfo_width() - 48)
        height = max(720, self.winfo_height() - 48)
        dialog = self._dialog("Таблицы", f"{width}x{height}")
        dialog.minsize(960, 640)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        account = ctk.CTkFrame(dialog, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        account.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        account.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            account,
            text="Аккаунт Google",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            account,
            text="Не логин и пароль. Нужен JSON-ключ сервисного аккаунта. "
            "Каждую таблицу откройте этому email как Редактор.",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            wraplength=980,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 8))

        creds_path = self.config_data.credentials
        email = ""
        if self.client:
            email = self.client.service_email
        elif creds_path.exists():
            email = credentials_email(creds_path)

        path_var = tk.StringVar(value=str(creds_path))
        key_box = _input_box(
            account,
            "JSON-ключ",
            "файл, скачанный из Google Cloud → Keys → Create key. Не заполняйте руками.",
        )
        key_box.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 4))
        _styled_entry(
            key_box,
            "например C:\\...\\credentials.json",
            textvariable=path_var,
        ).pack(fill="x", padx=10, pady=(4, 10))

        email_var = tk.StringVar(value=email or "файл ещё не выбран")
        ctk.CTkLabel(account, text="Email", text_color=MUTED, font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=3, column=0, sticky="w", padx=12, pady=(0, 10)
        )
        ctk.CTkLabel(account, textvariable=email_var, text_color=GREEN, anchor="w").grid(
            row=3, column=1, sticky="w", padx=8, pady=(0, 10)
        )

        def pick_key() -> None:
            if self._pick_credentials_file(parent=dialog):
                installed = self.config_data.credentials
                path_var.set(str(installed))
                email_var.set(credentials_email(installed) or installed.name)

        self._outline_button(account, "Выбрать JSON-ключ…", pick_key, 180).grid(
            row=2, column=2, padx=(0, 12), pady=(0, 4)
        )

        lists = ctk.CTkFrame(dialog, fg_color="transparent")
        lists.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        lists.grid_columnconfigure(0, weight=1)

        sources_editor = _RefList(
            lists,
            "Откуда брать данные",
            self.config_data.sources,
            "Источник",
            show_kind=True,
        )
        sources_editor.frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        dest_editor = _RefList(
            lists,
            "Куда вносить данные",
            self.config_data.destinations,
            "Назначение",
            show_kind=False,
        )
        dest_editor.frame.grid(row=1, column=0, sticky="ew")

        def save() -> None:
            sources = sources_editor.collect()
            destinations = dest_editor.collect()
            self.config_data.sources = sources
            self.config_data.destinations = destinations
            save_config(self.config_data)
            self._dest_headers.clear()
            dialog.destroy()
            self.client = None
            self._try_connect()

        self._primary_button(dialog, "Сохранить таблицы", save, 200).grid(row=2, column=0, pady=(0, 16))


class _RefList:
    def __init__(
        self,
        parent,
        title: str,
        refs: list[SheetRef],
        placeholder: str,
        show_kind: bool = False,
    ) -> None:
        self.placeholder = placeholder
        self.show_kind = show_kind
        self.rows: list[dict] = []
        self._next_row = 0
        self.frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        self.frame.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w")
        hint = (
            "«Записи» — календарь, «Общая информация» — блок под ним. "
            "Лист: все или Лист1, Заказы. Вкладки «Информация», «Прайс» попадут вниз сами."
            if show_kind
            else "Лист: все — все вкладки, или Лист1, Заказы"
        )
        ctk.CTkLabel(
            header,
            text=hint,
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ctk.CTkButton(
            header,
            text="+ Добавить",
            width=110,
            height=30,
            corner_radius=8,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            command=self.add_empty,
        ).grid(row=0, column=1, sticky="e")

        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 10))
        self.body.grid_columnconfigure(0, weight=1)

        if refs:
            for ref in refs:
                self._add_row(ref)
        else:
            self.add_empty()

    def add_empty(self) -> None:
        self._add_row(SheetRef(name=self.placeholder, spreadsheet_id="", sheet="все"))

    def _add_row(self, ref: SheetRef) -> None:
        index = self._next_row
        self._next_row += 1
        name_var = tk.StringVar(value="" if ref.name in {"Источник", "Назначение", self.placeholder} else ref.name)
        url_var = tk.StringVar(value="" if ref.is_placeholder() else ref.spreadsheet_id)
        sheet_var = tk.StringVar(value=ref.sheet or "")
        service_var = tk.StringVar(value=ref.service)
        address_var = tk.StringVar(value=ref.address)
        kind_var = tk.StringVar(value=KIND_LABELS.get(ref.kind, KIND_LABELS[KIND_RECORDS]))

        row_frame = ctk.CTkFrame(
            self.body,
            fg_color=CARD,
            corner_radius=8,
            border_width=2,
            border_color=BORDER,
        )
        row_frame.grid(row=index, column=0, sticky="ew", padx=4, pady=6)
        row_frame.grid_columnconfigure(1, weight=1)

        widgets = [row_frame]

        name_box = _labeled_field(row_frame, "Название")
        name_box.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=(8, 4))
        widgets.append(_styled_entry(name_box, "Гинеколог", width=160, textvariable=name_var))
        widgets[-1].pack(fill="x", pady=(4, 0))

        url_box = _labeled_field(row_frame, "Ссылка на таблицу")
        url_box.grid(row=0, column=1, sticky="ew", padx=6, pady=(8, 4))
        widgets.append(
            _styled_entry(url_box, "https://docs.google.com/spreadsheets/d/...", textvariable=url_var)
        )
        widgets[-1].pack(fill="x", pady=(4, 0))

        sheet_box = _labeled_field(row_frame, "Лист", "все или Лист1, Заказы")
        sheet_box.grid(row=0, column=2, sticky="ew", padx=6, pady=(8, 4))
        widgets.append(_styled_entry(sheet_box, "все", width=180, textvariable=sheet_var))
        widgets[-1].pack(fill="x", pady=(4, 0))

        service_box = _labeled_field(row_frame, "Тип услуги")
        service_box.grid(row=1, column=0, sticky="ew", padx=(10, 6), pady=(0, 10))
        widgets.append(_styled_entry(service_box, "Стрижка", width=160, textvariable=service_var))
        widgets[-1].pack(fill="x", pady=(4, 0))

        address_box = _labeled_field(row_frame, "Адрес")
        address_box.grid(row=1, column=1, sticky="ew", padx=6, pady=(0, 10))
        widgets.append(_styled_entry(address_box, "ул. Ленина", textvariable=address_var))
        widgets[-1].pack(fill="x", pady=(4, 0))

        if self.show_kind:
            kind_box = _labeled_field(row_frame, "Тип")
            kind_box.grid(row=1, column=2, sticky="ew", padx=6, pady=(0, 10))
            kind_menu = ctk.CTkOptionMenu(
                kind_box,
                variable=kind_var,
                values=[KIND_LABELS[KIND_RECORDS], KIND_LABELS[KIND_INFO]],
                height=36,
                fg_color=CARD,
                button_color=GREEN,
                button_hover_color=GREEN_HOVER,
                text_color=TEXT,
                dropdown_fg_color=CARD,
                dropdown_text_color=TEXT,
            )
            kind_menu.pack(fill="x", pady=(4, 0))
            widgets.append(kind_menu)

        row_data = {
            "name": name_var,
            "url": url_var,
            "sheet": sheet_var,
            "service": service_var,
            "address": address_var,
            "kind": kind_var,
            "map": dict(ref.map),
            "widgets": widgets,
        }

        def remove() -> None:
            for widget in row_data["widgets"]:
                widget.destroy()
            self.rows.remove(row_data)

        remove_btn = ctk.CTkButton(
            row_frame,
            text="✕",
            width=36,
            height=34,
            corner_radius=8,
            fg_color=CARD,
            hover_color="#fce8e6",
            border_width=2,
            border_color=BORDER,
            text_color=DANGER,
            command=remove,
        )
        remove_btn.grid(row=0, column=3, rowspan=2, padx=(6, 10), pady=8, sticky="n")
        widgets.append(remove_btn)
        self.rows.append(row_data)

    def collect(self) -> list[SheetRef]:
        out: list[SheetRef] = []
        for row in self.rows:
            url = row["url"].get().strip()
            if not url:
                continue
            out.append(
                SheetRef(
                    name=row["name"].get().strip() or self.placeholder,
                    spreadsheet_id=url,
                    sheet=row["sheet"].get().strip(),
                    map=row["map"],
                    service=row["service"].get().strip(),
                    address=row["address"].get().strip(),
                    kind=KIND_INFO if row["kind"].get() == KIND_LABELS[KIND_INFO] else KIND_RECORDS,
                )
            )
        return out


def main() -> None:
    configure_tls()
    _enable_windows_dpi()
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    app = SheetsHubApp()
    app.mainloop()
