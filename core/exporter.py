"""
exporter.py — Filtering, ordering, and CSV export for avionics log records.

Provides four public functions:

* ``filter_records``  — apply type / time / sequence filters (ANDed)
* ``apply_ordering``  — sort by timestamp or keep file order
* ``export_flat``     — one CSV row per record, all fields present as columns
* ``export_merged``   — one CSV row per spine-type tick; other types forward-filled

No external dependencies (stdlib only: ``csv``, ``io``, ``sys``).
"""

import csv
import io
import sys
from typing import Dict, IO, List, Optional

from .parser import Record
from .schema import RECORD_TYPES, get_all_field_names, get_all_decoded_column_names


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_records(
    records: List[Record],
    types: Optional[List[str]] = None,
    time_start: Optional[int] = None,
    time_end: Optional[int] = None,
    seq_start: Optional[int] = None,
    seq_end: Optional[int] = None,
) -> List[Record]:
    """
    Apply all active filters to *records* and return the surviving subset.

    All specified filters are ANDed together; unspecified filters (``None``)
    are treated as *pass-all*.

    Parameters
    ----------
    records : List[Record]
        Input records in any order.
    types : list of str, optional
        Whitelist of ``type_name`` strings (e.g. ``['IMU', 'BARO']``).
        ``None`` keeps all types.
    time_start : int, optional
        Keep records with ``time_ms >= time_start``.
    time_end : int, optional
        Keep records with ``time_ms <= time_end``.
    seq_start : int, optional
        Keep records with ``seq >= seq_start``.
    seq_end : int, optional
        Keep records with ``seq <= seq_end``.

    Returns
    -------
    List[Record]
        Filtered list; input order is preserved.
    """
    # Normalise the type whitelist to a frozenset for O(1) lookup
    type_set: Optional[frozenset] = frozenset(types) if types is not None else None

    result: List[Record] = []
    for rec in records:
        if type_set is not None and rec.type_name not in type_set:
            continue
        if time_start is not None and rec.time_ms < time_start:
            continue
        if time_end is not None and rec.time_ms > time_end:
            continue
        if seq_start is not None and rec.seq < seq_start:
            continue
        if seq_end is not None and rec.seq > seq_end:
            continue
        result.append(rec)

    return result


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def apply_ordering(
    records: List[Record],
    order: str = 'packet',
) -> List[Record]:
    """
    Return *records* in the requested order.

    Parameters
    ----------
    records : List[Record]
        Input record list.
    order : {'packet', 'chrono'}
        * ``'packet'`` — return the list as-is (file order).
        * ``'chrono'`` — stable sort by ``time_ms``; ties retain their relative
          file order (guaranteed by Python's ``sorted`` being stable).

    Returns
    -------
    List[Record]
        A new list; the original is never mutated.

    Raises
    ------
    ValueError
        If *order* is not one of the recognised modes.
    """
    if order == 'packet':
        return list(records)  # shallow copy; caller may mutate freely
    elif order == 'chrono':
        return sorted(records, key=lambda r: r.time_ms)
    else:
        raise ValueError(f"Unknown ordering mode {order!r}. Use 'packet' or 'chrono'.")


# ---------------------------------------------------------------------------
# Flat CSV export
# ---------------------------------------------------------------------------

def export_flat(
    records: List[Record],
    out: IO,
    include_decoded: bool = True,
) -> int:
    """
    Write a flat CSV to *out* — one row per record.

    The header is built **once** from the schema so every column is present
    regardless of which record types actually appear in the file.  Fields that
    do not belong to the current record type are written as empty strings.

    Column order
    ------------
    ``seq``, ``time_ms``, ``type``,
    <all raw field names — sorted alphabetically>,
    <all decoded sub-column names — ``fieldname__subfield``>  (if *include_decoded*)

    Parameters
    ----------
    records : List[Record]
        Records to export (already filtered and ordered by the caller).
    out : IO
        Any writable file-like object (``sys.stdout``, an open file, or an
        ``io.StringIO``).
    include_decoded : bool
        When ``True`` (default) the decoded bit-field sub-columns are appended
        after the raw fields.

    Returns
    -------
    int
        Number of data rows written (header row not counted).
    """
    raw_field_cols: List[str] = get_all_field_names()
    decoded_cols: List[str] = get_all_decoded_column_names() if include_decoded else []

    header: List[str] = ['seq', 'time_ms', 'type'] + raw_field_cols + decoded_cols

    writer = csv.DictWriter(out, fieldnames=header, extrasaction='ignore',
                            lineterminator='\n')
    writer.writeheader()

    rows_written: int = 0
    for rec in records:
        row: Dict = {
            'seq':     rec.seq,
            'time_ms': rec.time_ms,
            'type':    rec.type_name,
        }

        # Raw fields — only populate columns that belong to this record type
        for field_name, value in rec.raw_fields.items():
            row[field_name] = value

        # Decoded bit-field sub-columns — already flat {field__subfield: value}
        if include_decoded:
            for col_name, sub_value in rec.decoded_fields.items():
                row[col_name] = sub_value

        writer.writerow(row)
        rows_written += 1

    return rows_written


