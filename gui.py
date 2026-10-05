#!/usr/bin/env python3
"""
gui.py - Dear PyGui frontend for the avionics binary log inspector.

Imports core/ directly - no subprocess calls to inspect.py.

Requirements:
    pip install dearpygui

Usage:
    python gui.py [file.bin]
"""

import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

import dearpygui.dearpygui as dpg

from core.schema import RECORD_TYPES, RECORD_SIZE, BIT_FIELDS, decode_bitfields
from core.parser import parse_file, ParseResult, Record
from core.report import generate_report, format_report, ReportData
from core.exporter import filter_records, apply_ordering, export_flat, export_merged

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_TITLE   = "Binary Packet Inspector - Avionics"
WIN_W, WIN_H = 1400, 850

LEFT_W      = 230   # left panel width
DETAIL_H    = 220   # detail panel height
INFO_H      = 42    # info bar height
TABLE_H     = WIN_H - DETAIL_H - INFO_H - 60  # main table area height

# Colour palette (R, G, B, A)
COL_HEADER   = (60,  60,  80,  255)
COL_IMU      = (80,  140, 200, 255)
COL_BARO     = (80,  200, 120, 255)
COL_ADXL     = (200, 160, 60,  255)
COL_GPS      = (180, 100, 220, 255)
COL_BOARD    = (220, 100, 100, 255)
COL_UNKNOWN  = (150, 150, 150, 255)
COL_ERROR    = (220,  60,  60, 255)
COL_WARN     = (220, 180,  40, 255)
COL_OK       = (60,  200,  80, 255)

TYPE_COLOURS = {
    "IMU":     COL_IMU,
    "BARO":    COL_BARO,
    "ADXL":    COL_ADXL,
    "GPS_POS": COL_GPS,
    "GPS_VEL": COL_GPS,
    "GPS_ACC": COL_GPS,
    "GPS_LOW": COL_GPS,
    "BOARD":   COL_BOARD,
}

ALL_TYPE_NAMES = [v["name"] for v in RECORD_TYPES.values()]

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class AppState:
    """All mutable state for the application."""

    def __init__(self):
        self.filepath:        str | None       = None
        self.parse_result:    ParseResult | None = None
        self.report:          ReportData | None  = None

        # filtered + ordered view (list of Record)
        self.view:            list[Record]     = []

        # UI filter state
        self.type_enabled:    dict[str, bool]  = {n: True for n in ALL_TYPE_NAMES}
        self.order:           str              = "packet"   # 'packet' | 'chrono'
        self.time_start:      int | None       = None
        self.time_end:        int | None       = None
        self.format:          str              = "flat"     # 'flat' | 'merged'
        self.align_type:      str              = "IMU"
        self.include_decoded: bool             = True

        # Pagination
        self.page:            int              = 0
        self.page_size:       int              = 2000

        # selected record
        self.selected_seq:    int | None       = None
        self.selected_record: Record | None    = None

        # loading state
        self.loading:         bool             = False
        self.status_msg:      str              = "No file loaded."

    def recompute_view(self):
        """Re-apply current filters and ordering to the parsed records."""
        self.page = 0
        if not self.parse_result:
            self.view = []
            return
        enabled_names = [n for n, v in self.type_enabled.items() if v]
        records = filter_records(
            self.parse_result.records,
            types=enabled_names if len(enabled_names) < len(ALL_TYPE_NAMES) else None,
            time_start=self.time_start,
            time_end=self.time_end,
        )
        self.view = apply_ordering(records, order=self.order)


state = AppState()

# ---------------------------------------------------------------------------
# Tag helpers - all DPG item tags as string constants
# ---------------------------------------------------------------------------

