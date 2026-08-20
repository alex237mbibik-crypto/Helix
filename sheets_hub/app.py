from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import customtkinter as ctk
import tkinter as tk

from sheets_hub.calendar_sheet import info_tone
from sheets_hub.client import SheetsClient, SheetsError
from sheets_hub.config import (
    KIND_INFO,
    KIND_RECORDS,
    AppConfig,
    SheetRef,
    credentials_email,
    install_credentials,
    load_config,
    merge_tables,
    save_config,
    usable_refs,
)
from sheets_hub.models import Record
from sheets_hub.ssl_setup import configure_tls
from sheets_hub.split import explode_records, write_back_value

HIDDEN = {"_sid", "_sheet", "_row", "_tone"}

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


def _is_status_col(name: str) -> bool:
    key = _norm(name)
    return key in {"статус", "status", "состояние"} or "статус" in key


def _time_sort_key(text: str) -> tuple[int, int]:
    match = re.search(r"(\d{1,2})[:.\-](\d{2})", text or "")
    if not match:
        return (99, 99)
    return int(match.group(1)), int(match.group(2))


def _short_time(text: str) -> str:
    match = re.search(r"(\d{1,2})[:.\-](\d{2})", text or "")
    if not match:
        return (text or "").strip()
    return f"{int(match.group(1))}:{match.group(2)}"


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
    options = {
        "height": 36,
        "corner_radius": 6,
        "border_width": 2,
        "border_color": BORDER,
        "fg_color": CARD,
        "text_color": TEXT,
        "placeholder_text": placeholder,
        "placeholder_text_color": "#9aa0a6",
    }
    options.update(kwargs)
    return ctk.CTkEntry(parent, **options)


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
        self._picked: Record | None = None
        self._last_error_message = ""
        self.source_filter_var = tk.StringVar(value="")
        self._suppress_source_trace = False

        self._build()
        self._sync_source_filter_from_config()
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
        container.grid_rowconfigure(3, weight=1)

        self._build_header(container)
        self._build_filter(container)
        self._build_status(container)
        self._build_table(container)
        self._build_info_panel(container)

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
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))
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

        self.refresh_btn = self._primary_button(toolbar, "Обновить", self.reload_all, 110)
        self.refresh_btn.pack(side="left", padx=(0, 8))
        self._outline_button(toolbar, "Таблицы", self._open_tables_dialog, 110).pack(side="left")

    def _build_filter(self, parent: ctk.CTkFrame) -> None:
        filter_area = ctk.CTkFrame(parent, fg_color="transparent")
        filter_area.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 2))
        filter_area.grid_columnconfigure(0, weight=1)
        filter_area.grid_columnconfigure(1, weight=0)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_views())
        _styled_entry(
            filter_area,
            "Поиск по имени, дате или телефону",
            height=32,
            textvariable=self.search_var,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        source_box = ctk.CTkFrame(filter_area, fg_color="transparent")
        source_box.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            source_box,
            text="Таблица",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 8))
        self.source_filter = ctk.CTkComboBox(
            source_box,
            values=[""],
            variable=self.source_filter_var,
            width=190,
            height=32,
            corner_radius=6,
            border_width=1,
            border_color=BORDER,
            fg_color=CARD,
            button_color=LINE,
            button_hover_color=HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_text_color=TEXT,
            dropdown_hover_color=HOVER,
            font=ctk.CTkFont(size=13),
            state="readonly",
            command=lambda _value: self._on_source_filter_changed(),
        )
        self.source_filter.pack(side="left")
        self.source_filter.set("Таблица")
        ctk.CTkLabel(
            filter_area,
            text="Нажмите ячейку, чтобы записать имя. Пустое поле — слот свободен.",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_status(self, parent: ctk.CTkFrame) -> None:
        self.status = ctk.CTkLabel(
            parent,
            text="Загрузка…",
            anchor="w",
            text_color=MUTED,
            font=ctk.CTkFont(size=13),
        )
        self.status.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 4))

    def _build_table(self, parent: ctk.CTkFrame) -> None:
        table_wrap = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=10,
            border_width=1,
            border_color="#e0e0e0",
        )
        table_wrap.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 4))
        table_wrap.grid_columnconfigure(0, weight=1)
        table_wrap.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            table_wrap,
            text="Календарь записи",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(6, 2))

        inner = tk.Frame(table_wrap, bg=CARD, highlightthickness=0)
        inner.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(1, weight=1)
        self._table_inner = inner

        self.tree = ttk.Treeview(inner, show="headings", selectmode="browse")
        self.vsb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.tree.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.vsb.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<Delete>", lambda *_: self._delete_row())
        self._style_treeview()

        # Шапка календаря (даты) — не скроллится по вертикали.
        self.cal_header = tk.Frame(inner, bg=CARD, highlightthickness=0)
        self.cal_header_title = tk.Label(
            self.cal_header,
            text="",
            bg=CARD,
            fg=TEXT,
            font=_ui_font(13, bold=True),
            anchor="w",
        )
        self.cal_header_title.pack(fill="x", padx=4, pady=(0, 2))
        self.cal_header_canvas = tk.Canvas(self.cal_header, bg=CARD, highlightthickness=0, bd=0, height=36)
        self.cal_header_canvas.pack(fill="x", expand=False)
        self.cal_header_host = tk.Frame(self.cal_header_canvas, bg=CARD)
        self._cal_header_window = self.cal_header_canvas.create_window(
            (0, 0), window=self.cal_header_host, anchor="nw"
        )
        self.cal_header_host.bind("<Configure>", self._on_cal_header_configure)
        self.cal_header.bind("<MouseWheel>", self._on_cal_wheel)
        self.cal_header_canvas.bind("<MouseWheel>", self._on_cal_wheel)
        self.cal_header.bind("<Shift-MouseWheel>", self._on_cal_shift_wheel)
        self.cal_header_canvas.bind("<Shift-MouseWheel>", self._on_cal_shift_wheel)

        self.cal_canvas = tk.Canvas(inner, bg=CARD, highlightthickness=0, bd=0)
        self.cal_vsb = ttk.Scrollbar(inner, orient="vertical", command=self.cal_canvas.yview)
        self.cal_hsb = ttk.Scrollbar(inner, orient="horizontal", command=self._on_cal_xscroll)
        self.cal_canvas.configure(yscrollcommand=self.cal_vsb.set, xscrollcommand=self._on_cal_xscroll_set)
        self.cal_host = tk.Frame(self.cal_canvas, bg=CARD)
        self._cal_window = self.cal_canvas.create_window((0, 0), window=self.cal_host, anchor="nw")
        self.cal_host.bind("<Configure>", self._on_cal_host_configure)
        self.cal_canvas.bind("<Configure>", self._on_cal_canvas_configure)
        self.cal_canvas.bind("<Enter>", lambda *_: self.cal_canvas.focus_set())
        self.cal_canvas.bind("<MouseWheel>", self._on_cal_wheel)
        self.cal_host.bind("<MouseWheel>", self._on_cal_wheel)
        self.cal_canvas.bind("<Shift-MouseWheel>", self._on_cal_shift_wheel)
        self.bind_all("<MouseWheel>", self._on_cal_wheel)
        self.bind_all("<Shift-MouseWheel>", self._on_cal_shift_wheel)
        self._cal_time_col_w = 64
        self._cal_date_cols = 0
        self._cal_syncing_x = False

        self.empty_state = ctk.CTkFrame(inner, fg_color=CARD, corner_radius=10)
        self.empty_state.grid_columnconfigure(0, weight=1)
        self.empty_title = ctk.CTkLabel(
            self.empty_state,
            text="",
            text_color=TEXT,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.empty_title.grid(row=0, column=0, pady=(120, 8), padx=24)
        self.empty_text = ctk.CTkLabel(
            self.empty_state,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(size=13),
            justify="center",
            wraplength=560,
        )
        self.empty_text.grid(row=1, column=0, pady=(0, 14), padx=24)
        self.empty_retry = self._primary_button(self.empty_state, "Повторить", self.reload_all, 140)
        self.empty_retry.grid(row=2, column=0, pady=(0, 120))
        self.empty_state.grid_remove()

    def _build_info_panel(self, parent: ctk.CTkFrame) -> None:
        self.info_wrap = ctk.CTkFrame(
            parent,
            fg_color=GREEN_SOFT,
            corner_radius=10,
            border_width=1,
            border_color="#ceead6",
        )
        self.info_wrap.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.info_wrap,
            text="Общая информация",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(6, 0))

        self.info_body = ctk.CTkFrame(self.info_wrap, fg_color="transparent")
        self.info_body.pack(fill="x", padx=6, pady=(4, 8))
        self.info_body.grid_columnconfigure(0, weight=1)
        self.info_wrap.grid_remove()

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

    def _on_cal_header_configure(self, _event=None) -> None:
        self.cal_header_canvas.configure(scrollregion=self.cal_header_canvas.bbox("all"))
        self.cal_header_canvas.itemconfigure(
            self._cal_header_window,
            width=max(self.cal_header_canvas.winfo_width(), self.cal_header_host.winfo_reqwidth()),
        )
        height = max(28, self.cal_header_host.winfo_reqheight())
        self.cal_header_canvas.configure(height=height)

    def _on_cal_xscroll(self, *args) -> None:
        self.cal_canvas.xview(*args)
        self.cal_header_canvas.xview(*args)

    def _on_cal_xscroll_set(self, first, last) -> None:
        self.cal_hsb.set(first, last)
        if self._cal_syncing_x:
            return
        self._cal_syncing_x = True
        try:
            self.cal_header_canvas.xview_moveto(first)
        finally:
            self._cal_syncing_x = False

    def _on_cal_host_configure(self, _event=None) -> None:
        self.cal_canvas.configure(scrollregion=self.cal_canvas.bbox("all"))
        self._fit_calendar_to_canvas()

    def _on_cal_canvas_configure(self, event) -> None:
        self._fit_calendar_to_canvas(event.width, event.height)
        self._on_cal_header_configure()

    def _fit_calendar_to_canvas(self, width: int | None = None, height: int | None = None) -> None:
        if width is None:
            width = self.cal_canvas.winfo_width()
        if height is None:
            height = self.cal_canvas.winfo_height()
        if width < 40 or height < 40:
            return
        req_w = max(width, self.cal_host.winfo_reqwidth(), self.cal_header_host.winfo_reqwidth())
        req_h = max(height, self.cal_host.winfo_reqheight())
        self.cal_canvas.itemconfigure(self._cal_window, width=req_w, height=req_h)
        self.cal_host.configure(width=req_w, height=req_h)
        self.cal_canvas.configure(scrollregion=(0, 0, req_w, req_h))
        self.cal_header_canvas.itemconfigure(self._cal_header_window, width=req_w)
        self.cal_header_host.configure(width=req_w)
        self.cal_header_canvas.configure(scrollregion=(0, 0, req_w, self.cal_header_host.winfo_reqheight()))

    def _on_cal_wheel(self, event) -> None:
        if not self._pointer_over_calendar(event):
            return
        steps = self._wheel_steps(event)
        if steps:
            self.cal_canvas.yview_scroll(steps, "units")
        return "break"

    def _on_cal_shift_wheel(self, event) -> None:
        if not self._pointer_over_calendar(event):
            return
        steps = self._wheel_steps(event)
        if steps:
            self._on_cal_xscroll("scroll", steps, "units")
        return "break"

    def _pointer_over_calendar(self, event) -> bool:
        widgets = [
            getattr(self, "cal_canvas", None),
            getattr(self, "cal_header", None),
            getattr(self, "cal_header_canvas", None),
        ]
        for widget in widgets:
            if widget is None or not widget.winfo_ismapped():
                continue
            try:
                x, y = widget.winfo_rootx(), widget.winfo_rooty()
                w, h = widget.winfo_width(), widget.winfo_height()
            except tk.TclError:
                continue
            if x <= event.x_root <= x + w and y <= event.y_root <= y + h:
                return True
        return False

    def _wheel_steps(self, event) -> int:
        if sys.platform == "darwin":
            return int(-event.delta)
        return int(-event.delta / 120) if event.delta else 0

    def _refresh_cal_scroll(self) -> None:
        self.cal_host.update_idletasks()
        self.cal_header_host.update_idletasks()
        self.cal_canvas.yview_moveto(0)
        self.cal_canvas.xview_moveto(0)
        self.cal_header_canvas.xview_moveto(0)
        self._fit_calendar_to_canvas()
        self._on_cal_header_configure()

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _tables(self) -> list[SheetRef]:
        tables = merge_tables(self.config_data.sources, self.config_data.destinations)
        return tables or usable_refs(self.config_data.sources) or usable_refs(self.config_data.destinations)

    def _valid_sources(self) -> list[SheetRef]:
        return self._tables()

    def _selected_sources(self) -> list[SheetRef]:
        tables = [ref for ref in self._tables() if not ref.is_placeholder()]
        if not tables:
            return []
        selected = self.source_filter_var.get().strip()
        if selected:
            matched = [ref for ref in tables if ref.name == selected]
            if matched:
                return matched
        return [tables[0]]

    def _sync_source_filter_from_config(self) -> None:
        names = [ref.name for ref in self._tables() if not ref.is_placeholder() and ref.name]
        values = names or [""]
        current = self.source_filter_var.get().strip()
        self._suppress_source_trace = True
        try:
            self.source_filter.configure(values=values)
            if current in values and current:
                self.source_filter_var.set(current)
            elif values and values[0]:
                self.source_filter_var.set(values[0])
            else:
                self.source_filter_var.set("")
                self.source_filter.set("Таблица")
        finally:
            self._suppress_source_trace = False

    def _on_source_filter_changed(self, *_args) -> None:
        if getattr(self, "_suppress_source_trace", False):
            return
        selected = self.source_filter_var.get().strip()
        if not selected or selected == "Таблица":
            self._render_views()
            return
        # Уже показана эта таблица — просто перерисовать.
        if any(record.source_name == selected for record in self.records) or any(
            record.source_name == selected for record in self.info_records
        ):
            self._render_views()
            return
        if self.client:
            self.reload_all()

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
            if self._valid_sources():
                self.reload_all()
            else:
                self._set_status("Укажите таблицы в «Таблицы» — ссылку, с которой читать и куда писать.")
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
        self.refresh_btn.configure(text="Обновляю…")

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
            self.refresh_btn.configure(text="Обновить")
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
        self._last_error_message = ""
        self._sync_source_filter_from_config()
        sources = self._selected_sources()
        if not sources:
            self.records = []
            self.info_records = []
            self._sync_source_filter_from_config()
            self._render_table()
            self._render_info()
            self._set_status("Нет таблиц. Откройте «Таблицы» и вставьте ссылку на Google Таблицу.")
            return
        selected_name = sources[0].name
        self.records = []
        self.info_records = []
        self._render_views()
        self._set_status(f"Читаю «{selected_name}»…")

        def work():
            return self.client.fetch_all(sources)

        def done(result):
            records, errors = result
            self._last_error_message = "\n\n".join(errors[:8]) if errors else ""
            booking = [item for item in records if item.kind != KIND_INFO]
            self.info_records = [item for item in records if item.kind == KIND_INFO]
            self._sync_source_filter_from_config()
            if selected_name and selected_name in [
                ref.name for ref in self._tables() if not ref.is_placeholder()
            ]:
                self._suppress_source_trace = True
                try:
                    self.source_filter_var.set(selected_name)
                finally:
                    self._suppress_source_trace = False
            raw_count = len(booking)
            self.records = explode_records(booking)
            self._render_table()
            self._render_info()
            extra = f" · {len(errors)} ошибок" if errors else ""
            if getattr(self.client, "read_only_public", False):
                extra += " · только чтение"
            booked = sum(1 for item in self.records if item.layout == "calendar" and item.values.get("Статус") == "Занято")
            slots = sum(1 for item in self.records if item.layout == "calendar")
            info_note = f" · справка: {len(self.info_records)}" if self.info_records else ""
            if slots:
                self._set_status(f"{selected_name}: {slots} слотов, занято {booked}{info_note}{extra}")
            else:
                split_note = ""
                if len(self.records) > raw_count:
                    split_note = f" · разбито на {len(self.records)} пунктов"
                self._set_status(
                    f"{selected_name}: строк {raw_count}{split_note}{info_note}{extra}"
                )
            if errors:
                messagebox.showwarning("Таблица не загрузилась", "\n\n".join(errors[:8]))

        self._run_bg(work, done)

    def _status_value(self, record: Record) -> str:
        for key, value in record.values.items():
            if _is_status_col(key):
                return value
        return ""

    def _filtered_records(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        source_name = self.source_filter_var.get().strip()
        out = []
        for record in self.records:
            if source_name and record.source_name != source_name:
                continue
            blob = " ".join([record.source_name, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            out.append(record)
        return out

    def _columns(self, records: list[Record]) -> list[str]:
        seen: list[str] = []
        for record in records:
            for key in record.values:
                if key not in seen and key not in HIDDEN:
                    seen.append(key)
        return ["Источник", *seen]

    def _render_table(self) -> None:
        records = self._filtered_records()
        if not records:
            title = "Нет данных"
            message = "Пока нечего показать. Проверьте ссылку на таблицу и имя листа в «Таблицы»."
            can_retry = False
            if self._last_error_message:
                title = "Нет связи с Google"
                share = ""
                if self.client and getattr(self.client, "service_email", ""):
                    share = (
                        f"\n\nТаблицу откройте для доступа:\n{self.client.service_email}\n"
                        "(роль «Редактор» в Google Таблице)."
                    )
                message = (
                    "Таблица не загрузилась из Google.\n"
                    "Отключите VPN, выключите проверку HTTPS в антивирусе и нажмите «Повторить»."
                    f"{share}"
                )
                can_retry = True
            self._show_empty_state(title, message, can_retry=can_retry)
            self._visible = []
            self.count_label.configure(text="0")
            return

        self._hide_empty_state()
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

    def _show_empty_state(self, title: str, message: str, *, can_retry: bool) -> None:
        self.tree.grid_remove()
        self.vsb.grid_remove()
        self.cal_header.grid_remove()
        self.cal_canvas.grid_remove()
        self.cal_vsb.grid_remove()
        self.cal_hsb.grid_remove()
        self.empty_title.configure(text=title)
        self.empty_text.configure(text=message)
        self.empty_retry.grid() if can_retry else self.empty_retry.grid_remove()
        self.empty_state.grid(row=0, column=0, rowspan=3, columnspan=2, sticky="nsew")

    def _hide_empty_state(self) -> None:
        self.empty_state.grid_remove()

    def _show_calendar(self, on: bool) -> None:
        if on:
            self.tree.grid_remove()
            self.vsb.grid_remove()
            self.cal_header.grid(row=0, column=0, sticky="ew")
            self.cal_canvas.grid(row=1, column=0, sticky="nsew")
            self.cal_vsb.grid(row=1, column=1, sticky="ns")
            self.cal_hsb.grid(row=2, column=0, sticky="ew")
        else:
            self.cal_header.grid_remove()
            self.cal_canvas.grid_remove()
            self.cal_vsb.grid_remove()
            self.cal_hsb.grid_remove()
            self.tree.grid(row=0, column=0, rowspan=3, sticky="nsew")
            self.vsb.grid(row=0, column=1, rowspan=3, sticky="ns")

    def _draw_calendars(self, records: list[Record]) -> None:
        for child in self.cal_host.winfo_children():
            child.destroy()
        for child in self.cal_header_host.winfo_children():
            child.destroy()
        self.cal_host.grid_columnconfigure(0, weight=1)
        self.cal_host.grid_rowconfigure(0, weight=1)
        groups: dict[tuple[str, str, str], list[Record]] = {}
        for record in records:
            groups.setdefault((record.spreadsheet_id, record.sheet, record.source_name), []).append(record)
        # Один календарь: даты в зафиксированной шапке, слоты — в скролле.
        if not groups:
            self.cal_header_title.configure(text="")
            return
        ((_, _, name), items) = next(iter(groups.items()))
        self._draw_one_calendar(name, items)
        self.after_idle(self._refresh_cal_scroll)

    def _draw_one_calendar(self, name: str, items: list[Record]) -> None:
        dates = list(dict.fromkeys(item.values.get("Дата", "") for item in items if item.values.get("Дата")))
        times = list(dict.fromkeys(item.values.get("Время", "") for item in items if item.values.get("Время")))
        times.sort(key=_time_sort_key)
        cell_map = {(item.values.get("Время", ""), item.values.get("Дата", "")): item for item in items}
        sample = items[0].values
        title = " · ".join(part for part in (name, sample.get("Адрес", ""), sample.get("Тип услуги", "")) if part)
        self.cal_header_title.configure(text=title)
        self._cal_date_cols = len(dates)

        header = self.cal_header_host
        body = self.cal_host
        time_w = self._cal_time_col_w

        header.grid_columnconfigure(0, weight=0, minsize=time_w)
        body.grid_columnconfigure(0, weight=0, minsize=time_w)
        for col, _date in enumerate(dates, start=1):
            header.grid_columnconfigure(col, weight=1, minsize=96)
            body.grid_columnconfigure(col, weight=1, minsize=96)

        tk.Label(
            header,
            text="Время",
            bg=GREEN,
            fg="#ffffff",
            font=_ui_font(10, bold=True),
            padx=6,
            pady=6,
            width=6,
        ).grid(row=0, column=0, sticky="nsew", padx=1, pady=1)

        for col, date in enumerate(dates, start=1):
            weekend = any(part in date.lower() for part in ("сб", "вс"))
            tk.Label(
                header,
                text=date,
                bg=GREEN,
                fg="#fce8e6" if weekend else "#ffffff",
                font=_ui_font(10, bold=True),
                padx=4,
                pady=6,
                wraplength=120,
            ).grid(row=0, column=col, sticky="nsew", padx=1, pady=1)

        for row, time in enumerate(times):
            body.grid_rowconfigure(row, weight=1, minsize=28)
            tk.Label(
                body,
                text=_short_time(time),
                bg=SLOT_TIME,
                fg=TEXT,
                font=_ui_font(12, bold=True),
                padx=6,
                pady=2,
                width=6,
            ).grid(row=row, column=0, sticky="nsew", padx=1, pady=1)
            for col, date in enumerate(dates, start=1):
                record = cell_map.get((time, date))
                self._draw_slot(body, record, row, col)

    def _draw_slot(self, parent: tk.Frame, record: Record | None, row: int, col: int) -> None:
        if record is None:
            tk.Label(parent, text="", bg="#eeeeee").grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
            return
        status = record.values.get("Статус", "")
        if status == "Не записывать":
            bg, fg, text = SLOT_BLOCKED, MUTED, "не записывать"
        elif status == "Занято":
            text = record.values.get("Клиент", "").strip() or "занято"
            bg, fg = SLOT_GREEN, TEXT
        else:
            bg, fg, text = SLOT_GREEN, "#1b5e20", "запись"
        label = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=_ui_font(11, bold=status == "Занято"),
            wraplength=0,
            justify="left",
            padx=4,
            pady=2,
            anchor="w",
        )
        label.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
        label.bind("<Button-1>", lambda _event, item=record: self._edit_calendar_cell(item))

    def _edit_calendar_cell(self, record: Record) -> None:
        self._picked = record
        self._edit_cell(record, "Клиент")

    def _render_views(self) -> None:
        self._render_table()
        self._render_info()

    def _active_sheet_key(self) -> tuple[str, str] | None:
        for record in self.records:
            if record.layout == "calendar":
                return record.spreadsheet_id, record.sheet
        if self.records:
            item = self.records[0]
            return item.spreadsheet_id, item.sheet
        return None

    def _filtered_info(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        source_name = self.source_filter_var.get().strip()
        sheet_key = self._active_sheet_key()
        out: list[Record] = []
        for record in self.info_records:
            if source_name and record.source_name != source_name:
                continue
            # Справка только от того же листа, что и текущий календарь.
            if sheet_key and (record.spreadsheet_id, record.sheet) != sheet_key:
                continue
            blob = " ".join([record.source_name, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            out.append(record)
        return out

    def _render_info(self) -> None:
        for child in self.info_body.winfo_children():
            child.destroy()

        records = self._filtered_info()
        if not records:
            self.info_wrap.grid_remove()
            return
        self.info_wrap.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))

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
                font=_ui_font(12, bold=tone in {"warn", "ok"}),
                wraplength=980,
                justify="left",
                anchor="w",
                padx=10,
                pady=6,
            )
            card.grid(row=idx, column=0, sticky="ew", padx=2, pady=2)
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
        dialog.configure(fg_color=CARD)
        try:
            width, height = (int(part) for part in size.lower().split("x", 1))
        except ValueError:
            width, height = 480, 280
        screen_w = max(640, self.winfo_screenwidth())
        screen_h = max(480, self.winfo_screenheight())
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 120)
        x = max(20, (screen_w - width) // 2)
        y = max(40, (screen_h - height) // 3)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.transient(self)
        dialog.grab_set()
        return dialog

    def _edit_cell(self, record: Record, field: str) -> None:
        calendar = record.layout == "calendar" and field == "Клиент"
        when = f"{record.values.get('Дата', '')} · {record.values.get('Время', '')}".strip(" ·")
        dialog = self._dialog("Запись" if calendar else "Изменить ячейку", "480x240")
        ctk.CTkLabel(
            dialog,
            text=when or f"{record.source_name} · строка {record.row}",
            text_color=MUTED,
        ).pack(padx=16, pady=(16, 4), anchor="w")
        box = _input_box(dialog, "Имя клиента" if calendar else field, "сохранится в ту же ячейку таблицы")
        box.pack(fill="x", padx=16, pady=8)
        entry = _styled_entry(box, "Иванова А.")
        entry.pack(fill="x", padx=10, pady=(4, 10))
        entry.insert(0, record.values.get(field, ""))
        if calendar and record.values.get("Статус") == "Свободно":
            entry.delete(0, "end")
        entry.focus()

        def save() -> None:
            value = entry.get()
            if not self.client:
                messagebox.showerror("Нет подключения", "Сначала настройте ключ в «Таблицы».")
                return

            def work():
                to_write = write_back_value(record, field, value)
                self.client.update_cell(record, field, to_write)
                record.values[field] = value
                if record.origin_values:
                    record.origin_values[field] = to_write
                return to_write

            def done(_value):
                if calendar:
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
                self._set_status(f"Сохранено в таблицу: {when or record.source_name}")
                dialog.destroy()

            self._run_bg(work, done)

        def free_slot() -> None:
            entry.delete(0, "end")
            save()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        self._primary_button(buttons, "Сохранить", save, 140).pack(side="left")
        if calendar and record.values.get("Статус") == "Занято":
            ctk.CTkButton(
                buttons,
                text="Освободить",
                command=free_slot,
                width=120,
                height=36,
                corner_radius=8,
                fg_color=DANGER,
                hover_color=DANGER_HOVER,
                font=ctk.CTkFont(size=13, weight="bold"),
            ).pack(side="left", padx=(8, 0))
        dialog.bind("<Return>", lambda *_: save())

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

    def _open_tables_dialog(self) -> None:
        dialog = self._dialog("Таблицы", "820x420")
        dialog.minsize(640, 320)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        account = ctk.CTkFrame(dialog, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        account.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 8))
        account.grid_columnconfigure(1, weight=1)

        creds_path = self.config_data.credentials
        email = ""
        if self.client:
            email = self.client.service_email
        elif creds_path.exists():
            email = credentials_email(creds_path)
        path_var = tk.StringVar(value=str(creds_path))
        email_var = tk.StringVar(value=email or "файл ещё не выбран")

        ctk.CTkLabel(
            account,
            text="Ключ Google",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=10)
        _styled_entry(account, "credentials.json", textvariable=path_var, height=32).grid(
            row=0, column=1, sticky="ew", padx=8, pady=10
        )

        def pick_key() -> None:
            if self._pick_credentials_file(parent=dialog):
                installed = self.config_data.credentials
                path_var.set(str(installed))
                email_var.set(credentials_email(installed) or installed.name)

        self._outline_button(account, "Выбрать JSON…", pick_key, 140).grid(row=0, column=2, padx=8, pady=10)
        ctk.CTkLabel(account, textvariable=email_var, text_color=GREEN, anchor="w").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 8)
        )

        tables = self._tables()
        editor = _RefList(dialog, "Таблицы", tables, "Таблица")
        editor.frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))

        def save() -> None:
            refs = editor.collect()
            self.config_data.sources = refs
            self.config_data.destinations = list(refs)
            save_config(self.config_data)
            dialog.destroy()
            self.client = None
            self._try_connect()

        self._primary_button(dialog, "Сохранить таблицы", save, 200).grid(row=2, column=0, pady=(0, 14))


