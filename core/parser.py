"""
parser.py — Binary log file reader for the avionics flight computer.

Provides two parsing strategies:
  - 'stride' (default) — fixed-stride sequential read; fast and strict.
  - 'scan'   (recovery) — sliding-window byte-by-byte search; tolerant of
    corruption and misaligned data.

Use ``detect_record_size`` to verify that RECORD_SIZE matches the firmware
build before committing to a full parse.
"""

import os
import struct
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .schema import (
    RECORD_HEADER_FMT,
    RECORD_HEADER_SIZE,
    RECORD_SIZE,
    RECORD_TYPES,
    decode_bitfields,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Maximum plausible timestamp used during scan-mode validation (1 hour in ms)
_MAX_PLAUSIBLE_TIME_MS: int = 3_600_000


@dataclass
class Record:
    """A single fully-decoded avionics log record."""

    seq: int
    """Zero-based index of this record within the file (packet order)."""

    type_id: int
    """Raw type byte from the record header."""

    type_name: str
    """Human-readable name looked up from RECORD_TYPES."""

    time_ms: int
    """Millisecond timestamp from the record header."""

    raw_fields: Dict[str, int]
    """Mapping of field name → raw value as unpacked from the binary payload."""

    decoded_fields: Dict[str, object]
    """
    Flat mapping of sub-field name → decoded value for every field that has
    an entry in BIT_FIELDS (e.g. bitmask flags, bitfield sub-values, enums).
    Keys are prefixed as ``'original_field__sub_field'`` for bitmask/bitfield
    types and ``'original_field__original_field'`` for enum types.
    """

    raw_bytes: bytes
    """The complete RECORD_SIZE bytes for this record, as read from disk."""


@dataclass
class ParseResult:
    """Aggregate result returned by ``parse_file``."""

    records: List[Record] = field(default_factory=list)
    """All successfully parsed records in file order."""

    total_bytes: int = 0
    """Total number of bytes in the source file."""

    good_count: int = 0
    """Number of records successfully parsed."""

    bad_count: int = 0
    """Number of chunks skipped due to unknown type bytes."""

    truncated_tail: bool = False
    """
    True when the file ended with fewer bytes than RECORD_SIZE, indicating
    a partial write (e.g. power-loss during logging).
    """

    unknown_types: Dict[int, int] = field(default_factory=dict)
    """
    Mapping of unrecognised type byte → how many times it was encountered.
    Populated in stride mode and scan mode alike.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_chunk(
    chunk: bytes,
    seq: int,
    strict: bool,
    unknown_types: Dict[int, int],
) -> Optional[Record]:
    """
    Attempt to parse a single RECORD_SIZE-byte chunk into a ``Record``.

    Parameters
    ----------
    chunk : bytes
        Exactly RECORD_SIZE bytes.
    seq : int
        Packet sequence index (zero-based position in file).
    strict : bool
        If True, raise ``ValueError`` on unknown type_id rather than warning.
    unknown_types : dict
        Mutable dict updated in-place when an unknown type_id is encountered.

    Returns
    -------
    Record or None
        Parsed record on success; None on unknown type (non-strict mode).
    """
    type_id, time_ms = struct.unpack_from(RECORD_HEADER_FMT, chunk, 0)

    if type_id not in RECORD_TYPES:
        unknown_types[type_id] = unknown_types.get(type_id, 0) + 1
        msg = (
            f"[seq {seq}] Unknown type_id=0x{type_id:02X} at file offset "
            f"{seq * RECORD_SIZE} — skipping chunk."
        )
        if strict:
            raise ValueError(msg)
        warnings.warn(msg, stacklevel=3)
        return None

    spec = RECORD_TYPES[type_id]
    fmt: str = spec['fmt']
    names: List[str] = spec['fields']

    payload = chunk[RECORD_HEADER_SIZE: RECORD_HEADER_SIZE + struct.calcsize(fmt)]
    values = struct.unpack(fmt, payload)

    raw_fields: Dict[str, int] = dict(zip(names, values))

    # Decode bit-packed fields into expanded sub-field dict
    decoded_fields: Dict[str, object] = {}
    for fname, fval in raw_fields.items():
        sub = decode_bitfields(fname, fval)
        for sub_name, sub_val in sub.items():
            # Flatten into 'original_field__sub_field' namespace
            decoded_fields[f'{fname}__{sub_name}'] = sub_val

    return Record(
        seq=seq,
        type_id=type_id,
        type_name=spec['name'],
        time_ms=time_ms,
        raw_fields=raw_fields,
        decoded_fields=decoded_fields,
        raw_bytes=bytes(chunk),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(
    path: str,
    mode: str = 'stride',
    strict: bool = False,
    record_size_override: Optional[int] = None,
) -> ParseResult:
    """
    Parse a binary avionics log file into a list of ``Record`` objects.

    Parameters
    ----------
    path : str
        Absolute or relative path to the binary log file.
    mode : str
        ``'stride'`` (default) — read sequential fixed-size chunks.
        ``'scan'``   — sliding-window recovery mode for corrupted files.
    strict : bool
        If True, raise ``ValueError`` on the first unrecognised type byte.
        If False (default), emit a warning and continue.
    record_size_override : int, optional
        Override RECORD_SIZE for this parse (e.g. when ``detect_record_size``
        suggests a different stride).  Affects stride mode only.

    Returns
    -------
    ParseResult
        Populated result object; see ``ParseResult`` for field descriptions.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        In strict mode, on the first unknown type_id.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Log file not found: {path!r}")

    stride = record_size_override if record_size_override is not None else RECORD_SIZE
    result = ParseResult()
    result.total_bytes = os.path.getsize(path)

    if mode == 'stride':
        _parse_stride(path, stride, strict, result)
    elif mode == 'scan':
        _parse_scan(path, stride, strict, result)
    else:
        raise ValueError(f"Unknown mode {mode!r}. Choose 'stride' or 'scan'.")

    return result


def _parse_stride(
    path: str,
    stride: int,
    strict: bool,
    result: ParseResult,
) -> None:
    """
    Fixed-stride sequential parse.  Fastest mode; assumes the file is intact.

    Modifies *result* in-place.
    """
    seq = 0
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(stride)
            if len(chunk) < stride:
                if chunk:  # partial record at EOF
                    result.truncated_tail = True
                break

            record = _parse_chunk(chunk, seq, strict, result.unknown_types)
            if record is None:
                result.bad_count += 1
            else:
                result.records.append(record)
                result.good_count += 1
            seq += 1


def _parse_scan(
    path: str,
    stride: int,
    strict: bool,
    result: ParseResult,
) -> None:
    """
    Sliding-window recovery parse.  Slower but tolerant of byte-level
    corruption or unexpected padding.

    A position is accepted as a record start when:
      1. The byte at that position is a recognised type_id.
      2. The four bytes at positions [pos+1 .. pos+4] decode to a uint32
         timestamp < ``_MAX_PLAUSIBLE_TIME_MS`` (1 hour).

    On a successful match the window advances by *stride*; otherwise by 1.

    Modifies *result* in-place.
    """
    with open(path, 'rb') as fh:
        buf = fh.read()

    total = len(buf)
    pos = 0
    seq = 0

    while pos + stride <= total:
        type_id = buf[pos]

        # Quick plausibility gate before the more expensive unpack
        if type_id in RECORD_TYPES and pos + RECORD_HEADER_SIZE <= total:
            # Decode timestamp from bytes [pos+1 .. pos+4]
            (time_ms,) = struct.unpack_from('<I', buf, pos + 1)
            if time_ms < _MAX_PLAUSIBLE_TIME_MS:
                chunk = buf[pos: pos + stride]
                record = _parse_chunk(chunk, seq, strict, result.unknown_types)
                if record is not None:
                    result.records.append(record)
                    result.good_count += 1
                    pos += stride
                    seq += 1
                    continue
                else:
                    result.bad_count += 1

        # No valid record found at this position — advance by one byte
        pos += 1

    # Check for a truncated tail (bytes remaining that are fewer than stride)
    remaining = total - pos
    if 0 < remaining < stride:
        result.truncated_tail = True


def detect_record_size(path: str) -> int:
    """
    Heuristic to detect the record stride that best matches the binary file.

    Tries candidate RECORD_SIZE values from 10 to 64 bytes inclusive.  For
    each candidate the first up-to-100 fixed-stride chunks are examined and
    the fraction with a valid type_id byte is computed.  The candidate with
    the highest hit-rate is returned.

    This is useful for verifying that :data:`RECORD_SIZE` in *schema.py*
    matches the ``sizeof(Data_t)`` from the actual firmware build being
    analysed.

    Parameters
    ----------
    path : str
        Absolute or relative path to the binary log file.

    Returns
    -------
    int
        Candidate record size with the highest hit-rate.  Falls back to the
        schema-defined :data:`RECORD_SIZE` if no candidate scores above zero.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Log file not found: {path!r}")

    MAX_CHUNKS = 100
    valid_type_ids = set(RECORD_TYPES.keys())

    best_size = RECORD_SIZE
    best_hits = -1

    with open(path, 'rb') as fh:
        preamble = fh.read(64 * MAX_CHUNKS)  # read enough for any candidate

    for candidate in range(10, 65):
        hits = 0
        chunks_examined = 0
        offset = 0

        while chunks_examined < MAX_CHUNKS and offset + candidate <= len(preamble):
            type_id = preamble[offset]
            if type_id in valid_type_ids:
                hits += 1
            chunks_examined += 1
            offset += candidate

        if chunks_examined == 0:
            continue

        hit_rate = hits / chunks_examined
        # Prefer a higher hit rate; on a tie prefer the schema default
        if hit_rate > best_hits or (
            hit_rate == best_hits and candidate == RECORD_SIZE
        ):
            best_hits = hit_rate
            best_size = candidate

    return best_size