TAG_MAIN_WIN        = "main_window"
TAG_FILE_LABEL      = "file_label"
TAG_STATUS_TEXT     = "status_text"
TAG_INFO_BAR        = "info_bar"
TAG_RECORD_TABLE    = "record_table"
TAG_DETAIL_GROUP    = "detail_group"
TAG_LOADING_OVERLAY = "loading_overlay"
TAG_ORDER_RADIO     = "order_radio"
TAG_FORMAT_RADIO    = "format_radio"
TAG_TIME_START      = "time_start"
TAG_TIME_END        = "time_end"
TAG_ALIGN_COMBO     = "align_combo"
TAG_DECODE_CHECK    = "decode_check"
TAG_EXPORT_FILENAME = "export_filename"
TAG_EXPORT_BTN      = "export_btn"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _get_safe_filepath(desired_path: str) -> str:
    """Returns a non-existing path by appending _1, _2 etc. if needed."""
    if not os.path.exists(desired_path):
        return desired_path
    dir_name = os.path.dirname(desired_path)
    base = os.path.basename(desired_path)
    stem, ext = os.path.splitext(base)
    idx = 1
    while True:
        cand = os.path.join(dir_name, f"{stem}_{idx}{ext}")
        if not os.path.exists(cand):
            return cand
        idx += 1


def _update_default_export_name():
    if not state.filepath: return
    stem = os.path.splitext(os.path.basename(state.filepath))[0]
    fmt_str = f"merged_{state.align_type.lower()}" if state.format == "merged" else "flat"
    base_name = f"{stem}_{fmt_str}_{state.order}.csv"
    
    dir_name = os.path.dirname(state.filepath)
    safe_path = _get_safe_filepath(os.path.join(dir_name, base_name))
    _safe_set_value(TAG_EXPORT_FILENAME, os.path.basename(safe_path))

def type_colour(type_name: str) -> list[int]:
    return list(TYPE_COLOURS.get(type_name, COL_UNKNOWN))


def format_field_value(field_name: str, value) -> str:
    """Pretty-print a raw field value with unit hints."""
    # GPS coordinates: stored as 1e-7 degrees
    if field_name in ("lat", "lon"):
        return f"{value} ({value / 1e7:.6f} deg)"
    # GPS altitude / height: stored in mm
    if field_name in ("gps_alt", "hMSL"):
        return f"{value} mm ({value / 1000:.2f} m)"
    # Pressure: hPa float
    if field_name == "pressure":
        return f"{value:.2f} hPa"
    # Temperature: Celsius float
    if field_name == "temp":
        return f"{value:.2f}  degC"
    # Velocities: mm/s
    if field_name in ("ground_vel", "vert_vel"):
        return f"{value} mm/s ({value / 1000:.2f} m/s)"
    # Heading: 1e-5 degrees
    if field_name == "heading":
        return f"{value} ({value / 1e5:.2f} deg)"
    # Accuracies: mm or 1e-5 deg
    if field_name in ("hAcc", "vAcc"):
        return f"{value} mm (+/- {value / 1000:.2f} m)"
    if field_name == "headAcc":
        return f"{value} ({value / 1e5:.2f} deg)"
    return str(value)


# ---------------------------------------------------------------------------
# File loading (runs in a background thread)
# ---------------------------------------------------------------------------

def load_file_thread(path: str):
    state.loading = True
    state.status_msg = f"Parsing {os.path.basename(path)}..."
    _safe_set_value(TAG_STATUS_TEXT, state.status_msg)

    try:
        result = parse_file(path, mode="stride", strict=False)
        report = generate_report(result, filename=os.path.basename(path))
        state.parse_result = result
        state.report       = report
        state.filepath     = path
        state.time_start   = None
        state.time_end     = None
        state.recompute_view()
        state.status_msg = (
            f"{result.good_count} records loaded"
            + (f"  |  {result.bad_count} bad" if result.bad_count else "")
        )
        state.selected_record = None
        _rebuild_table()
        _rebuild_detail_panel(None)
        _refresh_info_bar()
        _update_default_export_name()
    except Exception as exc:
        state.status_msg = f"ERROR: {exc}"
    finally:
        state.loading = False
        _safe_set_value(TAG_STATUS_TEXT, state.status_msg)


def _safe_set_value(tag: str, value):
    """Set a DPG item value from any thread."""
    try:
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Table rebuild
# ---------------------------------------------------------------------------

