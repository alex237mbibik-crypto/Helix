from __future__ import annotations

import csv
import sys
import threading
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import customtkinter as ctk
import tkinter as tk

from sheets_hub.client import SheetsClient, SheetsError, values_for_destination
from sheets_hub.config import (
    AppConfig,
    SheetRef,
    credentials_email,
    install_credentials,
    load_config,
    save_config,
)
from sheets_hub.models import Record
from sheets_hub.split import (
    address_value,
    explode_records,
    explode_values,
    match_destination,
    service_value,
    write_back_value,
)

HIDDEN = {"_sid", "_sheet", "_row"}
STATUS_OPTIONS = ("Ожидание", "Подтверждено", "Отменено")

GREEN = "#188038"
GREEN_HOVER = "#137333"
BG = "#f8f9fa"
CARD = "#ffffff"
BORDER = "#dadce0"
LINE = "#e8eaed"
TEXT = "#202124"
MUTED = "#5f6368"
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
        self._visible: list[Record] = []
        self._jobs = 0
        self._last_tree_width = 0
        self._sort_desc = False
        self._add_fields: dict[str, ctk.CTkEntry | ctk.CTkComboBox] = {}
        self._dest_headers: dict[str, list[str]] = {}
        self._dest_by_label: dict[str, SheetRef] = {}

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

        self.dest_var = tk.StringVar(value="")
        self.dest_menu = ctk.CTkOptionMenu(
            top,
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
        self.dest_menu.pack(side="left", padx=(8, 0))

        self.add_fields_wrap = ctk.CTkFrame(self.add_form, fg_color="transparent")
        self.add_fields_wrap.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 12))

        self._rebuild_add_fields([])

    def _rebuild_add_fields(self, headers: list[str]) -> None:
        for child in self.add_fields_wrap.winfo_children():
            child.destroy()
        self._add_fields = {}

        if not headers:
            ctk.CTkLabel(
                self.add_fields_wrap,
                text="Укажите таблицу-назначение в «Таблицы» — сюда будут попадать новые строки.",
                text_color=MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return

        cols = 4
        for i, header in enumerate(headers):
            row, col = divmod(i, cols)
            field = ctk.CTkFrame(self.add_fields_wrap, fg_color="transparent")
            field.grid(row=row, column=col, sticky="ew", padx=6, pady=4)
            self.add_fields_wrap.grid_columnconfigure(col, weight=1)
            ctk.CTkLabel(
                field,
                text=header,
                text_color=MUTED,
                font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            ).pack(fill="x")
            if _is_status_col(header):
                widget = ctk.CTkComboBox(
                    field,
                    values=list(STATUS_OPTIONS),
                    height=36,
                    corner_radius=8,
                    border_color=BORDER,
                    fg_color=CARD,
                    button_color=GREEN,
                    button_hover_color=GREEN_HOVER,
                )
                widget.set(STATUS_OPTIONS[0])
            else:
                widget = ctk.CTkEntry(
                    field,
                    height=36,
                    corner_radius=8,
                    border_color=BORDER,
                    fg_color=CARD,
                    text_color=TEXT,
                )
                if _is_date_col(header):
                    widget.insert(0, date.today().isoformat())
            widget.pack(fill="x")
            self._add_fields[header] = widget

        last_row = (len(headers) - 1) // cols
        btn_wrap = ctk.CTkFrame(self.add_fields_wrap, fg_color="transparent")
        btn_wrap.grid(row=last_row + 1, column=0, sticky="w", padx=6, pady=(8, 0))
        self._primary_button(btn_wrap, "Добавить", self._add_to_destination, 140).pack(side="left")

    def _build_filter(self, parent: ctk.CTkFrame) -> None:
        filter_area = ctk.CTkFrame(parent, fg_color="transparent")
        filter_area.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 4))
        filter_area.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            filter_area,
            text="Поиск",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, padx=(0, 8), pady=6, sticky="w")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_table())
        ctk.CTkEntry(
            filter_area,
            textvariable=self.search_var,
            placeholder_text="фильтр по любой колонке",
            height=36,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            text_color=TEXT,
            font=ctk.CTkFont(size=13),
        ).grid(row=0, column=1, sticky="ew", pady=6, padx=(0, 12))

        ctk.CTkLabel(
            filter_area,
            text="Дата",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=2, padx=(0, 8), pady=6, sticky="w")

        self.date_var = tk.StringVar()
        self.date_var.trace_add("write", lambda *_: self._render_table())
        ctk.CTkEntry(
            filter_area,
            textvariable=self.date_var,
            placeholder_text="ГГГГ-ММ-ДД",
            width=140,
            height=36,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            text_color=TEXT,
        ).grid(row=0, column=3, pady=6, padx=(0, 8))

        ctk.CTkLabel(
            filter_area,
            text="Услуга",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=1, column=0, padx=(0, 8), pady=6, sticky="w")
        self.service_filter = tk.StringVar(value="Все")
        self.service_filter.trace_add("write", lambda *_: self._render_table())
        self.service_menu = ctk.CTkOptionMenu(
            filter_area,
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
        self.service_menu.grid(row=1, column=1, sticky="w", pady=6)

        ctk.CTkLabel(
            filter_area,
            text="Адрес",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=1, column=2, padx=(0, 8), pady=6, sticky="w")
        self.address_filter = tk.StringVar(value="Все")
        self.address_filter.trace_add("write", lambda *_: self._render_table())
        self.address_menu = ctk.CTkOptionMenu(
            filter_area,
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
        self.address_menu.grid(row=1, column=3, pady=6, sticky="w")

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
        table_wrap.grid_rowconfigure(0, weight=1)

        inner = tk.Frame(table_wrap, bg=CARD, highlightthickness=0)
        inner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(inner, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(inner, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<Delete>", lambda *_: self._delete_row())
        self._style_treeview()

    def _build_footer(self, parent: ctk.CTkFrame) -> None:
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=22, pady=(4, 14))
        footer.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            footer,
            text="Читаем из источников · одна строка с несколькими услугами/адресами делится на пункты · пишем в назначение по услуге и адресу",
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

    def _selected_destination(self) -> SheetRef | None:
        label = self.dest_var.get().strip()
        if label in self._dest_by_label:
            return self._dest_by_label[label]
        return next((item for item in self._valid_destinations() if item.label() == label), None)

    def _destination_for(self, values: dict[str, str], fallback: SheetRef | None = None) -> SheetRef | None:
        dests = self._valid_destinations()
        tagged = [item for item in dests if item.service or item.address]
        chosen = fallback or self._selected_destination()
        if tagged:
            return match_destination(service_value(values), address_value(values), tagged, chosen)
        return chosen

    def _refresh_dest_menu(self) -> None:
        dests = self._valid_destinations()
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

    def _run_bg(self, work: Callable, done: Callable) -> None:
        self._jobs += 1
        self.refresh_btn.configure(state="disabled")

        def runner() -> None:
            error = None
            result = None
            try:
                result = work()
            except Exception as exc:
                error = exc
            self.after(0, lambda: self._finish_bg(done, result, error))

        threading.Thread(target=runner, daemon=True).start()

    def _finish_bg(self, done: Callable, result, error) -> None:
        self._jobs = max(0, self._jobs - 1)
        if self._jobs == 0:
            self.refresh_btn.configure(state="normal")
        if error:
            self._set_status(f"Ошибка: {error}")
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
            self._render_table()
            self._set_status("Нет таблиц-источников. Откройте «Таблицы» и вставьте ссылки.")
            return
        self._set_status("Читаю таблицы…")

        def work():
            return self.client.fetch_all(sources)

        def done(result):
            records, errors = result
            raw_count = len(records)
            self.records = explode_records(records)
            self._refresh_split_filters()
            self._render_table()
            extra = f" · {len(errors)} ошибок" if errors else ""
            split_note = ""
            if len(self.records) > raw_count:
                split_note = f" · разбито на {len(self.records)} пунктов"
            self._set_status(f"Строк: {raw_count} из {len(sources)} источников{split_note}{extra}")
            if errors:
                messagebox.showwarning("Часть таблиц не загрузилась", "\n\n".join(errors[:8]))
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

        self._run_bg(work, done)

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
        dialog = self._dialog("Изменить ячейку", "460x240")
        ctk.CTkLabel(dialog, text=f"{record.source_name} · строка {record.row}", text_color=MUTED).pack(
            padx=16, pady=(16, 4), anchor="w"
        )
        ctk.CTkLabel(dialog, text=field, font=ctk.CTkFont(weight="bold"), text_color=TEXT).pack(
            padx=16, pady=4, anchor="w"
        )
        entry = ctk.CTkEntry(
            dialog, width=420, height=36, corner_radius=8, border_color=BORDER, fg_color=CARD, text_color=TEXT
        )
        entry.pack(padx=16, pady=8)
        entry.insert(0, record.values.get(field, ""))
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
            messagebox.showinfo("Нет колонок", "В таблице-назначении нет заголовков в первой строке.")
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
            messagebox.showinfo("Нет строки", "Выберите строку для удаления.")
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
        dialog = self._dialog("Таблицы", "1100x680")
        dialog.minsize(860, 520)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)
        dialog.grid_rowconfigure(2, weight=1)

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

        ctk.CTkLabel(account, text="Ключ", text_color=MUTED, font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=2, column=0, sticky="w", padx=12, pady=(0, 4)
        )
        path_var = tk.StringVar(value=str(creds_path))
        ctk.CTkEntry(
            account,
            textvariable=path_var,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            text_color=TEXT,
        ).grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 4))

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

        sources_editor = _RefList(
            dialog,
            "Откуда брать данные",
            self.config_data.sources,
            "Источник",
        )
        sources_editor.frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)

        dest_editor = _RefList(
            dialog,
            "Куда вносить данные",
            self.config_data.destinations,
            "Назначение",
        )
        dest_editor.frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)

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

        self._primary_button(dialog, "Сохранить таблицы", save, 200).grid(row=3, column=0, pady=(0, 16))


