"""
report.py — Summary statistics and formatted output for parsed avionics logs.

Consumes a ``ParseResult`` (produced by ``parser.py``) and returns a
``ReportData`` dataclass.  No I/O is performed here; both frontends call
``format_report`` (or build their own view from ``ReportData``) independently.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser import ParseResult, Record


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TypeStats:
    """Per-message-type statistics derived from timestamps of that type."""

    count: int
    avg_rate_hz: float   # count / (overall_duration_sec) — 0.0 when undefined
    min_gap_ms: float    # minimum gap between consecutive records of this type
    max_gap_ms: float    # maximum gap between consecutive records of this type
    avg_gap_ms: float    # mean   gap between consecutive records of this type


@dataclass
class ReportData:
    """Aggregated report produced by ``generate_report``."""

    filename: str
    total_bytes: int
    record_size: int

    good_count: int
    bad_count: int
    unknown_types: dict          # {type_id (int): occurrence_count (int)}
    truncated_tail: bool

    time_start_ms: int           # time_ms of the first good record
    time_end_ms: int             # time_ms of the last  good record
    duration_ms: int             # time_end_ms - time_start_ms

    out_of_order_count: int      # records where time_ms < previous time_ms
    max_jitter_ms: int           # largest absolute backward jump (ms)

    per_type: Dict[str, TypeStats]   # keyed by type_name string


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def generate_report(parse_result: ParseResult, filename: str) -> ReportData:
    """
    Derive all ``ReportData`` fields from a ``ParseResult``.

    Parameters
    ----------
    parse_result : ParseResult
        The result returned by ``parser.parse_file``.
    filename : str
        Human-readable label (typically the basename of the source file).

    Returns
    -------
    ReportData
        Fully populated report; safe to pass to ``format_report`` immediately.

    Notes
    -----
    * Records are examined in the order they appear in the list (file order).
    * Out-of-order detection compares each record's ``time_ms`` to the
      immediately preceding record's ``time_ms``; a strict less-than triggers
      the counter.
    * ``max_jitter_ms`` is the largest *backward* jump seen (positive integer).
    * Per-type rate is ``count / duration_sec`` where ``duration_sec`` is the
      overall file duration.  For a single-record file the rate is 0.0.
    """
    records: List[Record] = parse_result.records

    # ------------------------------------------------------------------ #
    # Basic counts from ParseResult integrity stats
    # ------------------------------------------------------------------ #
    good_count: int = len(records)
    bad_count: int = parse_result.bad_count
    unknown_types: dict = dict(parse_result.unknown_types)
    truncated_tail: bool = parse_result.truncated_tail
    total_bytes: int = parse_result.total_bytes
    from .schema import RECORD_SIZE as _RS
    record_size: int = _RS

    # ------------------------------------------------------------------ #
    # Edge case: no good records at all
    # ------------------------------------------------------------------ #
    if good_count == 0:
        return ReportData(
            filename=filename,
            total_bytes=total_bytes,
            record_size=record_size,
            good_count=0,
            bad_count=bad_count,
            unknown_types=unknown_types,
            truncated_tail=truncated_tail,
            time_start_ms=0,
            time_end_ms=0,
            duration_ms=0,
            out_of_order_count=0,
            max_jitter_ms=0,
            per_type={},
        )

    # ------------------------------------------------------------------ #
    # Time range
    # ------------------------------------------------------------------ #
    time_start_ms: int = records[0].time_ms
    time_end_ms: int = records[-1].time_ms
    duration_ms: int = time_end_ms - time_start_ms

    # ------------------------------------------------------------------ #
    # Out-of-order detection (file order, adjacent comparison)
    # ------------------------------------------------------------------ #
    out_of_order_count: int = 0
    max_jitter_ms: int = 0
    prev_time: int = records[0].time_ms

    for rec in records[1:]:
        if rec.time_ms < prev_time:
            out_of_order_count += 1
            jump: int = prev_time - rec.time_ms
            if jump > max_jitter_ms:
                max_jitter_ms = jump
        prev_time = rec.time_ms

    # ------------------------------------------------------------------ #
    # Per-type stats
    # ------------------------------------------------------------------ #
    # Collect timestamps per type in file order
    type_timestamps: Dict[str, List[int]] = {}
    for rec in records:
        type_timestamps.setdefault(rec.type_name, []).append(rec.time_ms)

    duration_sec: float = duration_ms / 1000.0

    per_type: Dict[str, TypeStats] = {}
    for type_name, timestamps in type_timestamps.items():
        count: int = len(timestamps)

        # Rate: overall count over overall duration (not per-type duration)
        avg_rate_hz: float = (count / duration_sec) if duration_sec > 0.0 else 0.0

        # Inter-record gaps for this type only
        if count >= 2:
            gaps: List[float] = [
                float(timestamps[i + 1] - timestamps[i])
                for i in range(count - 1)
            ]
            min_gap_ms: float = min(gaps)
            max_gap_ms: float = max(gaps)
            avg_gap_ms: float = sum(gaps) / len(gaps)
        else:
            # Single record — gaps are undefined; use 0.0 as sentinel
            min_gap_ms = 0.0
            max_gap_ms = 0.0
            avg_gap_ms = 0.0

        per_type[type_name] = TypeStats(
            count=count,
            avg_rate_hz=avg_rate_hz,
            min_gap_ms=min_gap_ms,
            max_gap_ms=max_gap_ms,
            avg_gap_ms=avg_gap_ms,
        )

    return ReportData(
        filename=filename,
        total_bytes=total_bytes,
        record_size=record_size,
        good_count=good_count,
        bad_count=bad_count,
        unknown_types=unknown_types,
        truncated_tail=truncated_tail,
        time_start_ms=time_start_ms,
        time_end_ms=time_end_ms,
        duration_ms=duration_ms,
        out_of_order_count=out_of_order_count,
        max_jitter_ms=max_jitter_ms,
        per_type=per_type,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_report(report: ReportData) -> str:
    """
    Render a ``ReportData`` to a human-readable plain-text string.

    No external dependencies — uses only f-string formatting and string joins.
    All columns are right/left-aligned with fixed field widths so the output
    looks clean in a monospace terminal or log file.

    Parameters
    ----------
    report : ReportData
        Populated report as returned by ``generate_report``.

    Returns
    -------
    str
        Multi-line string ready for ``print()`` or writing to a file.
    """
    lines: List[str] = []

    # ------------------------------------------------------------------ #
    # File info header
    # ------------------------------------------------------------------ #
    lines.append(
        f"File: {report.filename}  "
        f"({report.total_bytes} bytes, record_size={report.record_size})"
    )

    # ------------------------------------------------------------------ #
    # Record summary
    # ------------------------------------------------------------------ #
    total_records: int = report.good_count + report.bad_count
    bad_pct: float = (
        (report.bad_count / total_records * 100.0) if total_records > 0 else 0.0
    )
    truncated_str: str = "1 truncated tail" if report.truncated_tail else "0 truncated tail"
    lines.append(
        f"Records: {report.good_count} good, "
        f"{report.bad_count} bad ({bad_pct:.1f}%), "
        f"{truncated_str}"
    )

    # Unknown types (if any)
    if report.unknown_types:
        unk_parts = [f"0x{tid:02X}×{cnt}" for tid, cnt in sorted(report.unknown_types.items())]
        lines.append(f"Unknown type IDs: {', '.join(unk_parts)}")

    # ------------------------------------------------------------------ #
    # Time range
    # ------------------------------------------------------------------ #
    duration_sec: float = report.duration_ms / 1000.0
    lines.append(
        f"Time range: {report.time_start_ms}ms -> {report.time_end_ms}ms  "
        f"({duration_sec:.1f} sec)"
    )

    # ------------------------------------------------------------------ #
    # Ordering quality
    # ------------------------------------------------------------------ #
    lines.append(
        f"Ordering: {report.out_of_order_count} out-of-order records "
        f"(max jitter: {report.max_jitter_ms}ms)"
    )

    # ------------------------------------------------------------------ #
    # Per-type table
    # ------------------------------------------------------------------ #
    if report.per_type:
        lines.append("")

        # Column headers + widths
        col_type  = "Type"
        col_count = "Count"
        col_rate  = "Rate(avg)"
        col_min   = "MinGap"
        col_max   = "MaxGap"
        col_avg   = "AvgGap"

        w_type  = max(len(col_type),  max(len(t) for t in report.per_type))
        w_count = max(len(col_count), max(len(str(s.count)) for s in report.per_type.values()))
        w_rate  = len(col_rate)
        w_min   = len(col_min)
        w_max   = len(col_max)
        w_avg   = len(col_avg)

        header = (
            f"{col_type:<{w_type}}  "
            f"{col_count:>{w_count}}  "
            f"{col_rate:>{w_rate}}  "
            f"{col_min:>{w_min}}  "
            f"{col_max:>{w_max}}  "
            f"{col_avg:>{w_avg}}"
        )
        sep_len = len(header)
        lines.append(header)
        lines.append("-" * sep_len)

        for type_name, stats in sorted(report.per_type.items()):
            rate_str = f"{stats.avg_rate_hz:.1f} Hz"
            min_str  = f"{stats.min_gap_ms:.0f} ms"
            max_str  = f"{stats.max_gap_ms:.0f} ms"
            avg_str  = f"{stats.avg_gap_ms:.0f} ms"

            lines.append(
                f"{type_name:<{w_type}}  "
                f"{stats.count:>{w_count}}  "
                f"{rate_str:>{w_rate}}  "
                f"{min_str:>{w_min}}  "
                f"{max_str:>{w_max}}  "
                f"{avg_str:>{w_avg}}"
            )

    return "\n".join(lines)
