import os
import io
import asyncio
from pyodide.ffi import create_proxy
from pyscript import document, window
from js import Uint8Array, Blob, URL

from core.parser import parse_file, Record
from core.report import generate_report
from core.exporter import filter_records, apply_ordering, export_flat, export_merged
from core.schema import RECORD_TYPES, BIT_FIELDS

# Same color mapping as gui.py for the web table
TYPE_COLOURS = {
    "IMU": "#00FFFF",
    "BARO": "#FFFF00",
    "ADXL": "#FFA500",
    "GPS_POS": "#90EE90",
    "GPS_VEL": "#98FB98",
    "GPS_ACC": "#00FF00",
    "GPS_LOW": "#32CD32",
    "BOARD": "#FF69B4",
}
COL_UNKNOWN = "#AAAAAA"
ALL_TYPE_NAMES = list(TYPE_COLOURS.keys())

class AppState:
    def __init__(self):
        self.parse_result = None
        self.report = None
        self.filepath = ""
        self.filename = ""
        
        self.type_enabled = {t: True for t in ALL_TYPE_NAMES}
        self.order = "packet"
        self.time_start = None
        self.time_end = None
        self.format = "flat"
        self.align_type = "IMU"
        
        self.view = []
        self.selected_seq = None
        
        self.page = 0
        self.page_size = 2000

    def recompute_view(self):
        self.page = 0
        if not self.parse_result:
            self.view = []
            return
            
        active_types = [t for t, enabled in self.type_enabled.items() if enabled]
        
        filtered = filter_records(
            self.parse_result.records,
            types=active_types,
            time_start=self.time_start,
            time_end=self.time_end,
            seq_start=None,
            seq_end=None,
        )
        self.view = apply_ordering(filtered, self.order)

state = AppState()

# ---------------------------------------------------------------------------
# UI Helpers & DOM Updates
# ---------------------------------------------------------------------------

def update_default_export_name():
    if not state.filename: return
    stem = state.filename.replace(".bin", "")
    fmt_str = f"merged_{state.align_type.lower()}" if state.format == "merged" else "flat"
    base_name = f"{stem}_{fmt_str}_{state.order}.csv"
    document.getElementById("export-filename").value = base_name

def rebuild_table():
    tbody = document.getElementById("tbody-records")
    if not state.view:
        tbody.innerHTML = ""
        return
        
    start_idx = state.page * state.page_size
    end_idx = start_idx + state.page_size
    page_records = state.view[start_idx:end_idx]
        
    html_parts = []
    # Build HTML string (very fast in browser)
    for rec in page_records:
        col = TYPE_COLOURS.get(rec.type_name, COL_UNKNOWN)
        fields_str = " ".join([f"{k}={v}" for k, v in list(rec.raw_fields.items())[:6]])
        if len(rec.raw_fields) > 6:
            fields_str += " ..."
            
        cls = "selected-row" if rec.seq == state.selected_seq else ""
        
        html_parts.append(
            f'<tr data-seq="{rec.seq}" class="{cls}" style="color:{col}; cursor:pointer;">'
            f'<td>{rec.seq}</td>'
            f'<td>{rec.time_ms}</td>'
            f'<td>{rec.type_name}</td>'
            f'<td style="color:#d4d4d4">{fields_str}</td>'
            f'</tr>'
        )
    tbody.innerHTML = "".join(html_parts)