# We keep a fixed set of columns and update row text only.
# DPG tables are rebuilt by deleting all rows and re-adding them.

_TABLE_COLS = ["seq", "time_ms", "type", "fields"]

def _rebuild_table():
    """Delete existing table rows and repopulate from state.view."""
    if not dpg.does_item_exist(TAG_RECORD_TABLE):
        return

    # Delete all children (rows) of the table
    dpg.delete_item(TAG_RECORD_TABLE, children_only=True, slot=1)

    start_idx = state.page * state.page_size
    end_idx = start_idx + state.page_size
    page_records = state.view[start_idx:end_idx]

    for rec in page_records:
        # Build a compact field summary string
        fields_str = "  ".join(f"{k}={v}" for k, v in list(rec.raw_fields.items())[:6])
        if len(rec.raw_fields) > 6:
            fields_str += "  ..."

        with dpg.table_row(parent=TAG_RECORD_TABLE):
            # Use selectable to make the whole row clickable and responsive
            dpg.add_selectable(
                label=str(rec.seq), 
                span_columns=True, 
                callback=_on_row_click, 
                user_data=rec
            )
            dpg.add_text(str(rec.time_ms))
            # Coloured type label
            col = type_colour(rec.type_name)
            dpg.add_text(rec.type_name, color=col)
            dpg.add_text(fields_str)


