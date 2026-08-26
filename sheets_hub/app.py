from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import customtkinter as ctk
import tkinter as tk

from sheets_hub.auth import credential_kind
from sheets_hub.calendar_sheet import classify_slot, extract_phone, info_tone, is_lock_text
from sheets_hub.client import SheetsClient, SheetsError
from sheets_hub.config import (
    KIND_INFO,
    KIND_RECORDS,
    ROOT,
    AppConfig,
    SheetRef,
    credentials_email,
    find_credentials_file,
    install_credentials,
    is_info_title,
    load_config,
    merge_tables,
    save_config,
    usable_refs,
)
from sheets_hub.models import Record
from sheets_hub.registry import DEFAULT_REGISTRY_SHEET, tables_signature
from sheets_hub.ssl_setup import configure_tls
from sheets_hub.split import explode_records, write_back_value

HIDDEN = {"_sid", "_sheet", "_row", "_tone"}

GREEN = "#188038"
GREEN_HOVER = "#137333"
GREEN_SOFT = "#e6f4ea"
SLOT_GREEN = "#7cb342"
SLOT_BOOKED = "#558b2f"
SLOT_BLOCKED = "#f1f3f4"
SLOT_LOCK = "#f9a825"
SLOT_TIME = "#f1f3f4"
SLOT_OUTLINE = "#dadce0"
AUTO_REFRESH_MS = 30_000
CAL_TIME_W = 72
CAL_DATE_W = 176
CAL_ROW_H = 72
CAL_HEADER_H = 60
CAL_GAP = 1
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
    # Не трогаем DPI вручную: CustomTkinter сам масштабирует.
    # SetProcessDpiAwareness + CTk на Windows даёт «разъехавшийся» интерфейс.
    return


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
        self._cal_frozen = False
        self._last_tree_width = 0
        self._sort_desc = False
        self._picked: Record | None = None
        self._last_error_message = ""
        self.source_filter_var = tk.StringVar(value="")
        self._suppress_source_trace = False
        self._search_after_id: str | None = None
        self._render_after_id: str | None = None
        self._calendar_fp: tuple | None = None
        self._calendar_struct_fp: tuple | None = None
        self._slot_labels: dict[tuple[str, str], tk.Label] = {}
        self._info_fp: tuple | None = None
        self._info_struct_fp: tuple | None = None
        self._info_cards: list[tk.Label] = []
        self._rendering = False
        self._connecting = False
        self._cal_draw_after_id: str | None = None
        self._cal_draw_gen = 0
        self._auto_refresh_after_id: str | None = None
        self._booking_open = 0
        self._lock_busy = False
        self._tables_dialog_open = False
        self._registry_syncing = False

        self._build()
        self._sync_source_filter_from_config()
        self.after(200, self._try_connect)
        self._schedule_auto_refresh()

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

        self.refresh_btn = self._primary_button(toolbar, "Обновить", self.reload_all, 120)
        self.refresh_btn.pack(side="left", padx=(0, 8))
        self._outline_button(toolbar, "Таблицы", self._open_tables_dialog, 110).pack(side="left")

        self.account_label = ctk.CTkLabel(
            header,
            text="Нет ключа доступа",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            anchor="e",
        )
        self.account_label.grid(row=1, column=0, columnspan=3, sticky="e", pady=(4, 0))

    def _build_filter(self, parent: ctk.CTkFrame) -> None:
        filter_area = ctk.CTkFrame(parent, fg_color="transparent")
        filter_area.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 2))
        filter_area.grid_columnconfigure(0, weight=1)
        filter_area.grid_columnconfigure(1, weight=0)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
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
        # Общая обводка вокруг текста и стрелки (у CTkComboBox кнопка иначе снаружи рамки).
        combo_wrap = ctk.CTkFrame(
            source_box,
            fg_color=CARD,
            corner_radius=6,
            border_width=1,
            border_color=BORDER,
        )
        combo_wrap.pack(side="left")
        self.source_filter = ctk.CTkComboBox(
            combo_wrap,
            values=[""],
            variable=self.source_filter_var,
            width=190,
            height=30,
            corner_radius=5,
            border_width=0,
            fg_color=CARD,
            button_color=CARD,
            button_hover_color=HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_text_color=TEXT,
            dropdown_hover_color=HOVER,
            font=ctk.CTkFont(size=13),
            state="readonly",
            command=lambda _value: self._on_source_filter_changed(),
        )
        self.source_filter.pack(side="left", padx=1, pady=1)
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
        inner.grid_rowconfigure(0, weight=1)
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

        # Календарь: горизонтальный скролл общий, вертикальный — только у слотов.
        # Даты живут вне вертикального canvas → не моргают и не пропадают.
        self.cal_shell = tk.Frame(inner, bg=CARD, highlightthickness=0)
        self.cal_shell.grid_columnconfigure(0, weight=1)
        self.cal_shell.grid_rowconfigure(1, weight=1)

        self.cal_header_title = tk.Label(
            self.cal_shell,
            text="",
            bg=CARD,
            fg=TEXT,
            font=_ui_font(13, bold=True),
            anchor="w",
        )
        self.cal_header_title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 2))

        self.cal_x_canvas = tk.Canvas(self.cal_shell, bg=CARD, highlightthickness=0, bd=0)
        self.cal_hsb = ttk.Scrollbar(self.cal_shell, orient="horizontal", command=self.cal_x_canvas.xview)
        self.cal_x_canvas.configure(xscrollcommand=self.cal_hsb.set)
        self.cal_x_canvas.grid(row=1, column=0, sticky="nsew")
        self.cal_hsb.grid(row=2, column=0, sticky="ew")

        self.cal_x_host = tk.Frame(self.cal_x_canvas, bg=CARD)
        self._cal_x_window = self.cal_x_canvas.create_window((0, 0), window=self.cal_x_host, anchor="nw")
        self.cal_x_host.grid_columnconfigure(0, weight=1)
        self.cal_x_host.grid_rowconfigure(1, weight=1)

        self.cal_header_host = tk.Frame(self.cal_x_host, bg=CARD, highlightthickness=0)
        self.cal_header_host.grid(row=0, column=0, sticky="ew")

        self.cal_canvas = tk.Canvas(self.cal_x_host, bg=CARD, highlightthickness=0, bd=0)
        self.cal_canvas.grid(row=1, column=0, sticky="nsew")
        self.cal_host = tk.Frame(self.cal_canvas, bg=CARD)
        self._cal_window = self.cal_canvas.create_window((0, 0), window=self.cal_host, anchor="nw")

        self.cal_vsb = ttk.Scrollbar(self.cal_shell, orient="vertical", command=self.cal_canvas.yview)
        self.cal_vsb.grid(row=1, column=1, sticky="ns")
        self.cal_canvas.configure(yscrollcommand=self.cal_vsb.set)

        self.cal_x_host.bind("<Configure>", self._on_cal_x_host_configure)
        self.cal_x_canvas.bind("<Configure>", self._on_cal_x_canvas_configure)
        self.cal_host.bind("<Configure>", self._on_cal_host_configure)
        self.cal_canvas.bind("<Configure>", self._on_cal_canvas_configure)
        self.cal_canvas.bind("<Enter>", lambda *_: self.cal_canvas.focus_set())
        for widget in (
            self.cal_shell,
            self.cal_x_canvas,
            self.cal_x_host,
            self.cal_header_host,
            self.cal_canvas,
            self.cal_host,
        ):
            widget.bind("<MouseWheel>", self._on_cal_wheel)
            widget.bind("<Shift-MouseWheel>", self._on_cal_shift_wheel)
        self.bind_all("<MouseWheel>", self._on_cal_wheel)
        self.bind_all("<Shift-MouseWheel>", self._on_cal_shift_wheel)
        self._cal_time_col_w = CAL_TIME_W
        self._cal_date_w = CAL_DATE_W
        self._cal_date_cols = 0
        self._cal_total_w = 0
        self._cal_col_frames: dict[int, list[tk.Frame]] = {}
        self._cal_placed_cells: list[dict] = []
        self._cal_resizing = False
        self._cal_viewport_w = 0
        self._cal_stretch_after_id: str | None = None
        self._cal_scroll_lock = False
        self._root_size: tuple[int, int] | None = None
        # Пересчёт колонок только после отрисовки и после паузы ресайза окна.
        self.bind("<Configure>", self._on_root_configure)

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

        # Обычный блок без скролла; при переполнении подменим на scrollable.
        self.info_body = ctk.CTkFrame(self.info_wrap, fg_color="transparent")
        self.info_body.pack(fill="x", padx=6, pady=(4, 8))
        self.info_body.grid_columnconfigure(0, weight=1)
        self._info_scroll = None
        self._info_max_h = 220
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

    def _on_cal_x_host_configure(self, _event=None) -> None:
        if self._cal_resizing or self._cal_scroll_lock:
            return
        try:
            self.cal_x_canvas.configure(scrollregion=self.cal_x_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_cal_x_canvas_configure(self, event) -> None:
        # Только высота viewport — ширину колонок здесь НЕ трогаем.
        if self._cal_resizing or self._cal_scroll_lock or getattr(self, "_cal_frozen", False):
            return
        try:
            self.cal_x_canvas.itemconfigure(self._cal_x_window, height=max(1, event.height))
        except tk.TclError:
            pass

    def _on_cal_host_configure(self, _event=None) -> None:
        if self._cal_resizing or self._cal_scroll_lock:
            return
        try:
            self.cal_canvas.configure(scrollregion=self.cal_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_cal_canvas_configure(self, event) -> None:
        # Поддерживаем ширину уже посчитанного контента; колонки не пересчитываем.
        if self._cal_resizing or self._cal_scroll_lock or getattr(self, "_cal_frozen", False):
            return
        try:
            req_w = max(getattr(self, "_cal_total_w", 0), event.width, 1)
            self.cal_canvas.itemconfigure(self._cal_window, width=req_w)
            self.cal_canvas.configure(scrollregion=self.cal_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_root_configure(self, event) -> None:
        # Ресайз главного окна → debounce, потом один пересчёт колонок.
        if event.widget is not self:
            return
        size = (int(event.width), int(event.height))
        if self._root_size == size:
            return
        self._root_size = size
        if getattr(self, "_cal_frozen", False) or self._cal_date_cols <= 0:
            return
        self._schedule_calendar_relayout(delay_ms=350)

    def _calendar_viewport_width(self) -> int:
        try:
            return max(1, self.cal_x_canvas.winfo_width())
        except tk.TclError:
            return 1

    def _compute_date_col_width(self) -> int:
        """Ширина колонки даты: не меньше CAL_DATE_W (читаемый текст), лишнее — горизонтальный скролл."""
        cols = max(self._cal_date_cols, 1)
        avail = self._calendar_viewport_width() - self._cal_time_col_w
        if avail < CAL_DATE_W:
            return CAL_DATE_W
        # Растягиваем только если колонок мало и места хватает; иначе фиксированный минимум + скролл.
        fitted = avail // cols
        if fitted >= CAL_DATE_W:
            return min(fitted, 220)
        return CAL_DATE_W

    def _schedule_calendar_relayout(self, *, delay_ms: int = 350) -> None:
        """Отложенный пересчёт колонок (после паузы ресайза)."""
        if getattr(self, "_cal_frozen", False):
            return
        if self._cal_stretch_after_id is not None:
            try:
                self.after_cancel(self._cal_stretch_after_id)
            except Exception:
                pass
        self._cal_stretch_after_id = self.after(delay_ms, self._layout_calendar_columns)

    def _layout_calendar_columns(self) -> None:
        """Единственная точка пересчёта ширины колонок."""
        self._cal_stretch_after_id = None
        if getattr(self, "_cal_frozen", False):
            return
        if self._cal_date_cols <= 0 or not self._cal_placed_cells:
            self._sync_calendar_widths()
            self._fit_calendar_height()
            return
        view_w = self._calendar_viewport_width()
        date_w = self._compute_date_col_width()
        self._cal_viewport_w = view_w
        if abs(date_w - self._cal_date_w) >= 2 or self._cal_total_w < view_w:
            self._apply_calendar_column_widths(date_w)
        else:
            self._sync_calendar_widths()
        self._fit_calendar_height()

    def _cal_col_x(self, col: int) -> int:
        if col <= 0:
            return 0
        return self._cal_time_col_w + (col - 1) * self._cal_date_w

    def _cal_col_width(self, col: int) -> int:
        return self._cal_time_col_w if col == 0 else self._cal_date_w

    def _apply_calendar_column_widths(self, date_w: int) -> None:
        self._cal_resizing = True
        try:
            time_w = self._cal_time_col_w
            self._cal_date_w = date_w
            cols = max(self._cal_date_cols, 1)
            self._cal_total_w = time_w + date_w * cols
            rows = max((item["row"] for item in self._cal_placed_cells if not item["header"]), default=-1) + 1
            try:
                self.cal_header_host.configure(width=self._cal_total_w, height=CAL_HEADER_H)
                self.cal_header_host.grid_propagate(False)
                self.cal_host.configure(
                    width=self._cal_total_w,
                    height=max(CAL_ROW_H, rows * CAL_ROW_H),
                )
            except tk.TclError:
                pass
            gap = CAL_GAP
            for item in self._cal_placed_cells:
                col_w = self._cal_col_width(item["col"])
                row_h = item["row_h"]
                try:
                    item["frame"].place(
                        x=self._cal_col_x(item["col"]) + gap,
                        y=item["row"] * row_h + gap,
                        width=max(8, col_w - 2 * gap),
                        height=max(8, row_h - 2 * gap),
                    )
                    for child in item["frame"].winfo_children():
                        if isinstance(child, tk.Label):
                            child.configure(wraplength=max(40, col_w - 12))
                except tk.TclError:
                    pass
            self._sync_calendar_widths()
        finally:
            self._cal_resizing = False

    def _sync_calendar_widths(self) -> None:
        view_w = self._calendar_viewport_width()
        req_w = max(
            getattr(self, "_cal_total_w", 0),
            view_w,
            1,
        )
        try:
            self.cal_canvas.itemconfigure(self._cal_window, width=req_w)
            self.cal_x_canvas.itemconfigure(self._cal_x_window, width=req_w)
            self.cal_x_canvas.configure(scrollregion=self.cal_x_canvas.bbox("all"))
            self.cal_canvas.configure(scrollregion=self.cal_canvas.bbox("all"))
            self._sync_calendar_scrollbars()
        except tk.TclError:
            pass

    def _sync_calendar_scrollbars(self) -> None:
        """Показывать полосы только когда контент реально не влезает — иначе двойные «пустые» полосы."""
        try:
            view_w = max(1, self.cal_x_canvas.winfo_width())
            view_h = max(1, self.cal_canvas.winfo_height())
            need_x = getattr(self, "_cal_total_w", 0) > view_w + 2
            body_h = max(self.cal_host.winfo_reqheight(), 1)
            need_y = body_h > view_h + 2
            if need_x:
                self.cal_hsb.grid(row=2, column=0, sticky="ew")
            else:
                self.cal_hsb.grid_remove()
                self.cal_x_canvas.xview_moveto(0)
            if need_y:
                self.cal_vsb.grid(row=1, column=1, sticky="ns")
            else:
                self.cal_vsb.grid_remove()
                self.cal_canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def _fit_calendar_height(self) -> None:
        # Только высота контента — не растягивать слоты на весь canvas.
        try:
            req_h = max(self.cal_host.winfo_reqheight(), 1)
            self.cal_canvas.itemconfigure(self._cal_window, height=req_h)
            self.cal_canvas.configure(scrollregion=self.cal_canvas.bbox("all"))
            self._sync_calendar_scrollbars()
        except tk.TclError:
            pass

    def _fit_calendar_to_canvas(self, width: int | None = None, height: int | None = None) -> None:
        self._layout_calendar_columns()
    def _on_cal_wheel(self, event) -> None:
        if not self._pointer_over_calendar(event):
            return
        steps = self._wheel_steps(event)
        if not steps:
            return "break"
        self._cal_scroll_lock = True
        try:
            # Shift или горизонтальный жест → двигаем даты+слоты вместе.
            if getattr(event, "state", 0) & 0x0001:
                self.cal_x_canvas.xview_scroll(steps, "units")
            else:
                self.cal_canvas.yview_scroll(steps, "units")
        finally:
            self.after(80, self._unlock_cal_scroll)
        return "break"

    def _on_cal_shift_wheel(self, event) -> None:
        if not self._pointer_over_calendar(event):
            return
        steps = self._wheel_steps(event)
        if not steps:
            return "break"
        self._cal_scroll_lock = True
        try:
            self.cal_x_canvas.xview_scroll(steps, "units")
        finally:
            self.after(80, self._unlock_cal_scroll)
        return "break"

    def _unlock_cal_scroll(self) -> None:
        self._cal_scroll_lock = False

    def _pointer_over_calendar(self, event) -> bool:
        widget = getattr(self, "cal_shell", None)
        if widget is None or not widget.winfo_ismapped():
            return False
        try:
            x, y = widget.winfo_rootx(), widget.winfo_rooty()
            w, h = widget.winfo_width(), widget.winfo_height()
        except tk.TclError:
            return False
        return x <= event.x_root <= x + w and y <= event.y_root <= y + h

    def _wheel_steps(self, event) -> int:
        if sys.platform == "darwin":
            return int(-event.delta)
        return int(-event.delta / 120) if event.delta else 0

    def _refresh_cal_scroll(self) -> None:
        try:
            self.cal_host.update_idletasks()
            self.cal_header_host.update_idletasks()
            self.cal_x_host.update_idletasks()
            x_h = max(1, self.cal_x_canvas.winfo_height())
            self.cal_x_canvas.itemconfigure(self._cal_x_window, height=x_h)
            self.cal_canvas.yview_moveto(0)
            self.cal_x_canvas.xview_moveto(0)
            self._cal_viewport_w = 0
            # Один пересчёт сразу после отрисовки данных — не через Configure.
            self._layout_calendar_columns()
        except tk.TclError:
            pass

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
            self._schedule_render(immediate=True)
            return
        # Всегда перечитываем выбранную таблицу — иначе в справке может остаться чужое.
        if self.client:
            self.reload_all()
    def _set_account_label(self, email: str = "") -> None:
        text = (email or "").strip()
        if not hasattr(self, "account_label"):
            return
        if not text:
            self.account_label.configure(text="Нет ключа доступа", text_color=MUTED)
        else:
            self.account_label.configure(text=f"Сервис: {text}", text_color=GREEN)

    def _resolve_credentials_path(self) -> Path | None:
        found = find_credentials_file(self.config_data.credentials)
        if found is not None:
            if found != self.config_data.credentials:
                self.config_data.credentials = found
                try:
                    save_config(self.config_data)
                except Exception:
                    pass
            return found
        return None

    def _try_connect(self, prompt_key: bool = True) -> None:
        path = self._resolve_credentials_path()
        if path is None:
            expected = ROOT / "credentials.json"
            self.client = None
            self._set_account_label("")
            self._set_status(f"Нет credentials.json. Нужен файл: {expected}")
            if prompt_key:
                self.after(200, self._ask_for_credentials)
            return
        kind = credential_kind(path)
        if kind != "service_account":
            self.client = None
            self._set_account_label("")
            self._set_status("Нужен JSON сервисного аккаунта (type: service_account).")
            if prompt_key:
                messagebox.showwarning(
                    "Неверный credentials.json",
                    f"Файл найден:\n{path}\n\n"
                    "Нужен JSON сервисного аккаунта из Google Cloud "
                    "(поле \"type\": \"service_account\" и client_email).\n\n"
                    "Каждую таблицу откройте для этого email с ролью «Редактор».",
                )
                self.after(200, self._ask_for_credentials)
            return
        if self._connecting:
            return
        self._connecting = True
        self._set_status("Подключаюсь…")

        def work():
            return SheetsClient(path)

        def done(client) -> None:
            self.client = client
            email = client.service_email
            self._set_account_label(email)
            self._set_status(f"Доступ: {email}")
            self._schedule_auto_refresh()
            self._sync_registry_async(then_reload=True)

        def finish(client=None, error: BaseException | None = None) -> None:
            self._connecting = False
            self._jobs = max(0, self._jobs - 1)
            if self._jobs == 0:
                self.refresh_btn.configure(text="Обновить", state="normal")
                self._cal_frozen = False
            if error is not None:
                self.client = None
                self._set_account_label("")
                self._set_status(str(error).split("\n")[0])
                if isinstance(error, SheetsError):
                    messagebox.showwarning("Нет доступа к Google", str(error))
                else:
                    messagebox.showerror("Ошибка", str(error))
                return
            done(client)

        # Не блокируем UI сетью/SSL — иначе Windows пишет «Не отвечает».
        self._jobs += 1
        self.refresh_btn.configure(text="Читаю…", state="disabled")

        def runner() -> None:
            try:
                client = work()
            except Exception as exc:
                self.after(0, lambda e=exc: finish(error=e))
                return
            self.after(0, lambda c=client: finish(client=c))

        threading.Thread(target=runner, daemon=True).start()

    def _ask_for_credentials(self) -> None:
        expected = ROOT / "credentials.json"
        go = messagebox.askokcancel(
            "Нужен ключ сервисного аккаунта",
            "Не найден JSON сервисного аккаунта.\n\n"
            f"Положите его сюда:\n{expected}\n\n"
            "Имя файла лучше: credentials.json\n\n"
            "Сейчас можно выбрать файл вручную.",
        )
        if not go:
            return
        if self._pick_credentials_file():
            save_config(self.config_data)
            self._try_connect(prompt_key=False)

    def _pick_credentials_file(self, parent=None) -> bool:
        chosen = filedialog.askopenfilename(
            parent=parent or self,
            title="JSON сервисного аккаунта",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if not chosen:
            return False
        try:
            installed = install_credentials(Path(chosen))
        except Exception as exc:
            messagebox.showerror("Неверный ключ", str(exc), parent=parent or self)
            return False
        if credential_kind(installed) != "service_account":
            messagebox.showerror(
                "Неверный ключ",
                "Нужен JSON сервисного аккаунта (type: service_account),\n"
                "а не OAuth Desktop client.",
                parent=parent or self,
            )
            return False
        self.config_data.credentials = installed
        return True

    def _run_bg(self, work: Callable, done: Callable, *, alert: bool = True) -> None:
        self._jobs += 1
        # Текст той же длины — иначе кнопка расширяется и ломает сетку календаря.
        self.refresh_btn.configure(text="Читаю…", state="disabled")

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
            self.refresh_btn.configure(text="Обновить", state="normal")
            self._cal_frozen = False
        if error:
            self._cal_frozen = False
            self._set_status(f"Ошибка: {error}")
            if alert:
                messagebox.showerror("Ошибка", str(error))
            return
        done(result)

    def _schedule_auto_refresh(self) -> None:
        if self._auto_refresh_after_id is not None:
            try:
                self.after_cancel(self._auto_refresh_after_id)
            except Exception:
                pass
        self._auto_refresh_after_id = self.after(AUTO_REFRESH_MS, self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        self._auto_refresh_after_id = None
        try:
            if (
                self.client
                and self._jobs == 0
                and self._booking_open == 0
                and not self._lock_busy
                and not self._connecting
                and not self._tables_dialog_open
                and not self._registry_syncing
            ):
                self._sync_registry_async(then_reload=True)
        finally:
            self._schedule_auto_refresh()

    def _registry_configured(self) -> bool:
        text = (self.config_data.registry_spreadsheet_id or "").strip()
        return bool(text) and not text.upper().startswith("PASTE_")

    def _apply_remote_tables(self, refs: list[SheetRef]) -> bool:
        """Подставляет список таблиц из облака. True если что-то изменилось."""
        local = merge_tables(self.config_data.sources, self.config_data.destinations)
        if tables_signature(local) == tables_signature(refs):
            return False
        self.config_data.sources = list(refs)
        self.config_data.destinations = list(refs)
        try:
            save_config(self.config_data)
        except Exception:
            pass
        self._sync_source_filter_from_config()
        return True

    def _sync_registry_work(self) -> tuple[str, object]:
        """Облако — единственный источник списка таблиц для всех ПК."""
        if not self.client or not self._registry_configured():
            return ("skip", None)
        registry_id = self.config_data.registry_spreadsheet_id
        sheet = self.config_data.registry_sheet or DEFAULT_REGISTRY_SHEET
        remote = self.client.pull_table_registry(registry_id, sheet)
        local = usable_refs(merge_tables(self.config_data.sources, self.config_data.destinations))
        # Пустой облачный список + есть локальный кэш → один раз заливаем в облако.
        if not remote and local:
            self.client.push_table_registry(registry_id, local, sheet)
            return ("seeded", len(local))
        # Облако главное: всегда подтягиваем, даже если локально что-то другое.
        if remote:
            if tables_signature(remote) != tables_signature(local):
                return ("pull", remote)
            return ("ok", len(remote))
        return ("ok", 0)

    def _sync_registry_async(self, *, then_reload: bool = False) -> None:
        if not self.client:
            if then_reload:
                self._set_status("Укажите таблицы в «Таблицы».")
            return
        if not self._registry_configured():
            if then_reload:
                self._set_status(
                    "Нужна служебная Google-таблица для общего списка. Откройте «Таблицы»."
                )
            return
        if self._registry_syncing or self._tables_dialog_open or self._booking_open:
            if then_reload and self._valid_sources() and self._jobs == 0:
                self.reload_all()
            return
        self._registry_syncing = True

        def work_safe():
            try:
                return self._sync_registry_work()
            except Exception as exc:
                return ("err", exc)

        def done_safe(payload) -> None:
            self._registry_syncing = False
            action, data = payload
            if action == "err":
                self._set_status(f"Синхронизация таблиц: {str(data).split(chr(10))[0]}")
                if then_reload and self._valid_sources():
                    self.reload_all()
                return
            if action == "pull" and isinstance(data, list):
                changed = self._apply_remote_tables(data)
                self._set_status(f"Список таблиц обновлён из облака: {len(data)}")
                if then_reload or changed:
                    if self._valid_sources():
                        self.reload_all()
                    else:
                        self._set_status("В общем списке нет таблиц.")
                return
            if action == "seeded":
                self._set_status(f"Общий список таблиц создан в облаке ({data})")
            if then_reload:
                if self._valid_sources():
                    self.reload_all()
                else:
                    self._set_status("Укажите таблицы в «Таблицы».")

        self._run_bg(work_safe, done_safe, alert=False)

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
        # Мягкое обновление: не ломаем уже нарисованную сетку, пока грузятся данные.
        quiet = bool(self._slot_labels)
        if not quiet:
            self._cal_frozen = True
            self._set_status(f"Читаю «{selected_name}»…")
        else:
            self._set_status(f"Обновляю «{selected_name}»…")

        def work():
            return self.client.fetch_all(sources)

        def done(result):
            self._cal_frozen = False
            records, errors = result
            self._last_error_message = "\n\n".join(errors[:8]) if errors else ""
            booking = [item for item in records if item.kind != KIND_INFO]
            self.info_records = self._dedupe_info([item for item in records if item.kind == KIND_INFO])
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
            # Не сбрасываем _calendar_fp — иначе каждые 30 с сетка мигает и пересобирается.
            extra = f" · {len(errors)} ошибок" if errors else ""
            if getattr(self.client, "read_only_public", False):
                extra += " · чтение CSV"
            booked = sum(1 for item in self.records if item.layout == "calendar" and item.values.get("Статус") == "Занято")
            slots = sum(1 for item in self.records if item.layout == "calendar")
            info_note = f" · справка: {len(self.info_records)}" if self.info_records else ""
            if slots:
                self._status_after_draw = f"{selected_name}: {slots} слотов, занято {booked}{info_note}{extra}"
            else:
                split_note = ""
                if len(self.records) > raw_count:
                    split_note = f" · разбито на {len(self.records)} пунктов"
                self._status_after_draw = (
                    f"{selected_name}: строк {raw_count}{split_note}{info_note}{extra}"
                )
            self._schedule_render(immediate=True)
            if errors:
                messagebox.showwarning("Таблица не загрузилась", "\n\n".join(errors[:8]))

        self._run_bg(work, done)

    def _status_value(self, record: Record) -> str:
        for key, value in record.values.items():
            if _is_status_col(key):
                return value
        return ""

    def _on_search_changed(self) -> None:
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(220, self._apply_search)

    def _apply_search(self) -> None:
        self._search_after_id = None
        calendar_visible = bool(getattr(self, "cal_shell", None) and self.cal_shell.winfo_ismapped())
        if calendar_visible and self._slot_labels:
            # Только подсветка слотов — без перерисовки сетки и справки (иначе ломается стиль).
            self._apply_calendar_search_styles()
            return
        self._schedule_render(immediate=True)

    def _schedule_render(self, *, immediate: bool = False) -> None:
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except Exception:
                pass
            self._render_after_id = None
        if immediate:
            self._render_views()
            return
        self._render_after_id = self.after(50, self._render_views)

    def _source_records(self) -> list[Record]:
        source_name = self.source_filter_var.get().strip()
        if not source_name:
            return list(self.records)
        return [record for record in self.records if record.source_name == source_name]

    def _filtered_records(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        out = []
        for record in self._source_records():
            # Календарь оставляем целиком — поиск только подсвечивает ячейки.
            if record.layout == "calendar":
                out.append(record)
                continue
            blob = " ".join([record.source_name, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            out.append(record)
        return out

    def _calendar_fingerprint(self, records: list[Record]) -> tuple:
        return (
            tuple(sorted({(r.spreadsheet_id, r.sheet, r.source_name) for r in records})),
            len(records),
            tuple(
                sorted(
                    (
                        r.values.get("Дата", ""),
                        r.values.get("Время", ""),
                        r.values.get("Клиент", ""),
                        r.values.get("Статус", ""),
                    )
                    for r in records
                )
            ),
        )

    def _calendar_structure_fp(self, records: list[Record]) -> tuple:
        """Каркас сетки (даты/время) — без текста слотов."""
        if not records:
            return ()
        dates = tuple(
            dict.fromkeys(item.values.get("Дата", "") for item in records if item.values.get("Дата"))
        )
        times = list(
            dict.fromkeys(item.values.get("Время", "") for item in records if item.values.get("Время"))
        )
        times.sort(key=_time_sort_key)
        sample = records[0]
        return (
            sample.spreadsheet_id,
            sample.sheet,
            sample.source_name,
            dates,
            tuple(times),
        )

    def _slot_visual(self, record: Record) -> tuple[str, str, str, bool]:
        status = record.values.get("Статус", "")
        if status == "Не записывать":
            return "не записывать", SLOT_BLOCKED, MUTED, False
        if status == "Записывают":
            return "записывают…", SLOT_LOCK, "#3e2723", True
        if status == "Занято":
            name = record.values.get("Клиент", "").strip() or "занято"
            phone = record.values.get("Телефон", "").strip()
            text = f"{name}\n{phone}" if phone and phone not in name else name
            return text, SLOT_BOOKED, "#ffffff", True
        return "запись", SLOT_GREEN, "#102910", False

    def _patch_calendar_slots(self, records: list[Record]) -> bool:
        """Обновить текст/цвет слотов на месте, без уничтожения сетки."""
        if not self._slot_labels:
            return False
        cell_map = {
            (item.values.get("Время", ""), item.values.get("Дата", "")): item for item in records
        }
        if set(cell_map.keys()) != set(self._slot_labels.keys()):
            return False
        for key, record in cell_map.items():
            label = self._slot_labels.get(key)
            if label is None:
                return False
            text, bg, fg, bold = self._slot_visual(record)
            try:
                label._record = record
                label.configure(text=text, font=_ui_font(11, bold=bold), bg=bg, fg=fg)
                cell = getattr(label, "_cell", None) or label.master
                cell.configure(bg=bg)
            except tk.TclError:
                return False
        self._apply_calendar_search_styles()
        return True

    def _on_slot_click(self, event) -> None:
        record = getattr(event.widget, "_record", None)
        if record is not None:
            self._edit_calendar_cell(record)

    def _apply_calendar_search_styles(self) -> None:
        query = self.search_var.get().strip().lower()
        for (time, date), label in list(self._slot_labels.items()):
            record = getattr(label, "_record", None)
            if record is None:
                continue
            status = record.values.get("Статус", "")
            blob = " ".join(
                [
                    record.values.get("Клиент", ""),
                    record.values.get("Телефон", ""),
                    date,
                    time,
                ]
            ).lower()
            if status == "Не записывать":
                bg, fg = SLOT_BLOCKED, MUTED
            elif status == "Записывают":
                bg, fg = SLOT_LOCK, "#3e2723"
            elif status == "Занято":
                if query and query in blob:
                    bg, fg = "#9ccc65", TEXT
                elif query:
                    bg, fg = "#dce8d4", MUTED
                else:
                    bg, fg = SLOT_BOOKED, "#ffffff"
            else:
                bg, fg = (("#e8f0e0", MUTED) if query else (SLOT_GREEN, "#102910"))
            try:
                label.configure(bg=bg, fg=fg)
                cell = getattr(label, "_cell", None) or label.master
                cell.configure(bg=bg)
            except tk.TclError:
                pass

    def _refresh_slot_label(self, record: Record) -> bool:
        key = (record.values.get("Время", ""), record.values.get("Дата", ""))
        label = self._slot_labels.get(key)
        if label is None:
            return False
        text, bg, fg, bold = self._slot_visual(record)
        try:
            label._record = record
            label.configure(text=text, font=_ui_font(11, bold=bold), bg=bg, fg=fg)
            cell = getattr(label, "_cell", None) or label.master
            cell.configure(bg=bg)
        except tk.TclError:
            return False
        calendar = [item for item in self._source_records() if item.layout == "calendar"]
        self._calendar_fp = self._calendar_fingerprint(calendar)
        self._calendar_struct_fp = self._calendar_structure_fp(calendar)
        self._apply_calendar_search_styles()
        return True
    def _columns(self, records: list[Record]) -> list[str]:
        seen: list[str] = []
        for record in records:
            for key in record.values:
                if key not in seen and key not in HIDDEN:
                    seen.append(key)
        return ["Источник", *seen]

    def _render_table(self) -> None:
        if self._rendering:
            return
        self._rendering = True
        try:
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
                fp = self._calendar_fingerprint(calendar)
                struct = self._calendar_structure_fp(calendar)
                if fp == self._calendar_fp:
                    self._apply_calendar_search_styles()
                    pending = getattr(self, "_status_after_draw", None)
                    if pending:
                        self._set_status(pending)
                        self._status_after_draw = None
                elif (
                    struct == self._calendar_struct_fp
                    and self._slot_labels
                    and self._patch_calendar_slots(calendar)
                ):
                    self._calendar_fp = fp
                    pending = getattr(self, "_status_after_draw", None)
                    if pending:
                        self._set_status(pending)
                        self._status_after_draw = None
                else:
                    self._draw_calendars(calendar)
                    self._calendar_fp = fp
                    self._calendar_struct_fp = struct
                booked = sum(1 for item in calendar if item.values.get("Статус") == "Занято")
                self._visible = calendar
                self.count_label.configure(text=str(booked))
                return

            self._show_calendar(False)
            self._calendar_fp = None
            self._calendar_struct_fp = None
            self._slot_labels = {}
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
            pending = getattr(self, "_status_after_draw", None)
            if pending:
                self._set_status(pending)
                self._status_after_draw = None
            self.after_idle(self._fit_columns)
        finally:
            self._rendering = False

    def _show_empty_state(self, title: str, message: str, *, can_retry: bool) -> None:
        self.tree.grid_remove()
        self.vsb.grid_remove()
        self.cal_shell.grid_remove()
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
            self.cal_shell.grid(row=0, column=0, rowspan=3, sticky="nsew")
        else:
            self.cal_shell.grid_remove()
            self.tree.grid(row=0, column=0, rowspan=3, sticky="nsew")
            self.vsb.grid(row=0, column=1, rowspan=3, sticky="ns")

    def _draw_calendars(self, records: list[Record]) -> None:
        if self._cal_draw_after_id is not None:
            try:
                self.after_cancel(self._cal_draw_after_id)
            except Exception:
                pass
            self._cal_draw_after_id = None
        self._cal_draw_gen += 1
        self._slot_labels = {}
        self._cal_col_frames = {}
        self._cal_placed_cells = []
        self._calendar_struct_fp = None
        self._cal_resizing = True
        try:
            for child in self.cal_host.winfo_children():
                child.destroy()
            for child in self.cal_header_host.winfo_children():
                child.destroy()
            groups: dict[tuple[str, str, str], list[Record]] = {}
            for record in records:
                groups.setdefault((record.spreadsheet_id, record.sheet, record.source_name), []).append(record)
            if not groups:
                self.cal_header_title.configure(text="")
                self._cal_resizing = False
                return
            ((_, _, name), items) = next(iter(groups.items()))
            self._draw_one_calendar(name, items)
            try:
                self.cal_canvas.grid(row=1, column=0, sticky="nsew")
                self.cal_x_canvas.grid(row=1, column=0, sticky="nsew")
            except tk.TclError:
                pass
        except Exception:
            self._cal_resizing = False
            raise

    def _finish_calendar_draw(self) -> None:
        self._cal_draw_after_id = None
        self._cal_resizing = False
        calendar = [item for item in self._source_records() if item.layout == "calendar"]
        if calendar:
            self._calendar_fp = self._calendar_fingerprint(calendar)
            self._calendar_struct_fp = self._calendar_structure_fp(calendar)
        self._refresh_cal_scroll()
        self._apply_calendar_search_styles()
        pending = getattr(self, "_status_after_draw", None)
        if pending:
            self._set_status(pending)
            self._status_after_draw = None

    def _draw_one_calendar(self, name: str, items: list[Record]) -> None:
        # Сетка и цвета — только UI приложения. Из таблицы берём дату/время/текст слота.
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
        header.configure(bg=SLOT_OUTLINE)
        body.configure(bg=SLOT_OUTLINE)
        time_w = self._cal_time_col_w
        date_w = self._compute_date_col_width() if self._calendar_viewport_width() > 40 else self._cal_date_w
        self._cal_date_w = date_w
        self._cal_total_w = time_w + date_w * max(len(dates), 1)

        try:
            header.configure(width=self._cal_total_w, height=CAL_HEADER_H)
            header.grid_propagate(False)
            body.configure(width=self._cal_total_w, height=max(CAL_ROW_H, len(times) * CAL_ROW_H))
        except tk.TclError:
            pass

        self._cal_header_cell(
            header,
            0,
            0,
            time_w,
            text="Время",
            bg=GREEN,
            fg="#ffffff",
            font=_ui_font(10, bold=True),
        )
        for col, date in enumerate(dates, start=1):
            weekend = any(part in date.lower() for part in ("сб", "вс"))
            self._cal_header_cell(
                header,
                0,
                col,
                date_w,
                text=date,
                bg=GREEN,
                fg="#fce8e6" if weekend else "#ffffff",
                font=_ui_font(10, bold=True),
                wraplength=max(40, date_w - 12),
            )

        # Тело рисуем порциями — иначе Windows помечает окно «Не отвечает».
        gen = self._cal_draw_gen
        self._cal_draw_state = {
            "gen": gen,
            "body": body,
            "times": times,
            "dates": dates,
            "cell_map": cell_map,
            "date_w": date_w,
            "time_w": time_w,
            "row_idx": 0,
        }
        self._set_status(f"Рисую календарь… 0/{len(times)}")
        self._cal_draw_after_id = self.after(1, self._draw_calendar_chunk)

    def _draw_calendar_chunk(self) -> None:
        state = getattr(self, "_cal_draw_state", None)
        if not state or state.get("gen") != self._cal_draw_gen:
            self._cal_draw_after_id = None
            return
        body = state["body"]
        times: list = state["times"]
        dates: list = state["dates"]
        cell_map = state["cell_map"]
        date_w = state["date_w"]
        time_w = state["time_w"]
        row_idx = state["row_idx"]
        batch = 2
        end = min(row_idx + batch, len(times))
        for row in range(row_idx, end):
            time = times[row]
            self._cal_body_cell(
                body,
                row,
                0,
                time_w,
                text=_short_time(time),
                bg=SLOT_TIME,
                fg=TEXT,
                font=_ui_font(12, bold=True),
                anchor="center",
                justify="center",
            )
            for col, date in enumerate(dates, start=1):
                record = cell_map.get((time, date))
                self._draw_slot(body, record, row, col, date_w)
        state["row_idx"] = end
        if end < len(times):
            self._set_status(f"Рисую календарь… {end}/{len(times)}")
            self._cal_draw_after_id = self.after(1, self._draw_calendar_chunk)
            return
        self._cal_draw_after_id = None
        self.after_idle(self._finish_calendar_draw)

    def _register_cal_cell(self, col: int, cell: tk.Frame, *, row: int, row_h: int, header: bool) -> None:
        self._cal_col_frames.setdefault(col, []).append(cell)
        self._cal_placed_cells.append(
            {"frame": cell, "row": row, "col": col, "row_h": row_h, "header": header}
        )

    def _cal_header_cell(self, parent: tk.Frame, row: int, col: int, width: int, **kwargs) -> tk.Label:
        gap = CAL_GAP
        cell = tk.Frame(
            parent,
            bg=kwargs.get("bg", GREEN),
            highlightthickness=0,
            bd=0,
        )
        cell.place(
            x=self._cal_col_x(col) + gap,
            y=row * CAL_HEADER_H + gap,
            width=max(8, width - 2 * gap),
            height=max(8, CAL_HEADER_H - 2 * gap),
        )
        self._register_cal_cell(col, cell, row=row, row_h=CAL_HEADER_H, header=True)
        label = tk.Label(cell, padx=4, pady=6, borderwidth=0, highlightthickness=0, **kwargs)
        label.pack(fill="both", expand=True)
        return label

    def _cal_body_cell(self, parent: tk.Frame, row: int, col: int, width: int, **kwargs) -> tk.Label:
        gap = CAL_GAP
        cell = tk.Frame(
            parent,
            bg=kwargs.get("bg", CARD),
            highlightthickness=0,
            bd=0,
        )
        cell.place(
            x=self._cal_col_x(col) + gap,
            y=row * CAL_ROW_H + gap,
            width=max(8, width - 2 * gap),
            height=max(8, CAL_ROW_H - 2 * gap),
        )
        self._register_cal_cell(col, cell, row=row, row_h=CAL_ROW_H, header=False)
        label = tk.Label(cell, padx=6, pady=4, borderwidth=0, highlightthickness=0, **kwargs)
        label.pack(fill="both", expand=True)
        return label

    def _draw_slot(self, parent: tk.Frame, record: Record | None, row: int, col: int, width: int) -> None:
        # Внешний вид слота всегда из палитры приложения; лист даёт только текст/статус.
        if record is None:
            self._cal_body_cell(parent, row, col, width, text="", bg=SLOT_BLOCKED, fg=MUTED)
            return
        text, bg, fg, bold = self._slot_visual(record)
        label = self._cal_body_cell(
            parent,
            row,
            col,
            width,
            text=text,
            bg=bg,
            fg=fg,
            font=_ui_font(11, bold=bold),
            wraplength=max(40, width - 14),
            justify="left",
            anchor="nw",
        )
        label._record = record
        label._cell = label.master
        self._slot_labels[(record.values.get("Время", ""), record.values.get("Дата", ""))] = label
        label.bind("<Button-1>", self._on_slot_click)

    def _edit_calendar_cell(self, record: Record) -> None:
        self._picked = record
        if record.values.get("Статус") == "Не записывать":
            messagebox.showinfo("Слот закрыт", "В эту ячейку нельзя записывать.")
            return
        if not self.client:
            messagebox.showerror("Нет подключения", "Сначала подключите ключ в «Таблицы».")
            return
        if self._lock_busy or self._booking_open:
            self._set_status("Сначала закройте текущее окно записи.")
            return
        self._lock_busy = True
        self._set_status("Закрепляю слот…")

        def work():
            try:
                return ("ok", self.client.acquire_calendar_lock(record))
            except Exception as exc:
                return ("err", exc)

        def done(payload) -> None:
            self._lock_busy = False
            kind, data = payload
            if kind == "err":
                self._set_status(str(data).split("\n")[0])
                messagebox.showwarning("Слот занят", str(data))
                self.reload_all()
                return
            previous, lock_text = data
            record.values["Статус"] = "Записывают"
            record.values["Клиент"] = ""
            record.values["Телефон"] = ""
            self._refresh_slot_label(record)
            self._set_status("Слот закреплён — введите данные")
            self._edit_cell(
                record,
                "Клиент",
                sheet_previous=previous,
                lock_text=lock_text,
            )

        self._run_bg(work, done, alert=False)

    def _render_views(self) -> None:
        self._render_table()
        # Справку после календаря — иначе update_idletasks в info ломает сетку на «Обновить».
        self.after_idle(self._render_info_if_changed)

    def _info_fingerprint(self) -> tuple:
        items = self._filtered_info()
        return tuple(
            (
                item.spreadsheet_id,
                item.sheet,
                item.row,
                str(item.values.get("Текст") or ""),
                str(item.values.get("_tone") or ""),
            )
            for item in items
        )

    def _info_structure_fp(self, records: list[Record] | None = None) -> tuple:
        items = records if records is not None else self._filtered_info()
        return tuple((item.spreadsheet_id, item.sheet, item.row) for item in items)

    def _info_has_cards(self) -> bool:
        return bool(self._info_cards)

    def _info_card_payloads(self, records: list[Record]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for record in records:
            text = str(record.values.get("Текст") or "").strip()
            if not text:
                text = "\n".join(
                    str(value).strip()
                    for key, value in record.values.items()
                    if str(value).strip() and key not in HIDDEN
                )
            if not text:
                continue
            tone = str(record.values.get("_tone") or info_tone(text) or "info")
            out.append((text, tone))
        return out

    def _patch_info_cards(self, payloads: list[tuple[str, str]]) -> bool:
        if not self._info_cards or len(self._info_cards) != len(payloads):
            return False
        wrap = max(480, self.winfo_width() - 80) if self.winfo_width() > 100 else 980
        for card, (text, tone) in zip(self._info_cards, payloads):
            bg, fg = INFO_TONES.get(tone, INFO_TONES["info"])
            try:
                card.configure(
                    text=text,
                    bg=bg,
                    fg=fg,
                    font=_ui_font(12, bold=tone in {"warn", "ok"}),
                    wraplength=wrap,
                )
            except tk.TclError:
                return False
        return True

    def _render_info_if_changed(self) -> None:
        records = self._filtered_info()
        fp = self._info_fingerprint()
        struct = self._info_structure_fp(records)
        if fp == self._info_fp and self._info_has_cards():
            return
        payloads = self._info_card_payloads(records)
        if (
            struct == self._info_struct_fp
            and self._info_has_cards()
            and len(payloads) == len(self._info_cards)
            and self._patch_info_cards(payloads)
        ):
            self._info_fp = fp
            return
        self._info_fp = fp
        self._info_struct_fp = struct
        self._render_info()

    def _active_sheet_key(self) -> tuple[str, str] | None:
        for record in self.records:
            if record.layout == "calendar":
                return record.spreadsheet_id, record.sheet
        if self.records:
            item = self.records[0]
            return item.spreadsheet_id, item.sheet
        return None

    def _selected_spreadsheet_id(self) -> str:
        sources = self._selected_sources()
        if not sources:
            return ""
        try:
            return sources[0].normalized_id()
        except ValueError:
            return ""

    def _dedupe_info(self, records: list[Record]) -> list[Record]:
        seen: set[str] = set()
        out: list[Record] = []
        for record in records:
            text = str(record.values.get("Текст") or "").strip()
            if not text:
                text = "\n".join(
                    str(value).strip()
                    for key, value in record.values.items()
                    if str(value).strip() and key not in HIDDEN and not str(key).startswith("_")
                )
            key = " ".join(text.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(record)
        return out

    def _filtered_info(self) -> list[Record]:
        query = self.search_var.get().strip().lower()
        source_name = self.source_filter_var.get().strip()
        sheet_key = self._active_sheet_key()
        selected_sid = self._selected_spreadsheet_id()
        out: list[Record] = []
        seen: set[str] = set()
        for record in self.info_records:
            # Только выбранная таблица из списка «Таблица».
            if source_name and source_name != "Таблица" and record.source_name != source_name:
                continue
            if selected_sid and record.spreadsheet_id != selected_sid:
                continue
            if sheet_key:
                same_sheet = (record.spreadsheet_id, record.sheet) == sheet_key
                # Соседняя вкладка-справка той же книги (например «УСЛУГИ врача»).
                companion = record.spreadsheet_id == sheet_key[0] and is_info_title(record.sheet)
                if not same_sheet and not companion:
                    continue
            text = str(record.values.get("Текст") or "").strip()
            blob = " ".join([record.source_name, text, *record.values.values()]).lower()
            if query and query not in blob:
                continue
            dedupe_key = " ".join(text.lower().split()) if text else blob
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(record)
        return out

    def _info_host(self) -> ctk.CTkFrame:
        return self._info_scroll if self._info_scroll is not None else self.info_body

    def _reset_info_host(self, *, scrollable: bool) -> ctk.CTkFrame:
        self._info_cards = []
        if self._info_scroll is not None:
            self._info_scroll.destroy()
            self._info_scroll = None
        for child in self.info_body.winfo_children():
            child.destroy()
        if scrollable:
            self.info_body.pack_forget()
            try:
                self._info_scroll = ctk.CTkScrollableFrame(
                    self.info_wrap,
                    fg_color="transparent",
                    height=self._info_max_h,
                    orientation="vertical",
                )
            except TypeError:
                self._info_scroll = ctk.CTkScrollableFrame(
                    self.info_wrap,
                    fg_color="transparent",
                    height=self._info_max_h,
                )
            self._info_scroll.pack(fill="x", padx=6, pady=(4, 8))
            self._info_scroll.grid_columnconfigure(0, weight=1)
            for name in ("_scrollbar_horizontal", "horizontal_scrollbar", "_parent_scrollbar"):
                bar = getattr(self._info_scroll, name, None)
                if bar is not None:
                    try:
                        bar.grid_remove()
                    except Exception:
                        try:
                            bar.pack_forget()
                        except Exception:
                            pass
            return self._info_scroll
        try:
            self.info_body.pack(fill="x", padx=6, pady=(4, 8))
        except tk.TclError:
            pass
        self.info_body.grid_columnconfigure(0, weight=1)
        return self.info_body

    def _render_info(self) -> None:
        records = self._filtered_info()
        if not records:
            self._reset_info_host(scrollable=False)
            self._info_cards = []
            self._info_struct_fp = None
            self.info_wrap.grid_remove()
            return
        self.info_wrap.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))

        # Сначала рисуем без скролла; если не влезает — перерисуем со скроллом.
        host = self._reset_info_host(scrollable=False)
        cards = self._fill_info_cards(host, records)
        if not cards:
            self.info_wrap.grid_remove()
            self._info_cards = []
            return
        self.update_idletasks()
        need_scroll = host.winfo_reqheight() > self._info_max_h
        if need_scroll:
            host = self._reset_info_host(scrollable=True)
            cards = self._fill_info_cards(host, records)
        self._info_cards = cards
        self._info_struct_fp = self._info_structure_fp(records)

    def _fill_info_cards(self, host, records: list[Record]) -> list[tk.Label]:
        cards: list[tk.Label] = []
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
            wrap = max(480, self.winfo_width() - 80) if self.winfo_width() > 100 else 980
            card = tk.Label(
                host,
                text=text,
                bg=bg,
                fg=fg,
                font=_ui_font(12, bold=tone in {"warn", "ok"}),
                wraplength=wrap,
                justify="left",
                anchor="w",
                padx=10,
                pady=6,
            )
            card.grid(row=idx, column=0, sticky="ew", padx=2, pady=2)
            cards.append(card)
        host.grid_columnconfigure(0, weight=1)
        return cards

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
        if record.layout == "calendar" and field == "Клиент":
            self._edit_calendar_cell(record)
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

    def _edit_cell(
        self,
        record: Record,
        field: str,
        *,
        sheet_previous: str | None = None,
        lock_text: str | None = None,
    ) -> None:
        calendar = record.layout == "calendar" and field == "Клиент"
        when = f"{record.values.get('Дата', '')} · {record.values.get('Время', '')}".strip(" ·")
        dialog = self._dialog(
            "Запись" if calendar else "Изменить ячейку",
            "520x420" if calendar else "480x240",
        )
        if calendar and lock_text:
            self._booking_open += 1

        ctk.CTkLabel(
            dialog,
            text=when or f"{record.source_name} · строка {record.row}",
            text_color=MUTED,
        ).pack(padx=16, pady=(16, 4), anchor="w")
        box = _input_box(dialog, "Имя клиента" if calendar else field, "сохранится в ту же ячейку таблицы")
        box.pack(fill="x", padx=16, pady=8)
        entry = _styled_entry(box, "Иванова А. +79001234567" if calendar else "Иванова А.")
        entry.pack(fill="x", padx=10, pady=(4, 10))

        initial = record.values.get(field, "")
        prev_for_form = sheet_previous if sheet_previous is not None else ""
        if calendar:
            prev_status = classify_slot(prev_for_form) if sheet_previous is not None else record.values.get("Статус", "")
            if sheet_previous is not None:
                if prev_status == "Свободно" or is_lock_text(prev_for_form):
                    initial = ""
                elif prev_status == "Не записывать":
                    initial = prev_for_form
                else:
                    name, phone = extract_phone(prev_for_form)
                    if phone and phone not in name:
                        initial = f"{name} {phone}".strip()
                    else:
                        initial = name or prev_for_form
            else:
                phone = str(record.values.get("Телефон") or "").strip()
                name = str(record.values.get("Клиент") or "").strip()
                if record.values.get("Статус") == "Свободно":
                    initial = ""
                elif phone and phone not in name:
                    initial = f"{name} {phone}".strip()
                else:
                    initial = name
        entry.insert(0, initial)
        if calendar and (
            (sheet_previous is not None and classify_slot(prev_for_form) == "Свободно")
            or (sheet_previous is None and record.values.get("Статус") == "Свободно")
        ):
            entry.delete(0, "end")
        entry.focus()

        pregnant_var = tk.StringVar(value="")
        warn_label = None
        if calendar:
            preg_box = ctk.CTkFrame(dialog, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
            preg_box.pack(fill="x", padx=16, pady=(0, 8))
            ctk.CTkLabel(
                preg_box,
                text="Беременность",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=TEXT,
            ).pack(anchor="w", padx=12, pady=(10, 4))
            existing = (prev_for_form or str(record.values.get("Клиент") or "")).lower()
            if "береме" in existing:
                pregnant_var.set("yes")
            radios = ctk.CTkFrame(preg_box, fg_color="transparent")
            radios.pack(fill="x", padx=8, pady=(0, 8))
            ctk.CTkRadioButton(
                radios,
                text="Не беременна",
                variable=pregnant_var,
                value="no",
                text_color=TEXT,
                fg_color=GREEN,
                hover_color=GREEN_HOVER,
            ).pack(side="left", padx=8, pady=4)
            ctk.CTkRadioButton(
                radios,
                text="Беременна",
                variable=pregnant_var,
                value="yes",
                text_color=TEXT,
                fg_color=GREEN,
                hover_color=GREEN_HOVER,
            ).pack(side="left", padx=8, pady=4)

            warn_label = ctk.CTkLabel(
                preg_box,
                text=(
                    "НЕЛЬЗЯ БЕРЕМЕННЫМ:\n"
                    "7425 — ПАП-тест на основе жидкостной цитологии\n"
                    "74040 — Цервикальный скрининг\n"
                    "74042 — Цервикальный минимум\n"
                    "40-534 — Маркеры пролиферации"
                ),
                text_color=DANGER,
                font=ctk.CTkFont(size=12, weight="bold"),
                wraplength=460,
                justify="left",
                anchor="w",
            )

            def sync_warn(*_args) -> None:
                if pregnant_var.get() == "yes":
                    if not warn_label.winfo_ismapped():
                        warn_label.pack(fill="x", padx=12, pady=(0, 12))
                else:
                    try:
                        warn_label.pack_forget()
                    except Exception:
                        pass

            pregnant_var.trace_add("write", sync_warn)
            sync_warn()

        state = {"closed": False, "saved": False}

        def release_booking_counter() -> None:
            if calendar and lock_text and not state["closed"]:
                state["closed"] = True
                self._booking_open = max(0, self._booking_open - 1)

        def restore_previous() -> None:
            if not lock_text or not self.client:
                return
            restore_value = sheet_previous if sheet_previous is not None else ""

            def work():
                try:
                    current = self.client.read_cell(record, "Клиент")
                    if current == lock_text:
                        self.client.update_cell(record, "Клиент", restore_value)
                except Exception:
                    pass
                return True

            def done(_):
                self.reload_all()

            self._run_bg(work, done, alert=False)

        def close_dialog(*, restore: bool) -> None:
            if state["saved"]:
                release_booking_counter()
                try:
                    dialog.destroy()
                except Exception:
                    pass
                return
            release_booking_counter()
            try:
                dialog.destroy()
            except Exception:
                pass
            if restore and lock_text:
                restore_previous()

        def save(*, freeing: bool = False) -> None:
            value = entry.get()
            if not self.client:
                messagebox.showerror("Нет подключения", "Сначала подключите ключ в «Таблицы».")
                return
            if calendar and not freeing:
                typed = value.strip()
                booking = typed and typed.lower() != "запись" and "не запис" not in typed.lower()
                if booking and pregnant_var.get() not in {"yes", "no"}:
                    messagebox.showwarning(
                        "Беременность",
                        "Выберите: беременна или не беременна.",
                        parent=dialog,
                    )
                    return

            def work():
                if lock_text:
                    self.client.assert_calendar_lock(record, lock_text)
                to_write = write_back_value(record, field, value)
                self.client.update_cell(record, field, to_write)
                record.values[field] = value
                if record.origin_values:
                    record.origin_values[field] = to_write
                return to_write

            def done(_value):
                state["saved"] = True
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
                        name, phone = extract_phone(typed)
                        record.values["Клиент"] = name or typed
                        record.values["Телефон"] = phone
                    if self._refresh_slot_label(record):
                        booked = sum(
                            1
                            for item in self._visible
                            if item.layout == "calendar" and item.values.get("Статус") == "Занято"
                        )
                        self.count_label.configure(text=str(booked))
                    else:
                        self._calendar_fp = None
                        self._schedule_render(immediate=True)
                else:
                    self._schedule_render(immediate=True)
                self._set_status(f"Сохранено в таблицу: {when or record.source_name}")
                close_dialog(restore=False)
                self.reload_all()

            self._run_bg(work, done)

        def free_slot() -> None:
            entry.delete(0, "end")
            save(freeing=True)

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        self._primary_button(buttons, "Сохранить", lambda: save(freeing=False), 140).pack(side="left")
        show_free = calendar and (
            (sheet_previous is not None and classify_slot(sheet_previous or "") == "Занято")
            or (sheet_previous is None and record.values.get("Статус") == "Занято")
        )
        if show_free:
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
        self._outline_button(buttons, "Отмена", lambda: close_dialog(restore=True), 100).pack(
            side="right"
        )
        dialog.bind("<Return>", lambda *_: save(freeing=False))
        dialog.bind("<Escape>", lambda *_: close_dialog(restore=True))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_dialog(restore=True))
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
        dialog = self._dialog("Таблицы", "920x540")
        dialog.minsize(720, 420)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)
        self._tables_dialog_open = True

        account = ctk.CTkFrame(dialog, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        account.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 8))
        account.grid_columnconfigure(1, weight=1)

        creds_path = self.config_data.credentials
        email = ""
        if self.client:
            email = self.client.service_email
        elif creds_path.exists():
            email = credentials_email(creds_path)
        email_var = tk.StringVar(value=email or "ключ не выбран")

        ctk.CTkLabel(
            account,
            text="Сервисный аккаунт",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=10)
        ctk.CTkLabel(account, textvariable=email_var, text_color=GREEN, anchor="w").grid(
            row=0, column=1, sticky="ew", padx=8, pady=10
        )

        adv = ctk.CTkFrame(account, fg_color="transparent")
        adv.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        adv.grid_columnconfigure(1, weight=1)
        path_var = tk.StringVar(value=str(creds_path))

        def pick_key() -> None:
            if self._pick_credentials_file(parent=dialog):
                installed = self.config_data.credentials
                path_var.set(str(installed))
                email_var.set(credentials_email(installed) or installed.name)
                save_config(self.config_data)

        ctk.CTkLabel(adv, text="JSON ключа", text_color=MUTED, font=ctk.CTkFont(size=11)).grid(
            row=0, column=0, sticky="w", padx=4
        )
        _styled_entry(adv, "credentials.json", textvariable=path_var, height=28).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        self._outline_button(adv, "JSON…", pick_key, 80).grid(row=0, column=2, padx=4)

        sync_box = ctk.CTkFrame(dialog, fg_color=BG, corner_radius=10, border_width=1, border_color=LINE)
        sync_box.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        sync_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            sync_box,
            text="Список таблиц общий для всех компьютеров",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            sync_box,
            text="Один раз: создайте пустую Google-таблицу (не календарь), откройте её для "
            f"{email or 'сервисного аккаунта'} как Редактор и вставьте ссылку ниже. "
            "Дальше все ПК берут один и тот же список таблиц из облака.",
            text_color=MUTED,
            font=ctk.CTkFont(size=11),
            wraplength=780,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 6))
        raw_reg = (self.config_data.registry_spreadsheet_id or "").strip()
        if raw_reg.upper().startswith("PASTE_"):
            raw_reg = ""
        registry_var = tk.StringVar(value=raw_reg)
        sheet_var = tk.StringVar(value=self.config_data.registry_sheet or DEFAULT_REGISTRY_SHEET)
        ctk.CTkLabel(sync_box, text="Служебная таблица", text_color=MUTED, font=ctk.CTkFont(size=11)).grid(
            row=2, column=0, sticky="w", padx=12, pady=(0, 10)
        )
        _styled_entry(
            sync_box,
            "https://docs.google.com/spreadsheets/d/…",
            textvariable=registry_var,
            height=28,
        ).grid(row=2, column=1, sticky="ew", padx=4, pady=(0, 10))
        _styled_entry(sync_box, "SheetsHub", textvariable=sheet_var, height=28, width=120).grid(
            row=2, column=2, sticky="e", padx=(4, 12), pady=(0, 10)
        )

        editor = _RefList(
            dialog,
            "Рабочие таблицы (общие)",
            self._tables(),
            "Таблица",
            subtitle="Этот список одинаковый на всех ПК. Добавили здесь — появится везде после сохранения.",
        )
        editor.frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))

        status_var = tk.StringVar(value="")
        ctk.CTkLabel(dialog, textvariable=status_var, text_color=MUTED, font=ctk.CTkFont(size=11)).grid(
            row=3, column=0, sticky="w", padx=20, pady=(0, 4)
        )

        def close_dialog() -> None:
            self._tables_dialog_open = False
            try:
                dialog.destroy()
            except Exception:
                pass

        def load_from_cloud() -> None:
            registry_id = registry_var.get().strip()
            registry_sheet = sheet_var.get().strip() or DEFAULT_REGISTRY_SHEET
            if not registry_id or registry_id.upper().startswith("PASTE_"):
                status_var.set("Сначала вставьте ссылку на служебную таблицу.")
                return
            if not self.client:
                status_var.set("Нет подключения к Google — проверьте JSON ключа.")
                return
            status_var.set("Загружаю общий список…")

            def work():
                return self.client.pull_table_registry(registry_id, registry_sheet)

            def done(refs) -> None:
                editor.replace_refs(refs)
                self.config_data.registry_spreadsheet_id = registry_id
                self.config_data.registry_sheet = registry_sheet
                if refs:
                    self._apply_remote_tables(refs)
                    status_var.set(f"Загружено из облака: {len(refs)} таблиц")
                else:
                    status_var.set("В облаке пока пусто — добавьте таблицы и сохраните.")

            def work_safe():
                try:
                    return ("ok", work())
                except Exception as exc:
                    return ("err", exc)

            def done_safe(payload) -> None:
                kind, data = payload
                if kind == "err":
                    status_var.set(str(data).split("\n")[0])
                    messagebox.showerror(
                        "Не удалось загрузить",
                        str(data)
                        + "\n\nОткройте служебную таблицу для сервисного аккаунта (Редактор).",
                        parent=dialog,
                    )
                    return
                done(data)

            self._run_bg(work_safe, done_safe, alert=False)

        def save() -> None:
            refs = editor.collect()
            registry_id = registry_var.get().strip()
            registry_sheet = sheet_var.get().strip() or DEFAULT_REGISTRY_SHEET
            if not registry_id or registry_id.upper().startswith("PASTE_"):
                messagebox.showwarning(
                    "Нужна служебная таблица",
                    "Список таблиц общий для всех ПК.\n\n"
                    "1) Создайте пустую Google-таблицу\n"
                    "2) Откройте её для сервисного аккаунта как Редактор\n"
                    "3) Вставьте ссылку в поле «Служебная таблица»\n"
                    "4) Сохраните снова",
                    parent=dialog,
                )
                return
            if not refs:
                messagebox.showwarning("Нет таблиц", "Добавьте хотя бы одну рабочую таблицу.", parent=dialog)
                return
            if not self.client:
                messagebox.showerror("Нет подключения", "Сначала нужен рабочий credentials.json.", parent=dialog)
                return

            self.config_data.sources = refs
            self.config_data.destinations = list(refs)
            self.config_data.registry_spreadsheet_id = registry_id
            self.config_data.registry_sheet = registry_sheet
            save_config(self.config_data)
            status_var.set("Сохраняю общий список в облако…")

            def work_safe():
                try:
                    self.client.push_table_registry(registry_id, refs, registry_sheet)
                    return ("ok", len(refs))
                except Exception as exc:
                    return ("err", exc)

            def done_safe(payload) -> None:
                kind, data = payload
                if kind == "err":
                    messagebox.showerror(
                        "Не удалось сохранить в облако",
                        str(data)
                        + "\n\nСлужебная таблица должна быть открыта для сервисного аккаунта (Редактор).",
                        parent=dialog,
                    )
                    return
                self._set_status(f"Общий список сохранён: {data} таблиц — виден на всех ПК")
                close_dialog()
                self.client = None
                self._try_connect()

            self._run_bg(work_safe, done_safe, alert=False)

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.grid(row=4, column=0, pady=(0, 14))
        self._outline_button(buttons, "Загрузить из облака", load_from_cloud, 180).pack(
            side="left", padx=(0, 8)
        )
        self._primary_button(buttons, "Сохранить для всех ПК", save, 200).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        # При открытии сразу тянем общий список, если ссылка уже есть.
        if self._registry_configured() and self.client:
            dialog.after(200, load_from_cloud)

class _RefList:
    def __init__(
        self,
        parent,
        title: str,
        refs: list[SheetRef],
        placeholder: str,
        *,
        subtitle: str = "Одна ссылка: читаем и пишем сюда же. Лист — все или АВГУСТ 2026.",
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
            text=subtitle,
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            anchor="w",
            wraplength=640,
            justify="left",
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

    def replace_refs(self, refs: list[SheetRef]) -> None:
        for row in list(self.rows):
            try:
                row["frame"].destroy()
            except Exception:
                pass
        self.rows.clear()
        self._next_row = 0
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
    # Не удваиваем масштаб поверх системного DPI Windows.
    if sys.platform == "win32":
        try:
            ctk.set_widget_scaling(1.0)
            ctk.set_window_scaling(1.0)
        except Exception:
            pass
    app = SheetsHubApp()
    app.mainloop()
