#!/usr/bin/env python3
"""
bpi.py — CLI frontend for the avionics binary log inspector.

Usage:
    python bpi.py <file.bin> [options]

Run with --help for full option list.
"""

import argparse
import sys
import os

# ---------------------------------------------------------------------------
# Allow running from the project root without installing as a package
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

from core.schema import RECORD_TYPES, RECORD_SIZE
from core.parser import parse_file, detect_record_size
from core.report import generate_report, format_report
from core.exporter import filter_records, apply_ordering, export_flat, export_merged


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bpi.py",
        description="Avionics binary log inspector — decode, filter, and export flight data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bpi.py flight_0.bin --info
  python bpi.py flight_0.bin --raw 50
  python bpi.py flight_0.bin --csv imu baro --order chrono --out flight.csv
  python bpi.py flight_0.bin --csv imu baro gps_pos --format merged --align imu
  python bpi.py flight_0.bin --info --csv board --order chrono --time-start 5000 --time-end 30000
        """,
    )

    p.add_argument("file", help="Path to the .bin log file.")

    # ---- inspection ----
    p.add_argument(
        "--info",
        action="store_true",
        help="Print summary: record counts, rates, gaps, ordering quality.",
    )
    p.add_argument(
        "--raw",
        metavar="N",
        nargs="?",
        const=20,
        type=int,
        help="Hex + decoded dump of first N records (default 20 if flag given without N).",
    )
    p.add_argument(
        "--detect-size",
        action="store_true",
        help="Heuristically detect RECORD_SIZE from file content (for diagnosing schema mismatch).",
    )

    # ---- export ----
    p.add_argument(
        "--csv",
        metavar="TYPE",
        nargs="*",
        help=(
            "Export CSV. Optionally specify sensor types to include "
            f"(choices: {', '.join(t['name'].lower() for t in RECORD_TYPES.values())}). "
            "Default: all types."
        ),
    )
    p.add_argument(
        "--format",
        choices=["flat", "merged"],
        default="flat",
        help="CSV format. 'flat': one row per record. 'merged': timestamp-aligned. (default: flat)",
    )
    p.add_argument(
        "--align",
        metavar="TYPE",
        default="imu",
        help="Spine type for merged mode (default: imu).",
    )
    p.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Output CSV path. Default: <input_stem>.csv (stdout if '-').",
    )

    # ---- filtering ----
    p.add_argument("--time-start", metavar="MS", type=int, default=None, help="Discard records before this timestamp (ms).")
    p.add_argument("--time-end",   metavar="MS", type=int, default=None, help="Discard records after this timestamp (ms).")
    p.add_argument("--seq-start",  metavar="N",  type=int, default=None, help="Discard records before this sequence number.")
    p.add_argument("--seq-end",    metavar="N",  type=int, default=None, help="Discard records after this sequence number.")

    # ---- ordering ----
    p.add_argument(
        "--order",
        choices=["packet", "chrono"],
        default="packet",
        help="Record ordering. 'packet': file order. 'chrono': sorted by timestamp. (default: packet)",
    )

    # ---- decoding ----
    p.add_argument(
        "--no-decode",
        action="store_true",
        help="Omit decoded bit-field columns. Output raw values only.",
    )

    # ---- recovery ----
    p.add_argument(
        "--recover",
        action="store_true",
        help="Use scan mode for damaged files (slow, byte-by-byte search).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Abort on first bad/unknown record. Default: warn and skip.",
    )
    p.add_argument(
        "--record-size",
        metavar="N",
        type=int,
        default=None,
        help=f"Override RECORD_SIZE (default from schema: {RECORD_SIZE} bytes).",
    )

    return p


# ---------------------------------------------------------------------------
# Raw dump
# ---------------------------------------------------------------------------

def print_raw(records, n: int, include_decoded: bool) -> None:
    """Print a formatted hex + decoded dump of the first N records."""
    for rec in records[:n]:
        hex_str = rec.raw_bytes.hex(" ").upper()
        print(f"\n{'-'*70}")
        print(f"  seq={rec.seq:>5}  time={rec.time_ms:>10} ms  type=0x{rec.type_id:02X} ({rec.type_name})")
        print(f"  hex: {hex_str}")
        print(f"  raw fields:")
        for k, v in rec.raw_fields.items():
            print(f"         {k:<20} = {v}")
        if include_decoded and rec.decoded_fields:
            print(f"  decoded:")
            for k, v in rec.decoded_fields.items():
                print(f"         {k:<30} = {v}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # ---- validate file ----
    if not os.path.isfile(args.file):
        print(f"ERROR: File not found: {args.file}", file=sys.stderr)
        return 1

    # ---- detect size (optional diagnostic) ----
    if args.detect_size:
        detected = detect_record_size(args.file)
        print(f"Detected RECORD_SIZE: {detected} bytes  (schema default: {RECORD_SIZE})")
        if detected != RECORD_SIZE:
            print(
                f"WARNING: Detected size ({detected}) != schema size ({RECORD_SIZE}). "
                "Pass --record-size to override.",
                file=sys.stderr,
            )

    # ---- parse ----
    mode = "scan" if args.recover else "stride"
    parse_result = parse_file(
        args.file,
        mode=mode,
        strict=args.strict,
        record_size_override=args.record_size,
    )

    if parse_result.good_count == 0 and not args.detect_size:
        print(
            "ERROR: No valid records found. "
            "Check RECORD_SIZE with --detect-size, or try --recover for damaged files.",
            file=sys.stderr,
        )
        return 1

    # ---- info ----
    if args.info or (args.csv is None and args.raw is None and not args.detect_size):
        report = generate_report(parse_result, filename=os.path.basename(args.file))
        print(format_report(report))

    # ---- raw dump ----
    if args.raw is not None:
        include_decoded = not args.no_decode
        print_raw(parse_result.records, args.raw, include_decoded)

    # ---- CSV export ----
    if args.csv is not None:
        # Normalise requested types to upper-case names
        valid_names = {t["name"].upper() for t in RECORD_TYPES.values()}
        if args.csv:  # non-empty list means the user specified types
            requested = [t.upper() for t in args.csv]
            invalid = [t for t in requested if t not in valid_names]
            if invalid:
                print(f"ERROR: Unknown type(s): {', '.join(invalid)}", file=sys.stderr)
                print(f"       Valid types: {', '.join(sorted(valid_names))}", file=sys.stderr)
                return 1
            type_filter = requested
        else:
            type_filter = None  # all types

        # Filter
        records = filter_records(
            parse_result.records,
            types=type_filter,
            time_start=args.time_start,
            time_end=args.time_end,
            seq_start=args.seq_start,
            seq_end=args.seq_end,
        )

        # Order
        records = apply_ordering(records, order=args.order)

        if not records:
            print("WARNING: No records remain after filtering.", file=sys.stderr)
            return 0

        # Determine output destination
        if args.out == "-":
            out_fh = sys.stdout
            close_out = False
        else:
            if args.out:
                out_path = args.out
            else:
                stem = os.path.splitext(args.file)[0]
                out_path = stem + ".csv"
            out_fh = open(out_path, "w", newline="", encoding="utf-8")
            close_out = True

        include_decoded = not args.no_decode

        try:
            if args.format == "flat":
                n_rows = export_flat(records, out=out_fh, include_decoded=include_decoded)
            else:
                n_rows = export_merged(
                    records,
                    align_type=args.align.upper(),
                    out=out_fh,
                    include_decoded=include_decoded,
                )
        finally:
            if close_out:
                out_fh.close()

        if close_out:
            print(f"Exported {n_rows} rows -> {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