# ---------------------------------------------------------------------------
# Merged CSV export
# ---------------------------------------------------------------------------

def export_merged(
    records: List[Record],
    align_type: str = 'IMU',
    out: IO = sys.stdout,
    include_decoded: bool = True,
) -> int:
    """
    Write a merged CSV to *out* using *align_type* as the timestamp spine.

    For every spine record the most recent record of every other type whose
    ``time_ms`` is **≤** the spine record's ``time_ms`` is forward-filled into
    the same output row (nearest-prior / last-known-value semantics).

    Records of non-spine types that occurred *after* all spine records are
    silently discarded.

    Column order
    ------------
    ``seq``, ``time_ms``, ``type``,
    then for each type (spine first, others alphabetically):
        <raw fields of that type> [, <decoded sub-columns of that type>]

    Parameters
    ----------
    records : List[Record]
        Records to merge (already filtered and ordered by the caller).
        The list is consumed in its given order; pass ``apply_ordering``
        output if chronological ordering is desired.
    align_type : str
        The ``type_name`` that defines the timestamp axis (e.g. ``'IMU'``).
    out : IO
        Writable file-like object.
    include_decoded : bool
        Append decoded bit-field columns alongside raw values.

    Returns
    -------
    int
        Number of data rows written (header row not counted).
    """
    # ------------------------------------------------------------------ #
    # Split records by type
    # ------------------------------------------------------------------ #
    spine_records: List[Record] = []
    other_records: Dict[str, List[Record]] = {}   # type_name → sorted list

    for rec in records:
        if rec.type_name == align_type:
            spine_records.append(rec)
        else:
            other_records.setdefault(rec.type_name, []).append(rec)

    other_types: List[str] = sorted(other_records.keys())

    # ------------------------------------------------------------------ #
    # Build ordered column map per type
    # ------------------------------------------------------------------ #
    # We need the field names for each type in schema order so columns are
    # predictable.  Build a helper: type_name → [raw_field_names]
    type_field_map: Dict[str, List[str]] = {}
    for tid, tdef in RECORD_TYPES.items():
        type_field_map[tdef['name']] = tdef['fields']

    # ------------------------------------------------------------------ #
    # Build CSV header
    # ------------------------------------------------------------------ #
    header: List[str] = ['seq', 'time_ms', 'type']

    # Spine type columns first, then other types alphabetically
    def _type_columns(type_name: str) -> List[str]:
        """Return raw + (optionally decoded) column names for one type."""
        fields = type_field_map.get(type_name, [])
        cols = [f'{type_name}__{f}' for f in fields]
        if include_decoded:
            # Decoded sub-columns live inside decoded_fields; derive names from
            # get_all_decoded_column_names scoped to this type's fields.
            # We prefix with the type name as well for unambiguity.
            from .schema import BIT_FIELDS
            for f in fields:
                if f in BIT_FIELDS:
                    descriptor = BIT_FIELDS[f]
                    kind = descriptor['type']
                    if kind == 'bitmask':
                        for flag in descriptor['bits'].values():
                            cols.append(f'{type_name}__{f}__{flag}')
                    elif kind == 'bitfield':
                        for sub in descriptor['fields']:
                            cols.append(f'{type_name}__{f}__{sub["name"]}')
                    elif kind == 'enum':
                        cols.append(f'{type_name}__{f}__{f}')
        return cols

    all_type_order = [align_type] + other_types
    for tname in all_type_order:
        header.extend(_type_columns(tname))

    writer = csv.DictWriter(out, fieldnames=header, extrasaction='ignore',
                            lineterminator='\n')
    writer.writeheader()

    # ------------------------------------------------------------------ #
    # Pointer-based forward-fill merge (O(n) per type)
    # ------------------------------------------------------------------ #
    # Index into each other-type list
    other_ptrs: Dict[str, int] = {t: 0 for t in other_types}
    # State: most-recently seen record per other type (None until first match)
    state: Dict[str, Optional[Record]] = {t: None for t in other_types}

    rows_written: int = 0

    for spine_rec in spine_records:
        # Advance pointers for all other types up to the spine timestamp
        for tname in other_types:
            ptr = other_ptrs[tname]
            bucket = other_records[tname]
            while ptr < len(bucket) and bucket[ptr].time_ms <= spine_rec.time_ms:
                state[tname] = bucket[ptr]
                ptr += 1
            other_ptrs[tname] = ptr

        # Build the output row
        row: Dict = {
            'seq':     spine_rec.seq,
            'time_ms': spine_rec.time_ms,
            'type':    spine_rec.type_name,
        }

        # Helper to fill one record's fields into the row with a type prefix
        def _fill_record(rec: Record, tname: str) -> None:
            for fname, val in rec.raw_fields.items():
                row[f'{tname}__{fname}'] = val
            if include_decoded:
                # decoded_fields is already flat: {field__subfield: value}
                for col_name, sub_val in rec.decoded_fields.items():
                    row[f'{tname}__{col_name}'] = sub_val

        _fill_record(spine_rec, align_type)

        for tname in other_types:
            if state[tname] is not None:
                _fill_record(state[tname], tname)  # type: ignore[arg-type]

        writer.writerow(row)
        rows_written += 1

    return rows_written