def _refresh_info_bar():
    """Update the bottom info bar with current report stats."""
    if not state.report or not dpg.does_item_exist(TAG_INFO_BAR):
        return
    r = state.report
    
    max_page = max(0, (len(state.view) - 1) // state.page_size)
    page_text = f"Page {state.page + 1} of {max_page + 1}  ({len(state.view)} records shown)"
    
    text = (
        f"  {r.good_count} total"
        f"  |  {r.bad_count} bad"
        f"  |  {r.out_of_order_count} out-of-order (max jitter {r.max_jitter_ms} ms)"
        f"  |  {r.time_start_ms} ms -> {r.time_end_ms} ms  ({r.duration_ms / 1000:.1f} s)"
        f"  |  {page_text}"
    )
    dpg.set_value(TAG_INFO_BAR, text)


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

def _on_row_click(sender, app_data, user_data: Record):
    if state.selected_record and state.selected_record.seq == user_data.seq:
        # Deselect if clicking the same row again
        state.selected_record = None
        _rebuild_detail_panel(None)
    else:
        state.selected_record = user_data
        _rebuild_detail_panel(user_data)


def _rebuild_detail_panel(rec: Record | None):
    """Repopulate the detail panel with decoded fields or the global report if none selected."""
    if not dpg.does_item_exist(TAG_DETAIL_GROUP):
        return
    dpg.delete_item(TAG_DETAIL_GROUP, children_only=True)

    if rec is None:
        if state.report:
            # Display the flight report
            rep_str = format_report(state.report)
            dpg.add_text("  " + rep_str.replace("\n", "\n  "), color=(160, 200, 255, 255), parent=TAG_DETAIL_GROUP)
        else:
            dpg.add_text("  Click a row to inspect a record, or click an active row to deselect and view this report.", color=(120, 120, 140, 255), parent=TAG_DETAIL_GROUP)
        return

    col = type_colour(rec.type_name)

    dpg.add_text(
        f"  Record  seq={rec.seq}  |  time={rec.time_ms} ms  |  type=0x{rec.type_id:02X} {rec.type_name}",
        color=col,
        parent=TAG_DETAIL_GROUP,
    )
    dpg.add_text(
        f"  hex: {rec.raw_bytes.hex(' ').upper()}",
        color=(160, 160, 160, 255),
        parent=TAG_DETAIL_GROUP,
    )
    dpg.add_separator(parent=TAG_DETAIL_GROUP)

    for field_name, raw_val in rec.raw_fields.items():
        pretty = format_field_value(field_name, raw_val)
        line   = f"  {field_name:<22} = {pretty}"

        if field_name in BIT_FIELDS:
            # Show raw line in amber, then expand bit-fields below
            dpg.add_text(line, color=COL_WARN, parent=TAG_DETAIL_GROUP)
            decoded = decode_bitfields(field_name, int(raw_val))
            desc    = BIT_FIELDS[field_name]

            if desc["type"] == "bitmask":
                for bit_idx, bit_name in sorted(desc["bits"].items()):
                    is_set = bool(int(raw_val) & (1 << bit_idx))
                    flag_col = COL_OK if is_set else (100, 100, 100, 200)
                    flag_sym = "[Y]" if is_set else "[ ]"
                    dpg.add_text(
                        f"      bit{bit_idx}  {bit_name:<22} {flag_sym}",
                        color=flag_col,
                        parent=TAG_DETAIL_GROUP,
                    )

            elif desc["type"] == "bitfield":
                for sub in desc["fields"]:
                    sub_val = decoded.get(sub["name"], "?")
                    dpg.add_text(
                        f"      [{sub['msb']}:{sub['lsb']}]  {sub['name']:<20} = {sub_val}",
                        color=(160, 200, 255, 255),
                        parent=TAG_DETAIL_GROUP,
                    )

            elif desc["type"] == "enum":
                enum_val = decoded.get(field_name, "?")
                dpg.add_text(
                    f"      -> {enum_val}",
                    color=(160, 200, 255, 255),
                    parent=TAG_DETAIL_GROUP,
                )
        else:
            dpg.add_text(line, parent=TAG_DETAIL_GROUP)


# ---------------------------------------------------------------------------
# Filter / control callbacks
# ---------------------------------------------------------------------------

def _on_type_toggle(sender, app_data, user_data: str):
    state.type_enabled[user_data] = app_data
    state.recompute_view()
    _rebuild_table()


def _on_order_change(sender, app_data):
    state.order = "chrono" if app_data == "Chrono" else "packet"
    state.recompute_view()
    _rebuild_table()
    _update_default_export_name()


def _on_time_filter_change(sender, app_data, user_data: str):
    val = int(app_data) if app_data else None
    if user_data == "start":
        state.time_start = val if val and val > 0 else None
    else:
        state.time_end = val if val and val > 0 else None
    state.recompute_view()
    _rebuild_table()


def _on_format_change(sender, app_data):
    state.format = "merged" if app_data == "Merged" else "flat"
    _sync_align_visibility()
    _update_default_export_name()


def _sync_align_visibility():
    if dpg.does_item_exist(TAG_ALIGN_COMBO):
        dpg.configure_item(TAG_ALIGN_COMBO, show=(state.format == "merged"))


def _on_align_change(sender, app_data):
    state.align_type = app_data.upper()
    _update_default_export_name()


def _on_decode_toggle(sender, app_data):
    state.include_decoded = app_data


def _on_open_file(sender, app_data):
    if app_data and app_data.get("file_path_name"):
        path = app_data["file_path_name"]
        dpg.set_value(TAG_FILE_LABEL, os.path.basename(path))
        threading.Thread(target=load_file_thread, args=(path,), daemon=True).start()


def _on_export_csv():
    if not state.view:
        dpg.set_value(TAG_STATUS_TEXT, "Nothing to export - load a file first.")
        return

    if not state.filepath:
        return

    user_filename = dpg.get_value(TAG_EXPORT_FILENAME)
    if not user_filename:
        user_filename = "export.csv"

    dir_name = os.path.dirname(state.filepath)
    desired_path = os.path.join(dir_name, user_filename)
    
    # Enforce no-overwrite by checking safe path right at export time
    final_path = _get_safe_filepath(desired_path)

    try:
        with open(final_path, "w", newline="", encoding="utf-8") as fh:
            if state.format == "flat":
                n = export_flat(state.view, out=fh, include_decoded=state.include_decoded)
            else:
                n = export_merged(
                    state.view,
                    align_type=state.align_type,
                    out=fh,
                    include_decoded=state.include_decoded,
                )
        dpg.set_value(TAG_STATUS_TEXT, f"Exported {n} rows -> {os.path.basename(final_path)}")
        # Update the UI field to show the name we actually used (in case a number was appended)
        dpg.set_value(TAG_EXPORT_FILENAME, os.path.basename(final_path))
    except Exception as exc:
        dpg.set_value(TAG_STATUS_TEXT, f"Export ERROR: {exc}")


def _on_reload():
    if state.filepath:
        threading.Thread(target=load_file_thread, args=(state.filepath,), daemon=True).start()


def _on_page_prev():
    if state.page > 0:
        state.page -= 1
        _rebuild_table()
        _refresh_info_bar()

def _on_page_next():
    max_page = max(0, (len(state.view) - 1) // state.page_size)
    if state.page < max_page:
        state.page += 1
        _rebuild_table()
        _refresh_info_bar()


# ---------------------------------------------------------------------------
# Layout builder
# ---------------------------------------------------------------------------

def build_ui():
    dpg.create_context()

    # ---- theme ----
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,      (22, 22, 30, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,       (28, 28, 38, 255))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,       (40, 40, 55, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Header,        (55, 75, 110, 255))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (70, 100, 150, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (55, 75, 110, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (80, 110, 160, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Text,          (220, 220, 230, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (50, 50, 70, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,       (28, 28, 38, 255))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt,    (33, 33, 45, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,  4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,  6)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,     6, 4)
    dpg.bind_theme(global_theme)

    # ---- file dialog ----
    with dpg.file_dialog(
        label="Open Binary Log",
        directory_selector=False,
        show=False,
        callback=_on_open_file,
        tag="file_dialog",
        width=700,
        height=450,
    ):
        dpg.add_file_extension(".bin", color=(80, 200, 120, 255), custom_text="[Binary Log]")
        dpg.add_file_extension(".*")

    # ---- main window ----
    with dpg.window(
        label=APP_TITLE,
        tag=TAG_MAIN_WIN,
        width=WIN_W,
        height=WIN_H,
        no_close=True,
        no_collapse=True,
        no_move=True,
        no_resize=True,
    ):

        # === TOP BAR ===
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Open File...",
                callback=lambda: dpg.show_item("file_dialog"),
                width=100,
            )
            dpg.add_text("", tag=TAG_FILE_LABEL)
            dpg.add_spacer(width=20)
            dpg.add_button(label="Reload", callback=_on_reload, width=70)
            dpg.add_spacer(width=20)
            dpg.add_text("", tag=TAG_STATUS_TEXT, color=(160, 160, 160, 255))

        dpg.add_separator()

        # === MAIN CONTENT (left panel + right area side by side) ===
        with dpg.group(horizontal=True):

            # ----- LEFT PANEL -----
            with dpg.child_window(width=LEFT_W, height=WIN_H - 80, border=True):

                dpg.add_text("Sensor Types", color=(150, 150, 200, 255))
                dpg.add_separator()
                for name in ALL_TYPE_NAMES:
                    dpg.add_checkbox(
                        label=name,
                        default_value=True,
                        callback=_on_type_toggle,
                        user_data=name,
                        tag=f"chk_{name}",
                    )

                dpg.add_spacer(height=10)
                dpg.add_separator()
                dpg.add_text("Ordering", color=(150, 150, 200, 255))
                dpg.add_radio_button(
                    items=["Packet", "Chrono"],
                    default_value="Packet",
                    callback=_on_order_change,
                    tag=TAG_ORDER_RADIO,
                )

                dpg.add_spacer(height=10)
                dpg.add_separator()
                dpg.add_text("Time Filter (ms)", color=(150, 150, 200, 255))
                dpg.add_input_text(
                    label="Start",
                    tag=TAG_TIME_START,
                    width=100,
                    decimal=True,
                    on_enter=True,
                    callback=_on_time_filter_change,
                    user_data="start",
                )
                dpg.add_input_text(
                    label="End",
                    tag=TAG_TIME_END,
                    width=100,
                    decimal=True,
                    on_enter=True,
                    callback=_on_time_filter_change,
                    user_data="end",
                )

                dpg.add_spacer(height=10)
                dpg.add_separator()
                dpg.add_text("Export Format", color=(150, 150, 200, 255))
                dpg.add_radio_button(
                    items=["Flat", "Merged"],
                    default_value="Flat",
                    callback=_on_format_change,
                    tag=TAG_FORMAT_RADIO,
                )
                dpg.add_combo(
                    items=ALL_TYPE_NAMES,
                    default_value="IMU",
                    label="Align",
                    width=110,
                    callback=_on_align_change,
                    tag=TAG_ALIGN_COMBO,
                    show=False,   # only shown when Merged is selected
                )
                dpg.add_checkbox(
                    label="Decode bit-fields",
                    default_value=True,
                    callback=_on_decode_toggle,
                    tag=TAG_DECODE_CHECK,
                )

                dpg.add_spacer(height=10)
                dpg.add_separator()
                dpg.add_text("Export Name", color=(150, 150, 200, 255))
                dpg.add_input_text(
                    default_value="export.csv",
                    tag=TAG_EXPORT_FILENAME,
                    width=LEFT_W - 20,
                )
                dpg.add_spacer(height=5)
                dpg.add_button(
                    label="Export CSV",
                    callback=_on_export_csv,
                    tag=TAG_EXPORT_BTN,
                    width=LEFT_W - 20,
                )

            # ----- RIGHT AREA -----
            with dpg.group():

                # --- Record table ---
                with dpg.child_window(
                    width=WIN_W - LEFT_W - 20,
                    height=TABLE_H,
                    border=True,
                ):
                    with dpg.table(
                        tag=TAG_RECORD_TABLE,
                        header_row=True,
                        borders_innerH=True,
                        borders_outerH=True,
                        borders_innerV=True,
                        borders_outerV=True,
                        row_background=True,
                        scrollY=True,
                        scrollX=True,
                        freeze_rows=1,
                        policy=dpg.mvTable_SizingStretchProp,
                        resizable=True,
                    ):
                        dpg.add_table_column(label="seq",     width_fixed=True,   init_width_or_weight=60)
                        dpg.add_table_column(label="time_ms", width_fixed=True,   init_width_or_weight=100)
                        dpg.add_table_column(label="type",    width_fixed=True,   init_width_or_weight=90)
                        dpg.add_table_column(label="fields",  width_stretch=True)

                # --- Detail panel ---
                with dpg.child_window(
                    width=WIN_W - LEFT_W - 20,
                    height=DETAIL_H,
                    border=True,
                    tag=TAG_DETAIL_GROUP,
                ):
                    dpg.add_text(
                        "  Click a row to inspect a record.",
                        color=(120, 120, 140, 255),
                    )

        dpg.add_separator()

        # === PAGINATION & INFO BAR ===
        with dpg.group(horizontal=True):
            dpg.add_button(label=" < Prev Page ", callback=_on_page_prev)
            dpg.add_button(label=" Next Page > ", callback=_on_page_next)
            dpg.add_text("  No file loaded.", tag=TAG_INFO_BAR, color=(120, 160, 120, 255))

    # ---- viewport ----
    dpg.create_viewport(
        title=APP_TITLE,
        width=WIN_W,
        height=WIN_H,
        resizable=True,
    )
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window(TAG_MAIN_WIN, True)


# ---------------------------------------------------------------------------
# Viewport resize handler
# ---------------------------------------------------------------------------

def _on_viewport_resize():
    """Keep the main window filling the viewport when it's resized."""
    vw = dpg.get_viewport_client_width()
    vh = dpg.get_viewport_client_height()
    if dpg.does_item_exist(TAG_MAIN_WIN):
        dpg.configure_item(TAG_MAIN_WIN, width=vw, height=vh)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    build_ui()

    # If a file was passed on the command line, load it immediately
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        path = sys.argv[1]
        dpg.set_value(TAG_FILE_LABEL, os.path.basename(path))
        threading.Thread(target=load_file_thread, args=(path,), daemon=True).start()

    while dpg.is_dearpygui_running():
        _on_viewport_resize()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
