# Binary Packet Inspector — Architecture

## Overview

A two-frontend tool (CLI + GUI) for inspecting, decoding, and exporting binary
log files produced by the avionics flight computer.  
Both frontends share the same Python core library — no subprocess bridging.

---

## Binary File Format

Each record in the log file is a fixed-size `Data_t` struct (packed, little-endian):

```
┌──────────┬──────────────┬─────────────────────────────────────────┐
│ type     │ time_ms      │ payload  (union — size of largest member)│
│ uint8    │ uint32 (ms)  │ varies by type, zero-padded to union size│
└──────────┴──────────────┴─────────────────────────────────────────┘
```

Every record is **exactly `sizeof(Data_t)` bytes** (fixed stride).  
The `type` byte selects which union member to decode.

### Record Types

| Type | ID   | Struct             | Fields                                              | Rate     |
|------|------|--------------------|-----------------------------------------------------|----------|
| IMU  | 0x01 | `imu_data_t`       | ax,ay,az,gx,gy,gz (int16 × 6)                      | ~200 Hz  |
| BARO | 0x02 | `baro_data_t`      | pressure (float), temp (float)                      | ~25 Hz   |
| ADXL | 0x03 | `adxl_data_t`      | ax,ay,az (int16 × 3)                                | ~100 Hz  |
| GPS_POS | 0x04 | `gps_pos_data_t` | lat,lon,gps_alt (int32 × 3, 1e-7 deg / mm)        | ~10 Hz   |
| GPS_VEL | 0x05 | `gps_vel_data_t` | ground_vel,vert_vel,heading (int32 × 3)            | ~10 Hz   |
| GPS_ACC | 0x06 | `gps_accuracy_data_t` | hAcc,vAcc,headAcc (uint32 × 3)                | ~10 Hz   |
| GPS_LOW | 0x07 | `gps_low_freq_data_t` | UTC_time,hMSL,numSV,valid,fixType,flags        | ~10 Hz   |
| BOARD   | 0x08 | `board_data_t`   | temp,v_batt,state,error_code,pyro_state,RSSI (uint8 × 6) | ~10 Hz |

### Packed Fields (Bit-Fields)

Several uint8 fields pack multiple values into individual bits:

**`error_code`** — bitmask
```
bit 0  IMU_ERROR
bit 1  BARO_ERROR
bit 2  GPS_ERROR
bit 3  GSM_ERROR
bit 4  SD_ERROR
bit 5  LOG_ENABLED
bit 6  CMD_RECEIVED   (command received in last LoRa cycle)
bit 7  ACK_RECEIVED   (ack received in last LoRa cycle)
```

**`state`** — split bitfield
```
bits [7:5]  flight_state  → 0=PAD, 1=ARMED, 2=BOOST, 3=COAST,
                             4=APOGEE, 5=DESCENT, 6=LANDED, 7=ERROR
bits [4:0]  num_sv        → number of GPS satellites tracked
```

**`pyro_state`** — bitmask
```
bit 0  PYRO1_CONT   (continuity)
bit 1  PYRO1_TRIG   (triggered)
bit 2  PYRO2_CONT
bit 3  PYRO2_TRIG
bit 4  PYRO3_CONT
bit 5  PYRO3_TRIG
bit 6  PYRO4_CONT
bit 7  PYRO4_TRIG
```

**GPS `valid`** — bitmask
```
bit 0  DATE_VALID
bit 1  TIME_VALID
bit 2  FULLY_RESOLVED
```

**GPS `flags`** — bitmask
```
bit 0  GNSS_FIX_OK
bit 1  DIFF_SOLN
```

**GPS `fixType`** — enum
```
0  NO_FIX
1  DEAD_RECKONING
2  2D_FIX
3  3D_FIX
4  GNSS+DR
5  TIME_ONLY
```

---

## Project Layout