class _RefList:
    def __init__(self, parent, title: str, refs: list[SheetRef], placeholder: str) -> None:
        self.placeholder = placeholder
        self.rows: list[dict] = []
        self._next_row = 0
        self.frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="для каждой строки: тип услуги и адрес",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
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

        self.body = ctk.CTkScrollableFrame(self.frame, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))
        self.body.grid_columnconfigure(1, weight=1)

        if refs:
            for ref in refs:
                self._add_row(ref)
        else:
            self.add_empty()

    def add_empty(self) -> None:
        self._add_row(SheetRef(name=self.placeholder, spreadsheet_id="", sheet="Лист1"))

    def _add_row(self, ref: SheetRef) -> None:
        index = self._next_row
        self._next_row += 1
        name_var = tk.StringVar(value=ref.name)
        url_var = tk.StringVar(value="" if ref.is_placeholder() else ref.spreadsheet_id)
        sheet_var = tk.StringVar(value=ref.sheet or "Лист1")
        service_var = tk.StringVar(value=ref.service)
        address_var = tk.StringVar(value=ref.address)

        ctk.CTkEntry(
            self.body,
            textvariable=name_var,
            width=120,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            placeholder_text="Название",
        ).grid(row=index, column=0, padx=4, pady=4, sticky="w")
        url_entry = ctk.CTkEntry(
            self.body,
            textvariable=url_var,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            placeholder_text="Ссылка или ID Google Таблицы",
        )
        url_entry.grid(row=index, column=1, padx=4, pady=4, sticky="ew")
        ctk.CTkEntry(
            self.body,
            textvariable=sheet_var,
            width=90,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            placeholder_text="Лист",
        ).grid(row=index, column=2, padx=4, pady=4)
        ctk.CTkEntry(
            self.body,
            textvariable=service_var,
            width=120,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            placeholder_text="Тип услуги",
        ).grid(row=index, column=3, padx=4, pady=4)
        ctk.CTkEntry(
            self.body,
            textvariable=address_var,
            width=140,
            height=34,
            corner_radius=8,
            border_color=BORDER,
            fg_color=CARD,
            placeholder_text="Адрес",
        ).grid(row=index, column=4, padx=4, pady=4)
        row_data = {
            "name": name_var,
            "url": url_var,
            "sheet": sheet_var,
            "service": service_var,
            "address": address_var,
            "map": dict(ref.map),
            "widgets": [],
        }

        def remove() -> None:
            for widget in row_data["widgets"]:
                widget.destroy()
            self.rows.remove(row_data)

        remove_btn = ctk.CTkButton(
            self.body,
            text="✕",
            width=36,
            height=34,
            corner_radius=8,
            fg_color=CARD,
            hover_color="#fce8e6",
            border_width=1,
            border_color=BORDER,
            text_color=DANGER,
            command=remove,
        )
        remove_btn.grid(row=index, column=5, padx=4, pady=4)
        row_data["widgets"] = [
            widget
            for widget in self.body.grid_slaves(row=index)
        ]
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
                    sheet=row["sheet"].get().strip() or "Лист1",
                    map=row["map"],
                    service=row["service"].get().strip(),
                    address=row["address"].get().strip(),
                )
            )
        return out


def main() -> None:
    _enable_windows_dpi()
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    app = SheetsHubApp()
    app.mainloop()