def refresh_info_bar():
    bar = document.getElementById("info-text")
    if not state.report:
        bar.innerText = "No file loaded."
        return
    r = state.report
    v_len = len(state.view)
    
    max_page = max(0, (len(state.view) - 1) // state.page_size)
    page_text = f"Page {state.page + 1} of {max_page + 1} ({v_len} records shown)"
    
    text = (
        f"{r.good_count} total records | "
        f"{r.bad_count} bad | "
        f"{r.out_of_order_count} out-of-order | "
        f"Range: {r.time_start_ms} - {r.time_end_ms} ms | "
        f"{page_text}"
    )
    bar.innerText = text

def format_field_value(field_name, value):
    if field_name in ("lat", "lon"): return f"{value} ({value / 1e7:.6f} deg)"
    if field_name in ("gps_alt", "hMSL"): return f"{value} mm ({value / 1000:.2f} m)"
    if field_name == "pressure": return f"{value:.2f} hPa"
    if field_name == "temp": return f"{value:.2f} C"
    if field_name in ("ground_vel", "vert_vel"): return f"{value} mm/s ({value / 1000:.2f} m/s)"
    if field_name == "heading": return f"{value} ({value / 1e5:.2f} deg)"
    if field_name in ("hAcc", "vAcc"): return f"{value} mm (+/- {value / 1000:.2f} m)"
    if field_name == "headAcc": return f"{value} ({value / 1e5:.2f} deg)"
    return str(value)

def show_record_details(seq: int | None):
    state.selected_seq = seq
    
    if seq is None:
        if state.report:
            from core.report import format_report
            rep_str = format_report(state.report)
            document.getElementById("detail-pane").innerText = rep_str
        else:
            document.getElementById("detail-pane").innerHTML = "Click any row in the table to view decoded fields."
        rebuild_table()
        return

    # Find record
    rec = next((r for r in state.parse_result.records if r.seq == seq), None)
    if not rec: return
    
    lines = [f"SEQ: {rec.seq} | TIME: {rec.time_ms} ms | TYPE: {rec.type_name}", ""]
    
    # Raw fields and decoded bits
    for fname, fval in rec.raw_fields.items():
        val_str = format_field_value(fname, fval)
        lines.append(f"<span class='detail-raw'>{fname:<12} = {val_str}</span>")
        
        # Sub-fields from bitmask/bitfield
        if fname in BIT_FIELDS:
            desc = BIT_FIELDS[fname]
            decoded = {k.split("__")[1]: v for k, v in rec.decoded_fields.items() if k.startswith(fname + "__")}
            
            if desc["type"] == "bitmask":
                for bit_idx, sub_name in sorted(desc["bits"].items()):
                    is_set = decoded.get(sub_name, False)
                    sym = "[Y]" if is_set else "[ ]"
                    lines.append(f"<span class='detail-sub'>  {sym} {sub_name}</span>")
            elif desc["type"] == "bitfield":
                for sub in desc["fields"]:
                    sub_val = decoded.get(sub["name"], "?")
                    lines.append(f"<span class='detail-sub'>  [{sub['msb']}:{sub['lsb']}] {sub['name']:<15} = {sub_val}</span>")
            elif desc["type"] == "enum":
                enum_val = decoded.get(fname, "?")
                lines.append(f"<span class='detail-sub'>  -> {enum_val}</span>")
                
    document.getElementById("detail-pane").innerHTML = "<br>".join(lines)
    
    # Highlight row visually by re-rendering table
    rebuild_table()

# ---------------------------------------------------------------------------
# Event Handlers
# ---------------------------------------------------------------------------

async def on_file_upload(event):
    file_list = event.target.files
    if not file_list: return
        
    file = file_list.item(0)
    state.filename = file.name
    document.getElementById("status").innerText = f"Reading {file.name}..."
    
    try:
        array_buffer = await file.arrayBuffer()
        byte_array = Uint8Array.new(array_buffer)
        
        vfs_path = f"/tmp_{file.name}"
        with open(vfs_path, "wb") as f:
            f.write(byte_array.to_bytes())
            
        document.getElementById("status").innerText = f"Parsing..."
        
        result = parse_file(vfs_path)
        state.parse_result = result
        state.report = generate_report(result, filename=file.name)
        state.time_start = None
        state.time_end = None
        
        state.recompute_view()
        rebuild_table()
        refresh_info_bar()
        update_default_export_name()
        
        document.getElementById("status").innerText = f"Loaded {result.good_count} records."
        show_record_details(None)
        
    except Exception as e:
        document.getElementById("status").innerText = f"Error: {str(e)}"

def on_table_click(event):
    target = event.target
    while target and getattr(target, "tagName", "") != "TR":
        target = getattr(target, "parentElement", None)
    if target and target.hasAttribute("data-seq"):
        seq = int(target.getAttribute("data-seq"))
        if state.selected_seq == seq:
            show_record_details(None)
        else:
            show_record_details(seq)

def on_page_prev(event):
    if state.page > 0:
        state.page -= 1
        rebuild_table()
        refresh_info_bar()

def on_page_next(event):
    max_page = max(0, (len(state.view) - 1) // state.page_size)
    if state.page < max_page:
        state.page += 1
        rebuild_table()
        refresh_info_bar()

def on_type_change(event):
    tname = event.target.value
    state.type_enabled[tname] = event.target.checked
    state.recompute_view()
    rebuild_table()
    refresh_info_bar()

def on_order_change(event):
    state.order = event.target.value
    state.recompute_view()
    rebuild_table()
    update_default_export_name()

def on_time_change(event):
    try:
        ts = int(document.getElementById("time-start").value)
        state.time_start = ts if ts > 0 else None
    except: state.time_start = None
    
    try:
        te = int(document.getElementById("time-end").value)
        state.time_end = te if te > 0 else None
    except: state.time_end = None
    
    state.recompute_view()
    rebuild_table()
    refresh_info_bar()

def on_format_change(event):
    state.format = event.target.value
    align_container = document.getElementById("align-container")
    if state.format == "merged":
        align_container.classList.remove("hidden")
    else:
        align_container.classList.add("hidden")
    update_default_export_name()

def on_align_change(event):
    state.align_type = event.target.value
    update_default_export_name()

def trigger_download(filename, content_string):
    blob = Blob.new([content_string], {type: "text/csv;charset=utf-8;"})
    url = URL.createObjectURL(blob)
    a = document.createElement("a")
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)

def on_export(event):
    if not state.view: return
    
    user_filename = document.getElementById("export-filename").value
    if not user_filename: user_filename = "export.csv"
    
    document.getElementById("status").innerText = "Generating CSV..."
    
    out_io = io.StringIO()
    try:
        if state.format == "flat":
            export_flat(state.view, out=out_io, include_decoded=True)
        else:
            export_merged(state.view, align_type=state.align_type, out=out_io, include_decoded=True)
            
        trigger_download(user_filename, out_io.getvalue())
        document.getElementById("status").innerText = f"Exported {len(state.view)} rows -> {user_filename}"
    except Exception as e:
        document.getElementById("status").innerText = f"Export Error: {e}"

# ---------------------------------------------------------------------------
# Initialization & Setup
# ---------------------------------------------------------------------------

# Build type checkboxes
tf_container = document.getElementById("type-filters")
for t in ALL_TYPE_NAMES:
    col = TYPE_COLOURS.get(t, COL_UNKNOWN)
    lbl = document.createElement("label")
    lbl.style.color = col
    
    chk = document.createElement("input")
    chk.type = "checkbox"
    chk.value = t
    chk.checked = True
    chk.addEventListener("change", create_proxy(on_type_change))
    
    lbl.appendChild(chk)
    lbl.appendChild(document.createTextNode(f" {t}"))
    tf_container.appendChild(lbl)

# Bind other events
document.getElementById("file-upload").addEventListener("change", create_proxy(on_file_upload))
document.getElementById("file-upload").disabled = False

document.getElementById("tbody-records").addEventListener("click", create_proxy(on_table_click))

for el in document.getElementsByName("order"):
    el.addEventListener("change", create_proxy(on_order_change))
    
document.getElementById("time-start").addEventListener("input", create_proxy(on_time_change))
document.getElementById("time-end").addEventListener("input", create_proxy(on_time_change))

for el in document.getElementsByName("format"):
    el.addEventListener("change", create_proxy(on_format_change))
    
document.getElementById("align-combo").addEventListener("change", create_proxy(on_align_change))
document.getElementById("btn-export").addEventListener("click", create_proxy(on_export))

# Bind pagination
document.getElementById("btn-prev").addEventListener("click", create_proxy(on_page_prev))
document.getElementById("btn-next").addEventListener("click", create_proxy(on_page_next))

document.getElementById("status").innerText = "Ready. Select a .bin file."