class _RefList:
    def __init__(
        self,
        parent,
        title: str,
        refs: list[SheetRef],
        placeholder: str,
    ) -> None:
        self.placeholder = placeholder
        self.rows: list[dict] = []
        self._next_row = 0
        self.frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="Одна ссылка: читаем и пишем сюда же. Лист — все или АВГУСТ 2026.",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=1, column=0, sticky="w")
        ctk.CTkButton(
            header,
            text="+ Добавить",
            width=110,
            height=28,
            corner_radius=8,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            command=self.add_empty,
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        colnames = ctk.CTkFrame(self.frame, fg_color="transparent")
        colnames.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 0))
        self._layout_columns(colnames)
        for col, text in enumerate(["Название", "Ссылка", "Лист", "Услуга", "Адрес"]):
            ctk.CTkLabel(
                colnames,
                text=text,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=TEXT,
                anchor="w",
            ).grid(row=0, column=col, sticky="w", padx=3)

        self.body = ctk.CTkScrollableFrame(self.frame, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self.body.grid_columnconfigure(0, weight=1)

        if refs:
            for ref in refs:
                self._add_row(ref)
        else:
            self.add_empty()

    def _layout_columns(self, widget) -> None:
        widget.grid_columnconfigure(0, weight=0, minsize=108)
        widget.grid_columnconfigure(1, weight=1, minsize=220)
        widget.grid_columnconfigure(2, weight=0, minsize=100)
        widget.grid_columnconfigure(3, weight=0, minsize=108)
        widget.grid_columnconfigure(4, weight=0, minsize=120)
        widget.grid_columnconfigure(5, weight=0, minsize=36)

    def add_empty(self) -> None:
        self._add_row(SheetRef(name=self.placeholder, spreadsheet_id="", sheet="все"))
        self.body.after(40, self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        self.body.update_idletasks()
        canvas = getattr(self.body, "_parent_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(1.0)

    def _add_row(self, ref: SheetRef) -> None:
        index = self._next_row
        self._next_row += 1
        name_var = tk.StringVar(
            value="" if ref.name in {"Источник", "Назначение", "Таблица", self.placeholder} else ref.name
        )
        url_var = tk.StringVar(value="" if ref.is_placeholder() else ref.spreadsheet_id)
        sheet_var = tk.StringVar(value=ref.sheet or "все")
        service_var = tk.StringVar(value=ref.service)
        address_var = tk.StringVar(value=ref.address)

        row_frame = ctk.CTkFrame(self.body, fg_color="transparent")
        row_frame.grid(row=index, column=0, sticky="ew", pady=2)
        self._layout_columns(row_frame)

        fields = [
            (name_var, "Гинеколог", 108),
            (url_var, "https://docs.google.com/spreadsheets/d/...", None),
            (sheet_var, "все", 100),
            (service_var, "Гинеколог", 108),
            (address_var, "ул. Ленина", 120),
        ]
        for col, (var, placeholder, width) in enumerate(fields):
            kwargs: dict = {"height": 28, "textvariable": var}
            if width is not None:
                kwargs["width"] = width
            _styled_entry(row_frame, placeholder, **kwargs).grid(
                row=0, column=col, sticky="ew", padx=2
            )

        row_data = {
            "name": name_var,
            "url": url_var,
            "sheet": sheet_var,
            "service": service_var,
            "address": address_var,
            "kind": ref.kind,
            "map": dict(ref.map),
            "frame": row_frame,
        }

        def remove() -> None:
            row_data["frame"].destroy()
            self.rows.remove(row_data)

        ctk.CTkButton(
            row_frame,
            text="✕",
            width=28,
            height=28,
            corner_radius=6,
            fg_color=CARD,
            hover_color="#fce8e6",
            border_width=1,
            border_color=LINE,
            text_color=DANGER,
            command=remove,
        ).grid(row=0, column=5, padx=(2, 4))
        self.rows.append(row_data)

    def collect(self) -> list[SheetRef]:
        out: list[SheetRef] = []
        for row in self.rows:
            url = re.sub(r"\s+", "", row["url"].get().strip())
            if not url:
                continue
            out.append(
                SheetRef(
                    name=row["name"].get().strip() or self.placeholder,
                    spreadsheet_id=url,
                    sheet=row["sheet"].get().strip() or "все",
                    map=row["map"],
                    service=row["service"].get().strip(),
                    address=row["address"].get().strip(),
                    kind=row["kind"] or KIND_RECORDS,
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