```
Binary_Packet_Inspector_Avionics/
│
├── ARCHITECTURE.md          ← this file
│
├── core/
│   ├── __init__.py
│   ├── schema.py            ← struct layout + bit-field descriptors
│   ├── parser.py            ← binary reader, record iterator, integrity checks
│   ├── exporter.py          ← ordering modes, CSV/merge generation
│   └── report.py            ← summary statistics computation
│
├── inspect.py               ← CLI frontend
├── gui.py                   ← GUI frontend (Dear PyGui)
│
├── tests/
│   ├── gen_test_binary.py   ← generates synthetic .bin files for testing
│   └── test_parser.py       ← unit tests for parser + schema
│
└── requirements.txt
```

---

## Module Responsibilities

### `core/schema.py`
Single source of truth for the binary format. Defines:
- `RECORD_HEADER_FMT` — struct format string for the 5-byte header
- `RECORD_TYPES` — dict mapping type byte → (name, fmt, field_names)
- `BIT_FIELDS` — dict mapping field names → decode descriptors
- `RECORD_SIZE` — total bytes per record (must match `sizeof(Data_t)` on Teensy)
- Helper: `decode_bitfields(field_name, raw_value)` → dict of decoded sub-values

When the firmware gains a new `Data_t` union member:
1. Add the type constant to the firmware
2. Add an entry to `RECORD_TYPES` in `schema.py`
3. Done — parser, exporter, and GUI all pick it up automatically

### `core/parser.py`
- `parse_file(path, mode='stride')` → iterator of `Record` namedtuples
- `Record` fields: `seq, type_id, type_name, time_ms, raw_fields, decoded_fields`
- **Stride mode** (default): reads fixed-size chunks, warns on unknown type bytes,
  skips bad records, reports count at end
- **Scan mode** (`--recover`): searches byte-by-byte for valid type+plausible timestamp,
  used for partially corrupted files
- Detects and reports truncated trailing record (partial write at EOF)
- Returns `ParseResult`: records list + integrity stats

### `core/report.py`
- `generate_report(parse_result)` → `ReportData` dataclass
- Per-type: record count, average rate, min/max inter-record gap
- Overall: time range, total records, bad records, out-of-order count, max jitter
- Does not print anything — returns structured data, both frontends format it themselves

### `core/exporter.py`
Two export modes, each supporting two ordering modes:

**Flat mode** (`--format flat`):
- One CSV row per record
- Columns: `seq, time_ms, type, <all raw fields...>, <all decoded fields...>`
- Fields not belonging to current record type are empty
- Preserves full information including sequence and ordering

**Merged mode** (`--format merged --align <type>`):
- One "spine" type (e.g. IMU) defines the timestamp axis
- All other selected types are joined via nearest-timestamp match
- Forward-fill for slow sensors (BARO, GPS) between updates
- Columns: `seq, time_ms, <imu_fields>, <baro_fields>, <gps_pos_fields>...`
- Lost: records of non-spine types that fall between two spine timestamps

**Ordering modes** (applied before export):
- `packet` — records in file order (default). Use to debug logger, detect drops.
- `chrono` — records sorted by `time_ms`. Use for analysis. Reports jitter stats.

**Filtering** (applied before ordering):
- By type selection
- By time window (`time_start`, `time_end` in ms)
- By sequence range (`seq_start`, `seq_end`)

### `inspect.py` — CLI
```
Usage: python inspect.py <file.bin> [options]

  --info                    Summary table: counts, rates, gaps, ordering quality
  --raw [N]                 Hex + decoded dump of first N records (default 20)

  --csv [types ...]         Sensor streams to include (default: all)
  --format flat|merged      Export format (default: flat)
  --align <type>            Spine type for merged mode (default: imu)
  --out <path>              Output CSV path (default: <stem>.csv)

  --order packet|chrono     Record ordering (default: packet)
  --time-start <ms>
  --time-end   <ms>
  --seq-start  <N>
  --seq-end    <N>

  --no-decode               Omit decoded bit-field columns, raw values only
  --recover                 Use scan mode for damaged files
  --strict                  Abort on first bad record (default: warn+skip)

  --record-size <N>         Override RECORD_SIZE if schema.py value is wrong
```

### `gui.py` — Dear PyGui GUI
Imports `core/` directly — no subprocess calls to `inspect.py`.

