#!/usr/bin/env python3
"""
gen_test_binary.py — Generate a synthetic .bin log file for testing the inspector.

Writes records matching the firmware Data_t layout so the parser can be
validated without needing a real flight.

Usage:
    python tests/gen_test_binary.py [output_path] [--records N] [--corrupt]

Options:
    output_path     Where to write the file (default: tests/test_flight.bin)
    --records N     Total number of records to generate (default: 500)
    --corrupt       Inject ~5% bad bytes to test recovery/skip logic
    --out-of-order  Inject ~3% records with timestamps slightly in the past
"""

import struct
import random
import math
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import RECORD_TYPES, RECORD_HEADER_FMT, RECORD_SIZE, PAYLOAD_SIZE


# ---------------------------------------------------------------------------
# Synthetic sensor data generators
# ---------------------------------------------------------------------------

def make_imu(t_ms: int, seq: int) -> bytes:
    """Simulate IMU on a rocket: high-g boost then coast."""
    phase = t_ms / 1000.0
    # simulate ~4g boost for first 3 seconds, then free-fall ish
    az = int(-16384 + 8192 * max(0.0, 1.0 - phase / 3.0))  # roughly -1g to +1g
    ax = int(200 * math.sin(phase * 2.1))
    ay = int(200 * math.cos(phase * 1.7))
    gx = int(500 * math.sin(phase * 0.8))
    gy = int(300 * math.sin(phase * 1.2 + 0.5))
    gz = int(100 * math.cos(phase * 0.5))
    fields = struct.pack("<hhhhhh", ax, ay, az, gx, gy, gz)
    return _pack_record(0x01, t_ms, fields)


def make_baro(t_ms: int) -> bytes:
    phase = t_ms / 1000.0
    # pressure drops with altitude (simplified), peaks at ~5s
    altitude_m = max(0.0, 3000 * math.sin(math.pi * min(phase, 20.0) / 20.0))
    pressure = 101325.0 * math.exp(-altitude_m / 8500.0) / 100.0  # hPa
    temp = 25.0 - altitude_m * 0.0065
    fields = struct.pack("<ff", pressure, temp)
    return _pack_record(0x02, t_ms, fields)


def make_adxl(t_ms: int) -> bytes:
    """High-g accelerometer (ADS1115 based), same axes as IMU."""
    phase = t_ms / 1000.0
    az = int(-16384 + 8000 * max(0.0, 1.0 - phase / 3.0))
    ax = int(150 * math.sin(phase * 2.1))
    ay = int(150 * math.cos(phase * 1.7))
    fields = struct.pack("<hhh", ax, ay, az)
    return _pack_record(0x03, t_ms, fields)


def make_gps_pos(t_ms: int) -> bytes:
    phase = t_ms / 1000.0
    lat = int((28.6139 + phase * 0.00001) * 1e7)   # Delhi launch site, drifting slightly
    lon = int((77.2090 + phase * 0.000005) * 1e7)
    alt = int(max(0.0, 3000000 * math.sin(math.pi * min(phase, 20.0) / 20.0)))  # mm
    fields = struct.pack("<iii", lat, lon, alt)
    return _pack_record(0x04, t_ms, fields)


def make_gps_vel(t_ms: int) -> bytes:
    phase = t_ms / 1000.0
    vup   = int(200000 * math.cos(math.pi * min(phase, 20.0) / 20.0))  # mm/s vertical
    gspd  = int(5000 + 1000 * math.sin(phase))
    heading = int(180 * 1e5)  # constant heading, 1e-5 deg
    fields = struct.pack("<iii", gspd, vup, heading)
    return _pack_record(0x05, t_ms, fields)


def make_gps_acc(t_ms: int) -> bytes:
    fields = struct.pack("<III", 1500, 2000, 50000)  # mm, mm, 1e-5 deg
    return _pack_record(0x06, t_ms, fields)


def make_gps_low(t_ms: int) -> bytes:
    utc = 120000000 + t_ms * 1000  # fake iTOW in ms
    hmsl = int(max(0, 3000000 * math.sin(math.pi * min(t_ms / 1000, 20.0) / 20.0)))
    num_sv = min(12, 4 + int(t_ms / 5000))
    valid   = 0b111   # date+time+fully resolved
    fix     = 3       # 3D fix
    flags   = 0b01    # gnssFixOK
    fields = struct.pack("<IiBBBB", utc, hmsl, num_sv, valid, fix, flags)
    return _pack_record(0x07, t_ms, fields)


def make_board(t_ms: int) -> bytes:
    phase = t_ms / 1000.0
    cmd         = 0
    cmd_param   = 0
    v_batt      = 120          # raw ADC-ish value
    # state: flight_state in bits[7:5], numSV in bits[4:0]
    flight_state = 2 if phase < 3 else (3 if phase < 10 else (4 if phase < 12 else 5))
    num_sv       = min(12, 4 + int(phase / 5))
    state        = ((flight_state & 0x07) << 5) | (num_sv & 0x1F)
    # error_code: everything nominal except simulate a brief BARO error early on
    baro_err    = 1 if phase < 0.5 else 0
    error_code  = baro_err << 1
    flags       = 0b00000100 # LOG_ENABLED
    pyro_state  = 0b00000101   # pyro1 continuity + triggered after boost
    rssi        = 200
    fields = struct.pack("<BBBBBBBB", cmd, cmd_param, v_batt, state, error_code, flags, pyro_state, rssi)
    return _pack_record(0x08, t_ms, fields)


# ---------------------------------------------------------------------------
# Packing helper
# ---------------------------------------------------------------------------

def _pack_record(type_id: int, time_ms: int, payload: bytes) -> bytes:
    """Pack a complete record: header + payload, zero-padded to RECORD_SIZE."""
    header = struct.pack(RECORD_HEADER_FMT, type_id, time_ms)
    # Pad payload to PAYLOAD_SIZE
    padded = payload + b"\x00" * (PAYLOAD_SIZE - len(payload))
    return header + padded


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate(
    output_path: str,
    total_records: int = 500,
    corrupt: bool = False,
    out_of_order: bool = False,
) -> None:
    records: list[bytes] = []

    # Build a realistic mix: mostly IMU, then BARO/ADXL, then slow GPS+BOARD
    # Interleave them at rough proportions
    t_ms = 0
    imu_tick  = 5     # 200 Hz
    baro_tick = 10    # 100 Hz
    adxl_tick = 10    # 100 Hz
    gps_tick  = 100   # 10 Hz
    board_tick = 100

    next_times = {
        "imu":   0,
        "baro":  0,
        "adxl":  2,   # offset slightly so mixed order is realistic
        "gps":   50,
        "board": 75,
    }

    count = 0
    while count < total_records:
        # Find which sensor fires next
        sensor = min(next_times, key=next_times.get)
        t_ms = next_times[sensor]

        if sensor == "imu":
            records.append(make_imu(t_ms, count))
            next_times["imu"] += imu_tick
        elif sensor == "baro":
            records.append(make_baro(t_ms))
            next_times["baro"] += baro_tick
        elif sensor == "adxl":
            records.append(make_adxl(t_ms))
            next_times["adxl"] += adxl_tick
        elif sensor == "gps":
            # GPS emits 4 record types per cycle
            for fn in [make_gps_pos, make_gps_vel, make_gps_acc, make_gps_low]:
                if count < total_records:
                    records.append(fn(t_ms))
                    count += 1
            next_times["gps"] += gps_tick
            continue
        elif sensor == "board":
            records.append(make_board(t_ms))
            next_times["board"] += board_tick

        count += 1

    # Inject out-of-order jitter (~3% of records)
    if out_of_order:
        n_jitter = max(1, int(len(records) * 0.03))
        for _ in range(n_jitter):
            i = random.randint(5, len(records) - 1)
            # Swap record i with record i-3 (puts it slightly out of order)
            j = max(0, i - 3)
            records[i], records[j] = records[j], records[i]

    raw = b"".join(records)

    # Inject corruption (~5% byte flips)
    if corrupt:
        raw = bytearray(raw)
        n_corrupt = max(1, int(len(raw) * 0.05))
        for _ in range(n_corrupt):
            idx = random.randint(0, len(raw) - 1)
            raw[idx] = random.randint(0, 255)
        raw = bytes(raw)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(raw)

    size_kb = len(raw) / 1024
    print(f"Generated {len(records)} records ({size_kb:.1f} KB) -> {output_path}")
    if corrupt:
        print("  + corruption injected (~5% byte flips)")
    if out_of_order:
        print("  + out-of-order jitter injected (~3% record swaps)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Generate a synthetic avionics .bin log for testing.")
    p.add_argument("output", nargs="?", default="tests/test_flight.bin", help="Output .bin path.")
    p.add_argument("--records", type=int, default=500, help="Number of records to generate.")
    p.add_argument("--corrupt", action="store_true", help="Inject ~5%% byte corruption.")
    p.add_argument("--out-of-order", action="store_true", help="Inject ~3%% record order jitter.")
    args = p.parse_args()

    generate(
        output_path=args.output,
        total_records=args.records,
        corrupt=args.corrupt,
        out_of_order=args.out_of_order,
    )