**Layout:**
```
┌─────────────────────────────────────────────────────────────────┐
│  [Open File...]  flight_0.bin              [Reload]             │
├───────────────┬─────────────────────────────────────────────────┤
│  LEFT PANEL   │  RECORD TABLE (main area)                       │
│               │  seq │ time_ms │ type │ fields...               │
│  Type filter  │  ─────────────────────────────────────────────  │
│  ☑ IMU        │  rows...                                        │
│  ☑ BARO       │                                                 │
│  ☑ ADXL       │                                                 │
│  ☑ GPS_POS    ├─────────────────────────────────────────────────┤
│  ☑ GPS_VEL    │  DETAIL PANEL (click any row)                   │
│  ☑ GPS_ACC    │  Decoded view of selected record:               │
│  ☑ GPS_LOW    │  - Raw field values                             │
│  ☑ BOARD      │  - Bit-field breakdown (flags, state, etc.)     │
│               ├─────────────────────────────────────────────────┤
│  Order:       │  INFO BAR                                        │
│  ○ Packet     │  2041 records │ 14 out-of-order │ 41 bad        │
│  ● Chrono     │  Time: 0ms → 101823ms (101.8s)                  │
│               │                                                 │
│  Time filter: │                                                 │
│  [0      ] ms │                                                 │
│  [999999 ] ms │                                                 │
│               │                                                 │
│  Format:      │                                                 │
│  ○ Flat       │                                                 │
│  ● Merged     │                                                 │
│  Align: [imu] │                                                 │
│               │                                                 │
│ [Export CSV]  │                                                 │
└───────────────┴─────────────────────────────────────────────────┘
```

**Interactions:**
- Type checkboxes / order toggle / time filter → filter in-memory, no re-parse
- Click row → detail panel updates with full bit-field breakdown
- Export CSV → calls `exporter.py` with current settings

### `index.html` & `web_app.py` - Web App
A WebAssembly-powered (PyScript) interface designed to be hosted statically on GitHub Pages.
* **Mechanism**: Downloads the Python environment directly into the user's browser. It uses a `<py-config>` fetch block to download the `core/` directory from the repository.
* **Separation of Concerns**: Uses the exact same `core/` files as the CLI and Desktop GUI. No duplicate logic exists.
* **DOM Integration**: Python functions are bound to HTML elements using Pyodide's `create_proxy`. Files are loaded into the browser's virtual filesystem and parsed via `core.parser.parse_file()`.

---

## Data Flow

```
.bin file
    │
    ▼
parser.py ──────────────────────────────────► ParseResult
    │                                          (records list + integrity stats)
    │                                                    │
    ├──────────────────────────────────────────────────► report.py
    │                                                    (ReportData)
    │                                                         │
    │                                                    CLI --info / GUI info bar
    │
    ▼
exporter.py
    (apply type filter → apply time filter → apply ordering → format)
    │
    ├── flat mode   ──► CSV (one row per record)
    └── merged mode ──► CSV (one row per spine-type tick, other streams joined)
```

---

## Key Design Decisions

**Fixed-stride parsing**: Every record is `RECORD_SIZE` bytes. The parser never
guesses boundaries. Unknown type bytes produce a warning and skip forward by one
full stride. This means a single corrupt byte costs exactly one record, not the
rest of the file.

**Schema-driven**: Adding a new sensor type requires touching only `schema.py`.
The parser, exporter, report, CLI help text, and GUI table columns all derive
from the schema automatically.

**Ordering is post-parse**: Both ordering modes parse the entire file first,
then sort (or not). This means ordering mode can be toggled in the GUI without
re-reading the file.

**Decoded columns are additive**: Bit-field decoding produces extra columns
alongside the raw value, never replacing it. `--no-decode` suppresses them.
The raw value is always present for reference.

**`RECORD_SIZE` must match firmware**: Run `Serial.printf("sizeof(Data_t)=%d\n", sizeof(Data_t));`
at startup and set `RECORD_SIZE` in `schema.py` to match. A mismatch causes
every record to be misaligned and the file appears entirely corrupt.

---

## Dependencies

```
dearpygui     # GUI only
pandas        # merged mode only (optional — flat mode uses stdlib csv)
```

Core parser and CLI work with **Python stdlib only** (struct, csv, argparse).
