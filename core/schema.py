"""
schema.py — Binary format definition for the avionics flight computer log.

This is the SINGLE SOURCE OF TRUTH for the binary layout.
When firmware Data_t union gains a new member, add an entry to RECORD_TYPES here.
"""
import struct
from typing import Dict, Any

# ---------------------------------------------------------------------------
# Record layout constants
# ---------------------------------------------------------------------------

# Little-endian: uint8 type_id + uint32 time_ms = 5 bytes
RECORD_HEADER_FMT: str = '<BI'
RECORD_HEADER_SIZE: int = struct.calcsize(RECORD_HEADER_FMT)  # 5

# sizeof(Data_t) on the Teensy — full fixed stride per record
# (header + union payload, including any alignment padding)
RECORD_SIZE: int = 17

# Bytes available for the union payload after the header
PAYLOAD_SIZE: int = RECORD_SIZE - RECORD_HEADER_SIZE  # 12

# ---------------------------------------------------------------------------
# Record type registry
# ---------------------------------------------------------------------------
# Keys are the type_id byte written by firmware.
# 'fmt'    — struct format string for the *payload only* (little-endian).
#            Only covers the bytes actively used by that record type;
#            trailing padding up to PAYLOAD_SIZE is intentionally ignored.
# 'fields' — ordered list of field names matching 'fmt' groups.
RECORD_TYPES: Dict[int, Dict[str, Any]] = {
    0x01: {
        'name':   'IMU',
        'fmt':    '<hhhhhh',          # 6 × int16  = 12 bytes
        'fields': ['ax', 'ay', 'az', 'gx', 'gy', 'gz'],
    },
    0x02: {
        'name':   'BARO',
        'fmt':    '<ff',              # 2 × float32 = 8 bytes
        'fields': ['pressure', 'temp'],
    },
    0x03: {
        'name':   'ADXL',
        'fmt':    '<hhh',             # 3 × int16  = 6 bytes
        'fields': ['ax', 'ay', 'az'],
    },
    0x04: {
        'name':   'GPS_POS',
        'fmt':    '<iii',             # 3 × int32  = 12 bytes
        'fields': ['lat', 'lon', 'gps_alt'],
    },
    0x05: {
        'name':   'GPS_VEL',
        'fmt':    '<iii',             # 3 × int32  = 12 bytes
        'fields': ['ground_vel', 'vert_vel', 'heading'],
    },
    0x06: {
        'name':   'GPS_ACC',
        'fmt':    '<III',             # 3 × uint32 = 12 bytes
        'fields': ['hAcc', 'vAcc', 'headAcc'],
    },
    0x07: {
        'name':   'GPS_LOW',
        'fmt':    '<IiBBBB',          # uint32 + int32 + 4 × uint8 = 12 bytes
        'fields': ['UTC_time', 'hMSL', 'numSV', 'valid', 'fixType', 'flags'],
    },
    0x08: {
        'name':   'BOARD',
        'fmt':    '<BBBBBB',          # 6 × uint8 = 6 bytes
        'fields': ['temp', 'v_batt', 'state', 'error_code', 'pyro_state', 'rssi'],
    },
}

# ---------------------------------------------------------------------------
# Bit-field descriptors
# ---------------------------------------------------------------------------
# Three descriptor types are supported:
#
#   'bitmask'  — each bit is an independent boolean flag.
#                {'type': 'bitmask', 'bits': {bit_index: flag_name}}
#
#   'bitfield' — the byte is split into named sub-fields extracted by bit range.
#                {'type': 'bitfield',
#                 'fields': [{'name': str, 'msb': int, 'lsb': int,
#                             'enum': {int: str}  ← optional}]}
#
#   'enum'     — the whole byte maps to a symbolic name.
#                {'type': 'enum', 'values': {int: str}}
BIT_FIELDS: Dict[str, Dict[str, Any]] = {
    # BOARD.error_code — each bit signals a distinct fault / status flag
    'error_code': {
        'type': 'bitmask',
        'bits': {
            0: 'IMU_ERROR',
            1: 'BARO_ERROR',
            2: 'GPS_ERROR',
            3: 'GSM_ERROR',
            4: 'SD_ERROR',
            5: 'LOG_ENABLED',
            6: 'CMD_RECEIVED',
            7: 'ACK_RECEIVED',
        },
    },

    # BOARD.state — upper 3 bits = flight state enum; lower 5 bits = num_sv
    'state': {
        'type': 'bitfield',
        'fields': [
            {
                'name': 'flight_state',
                'msb': 7,
                'lsb': 5,
                'enum': {
                    0: 'PAD',
                    1: 'ARMED',
                    2: 'BOOST',
                    3: 'COAST',
                    4: 'APOGEE',
                    5: 'DESCENT',
                    6: 'LANDED',
                    7: 'ERROR',
                },
            },
            {
                'name': 'num_sv',
                'msb': 4,
                'lsb': 0,
                # No enum — raw numeric value used directly
            },
        ],
    },

    # BOARD.pyro_state — continuity and trigger bits for four pyro channels
    'pyro_state': {
        'type': 'bitmask',
        'bits': {
            0: 'PYRO1_CONT',
            1: 'PYRO1_TRIG',
            2: 'PYRO2_CONT',
            3: 'PYRO2_TRIG',
            4: 'PYRO3_CONT',
            5: 'PYRO3_TRIG',
            6: 'PYRO4_CONT',
            7: 'PYRO4_TRIG',
        },
    },

    # GPS_LOW.valid — u-blox validity flags
    'valid': {
        'type': 'bitmask',
        'bits': {
            0: 'DATE_VALID',
            1: 'TIME_VALID',
            2: 'FULLY_RESOLVED',
        },
    },

    # GPS_LOW.fixType — u-blox fix type enum
    'fixType': {
        'type': 'enum',
        'values': {
            0: 'NO_FIX',
            1: 'DEAD_RECKONING',
            2: '2D_FIX',
            3: '3D_FIX',
            4: 'GNSS+DR',
            5: 'TIME_ONLY',
        },
    },

    # GPS_LOW.flags — u-blox fix status flags
    'flags': {
        'type': 'bitmask',
        'bits': {
            0: 'GNSS_FIX_OK',
            1: 'DIFF_SOLN',
        },
    },
}


# ---------------------------------------------------------------------------
# Bit-field decode helper
# ---------------------------------------------------------------------------

def decode_bitfields(field_name: str, raw_value: int) -> Dict[str, Any]:
    """
    Decode a packed field into its component parts.

    Parameters
    ----------
    field_name : str
        The name of the field as listed in a record type's ``'fields'`` list.
    raw_value : int
        The raw integer value read from the binary payload.

    Returns
    -------
    Dict[str, Any]
        A flat dict mapping sub-field name → decoded value.
        For *bitmask* descriptors each key is a flag name and the value is a
        bool.
        For *bitfield* descriptors each key is a sub-field name and the value
        is the extracted integer (or its enum string if an ``'enum'`` mapping
        is present).
        For *enum* descriptors the single key is ``field_name`` and the value
        is the symbolic string (or the raw integer if unmapped).
        Returns an **empty dict** if ``field_name`` is not in BIT_FIELDS.
    """
    if field_name not in BIT_FIELDS:
        return {}

    descriptor = BIT_FIELDS[field_name]
    kind = descriptor['type']
    result: Dict[str, Any] = {}

    if kind == 'bitmask':
        for bit_index, flag_name in descriptor['bits'].items():
            result[flag_name] = bool(raw_value & (1 << bit_index))

    elif kind == 'bitfield':
        for sub in descriptor['fields']:
            msb: int = sub['msb']
            lsb: int = sub['lsb']
            # Build a mask for [msb:lsb] inclusive, then shift down
            mask = (1 << (msb - lsb + 1)) - 1
            extracted = (raw_value >> lsb) & mask
            enum_map = sub.get('enum')
            if enum_map is not None:
                result[sub['name']] = enum_map.get(extracted, extracted)
            else:
                result[sub['name']] = extracted

    elif kind == 'enum':
        result[field_name] = descriptor['values'].get(raw_value, raw_value)

    return result


# ---------------------------------------------------------------------------
# Column-name helpers
# ---------------------------------------------------------------------------

def get_all_field_names() -> list[str]:
    """Return a sorted list of all unique field names across all record types."""
    names: set[str] = set()
    for rec in RECORD_TYPES.values():
        names.update(rec['fields'])
    return sorted(names)


def get_all_decoded_column_names() -> list[str]:
    """
    Return all extra column names generated by ``decode_bitfields``, in the
    form ``'fieldname__subfield'``.

    Used by the exporter to build a complete CSV header before iterating
    records, so that every column is present even when some record types are
    absent from a particular log file.
    """
    columns: list[str] = []

    for field_name, descriptor in BIT_FIELDS.items():
        kind = descriptor['type']

        if kind == 'bitmask':
            for flag_name in descriptor['bits'].values():
                columns.append(f'{field_name}__{flag_name}')

        elif kind == 'bitfield':
            for sub in descriptor['fields']:
                columns.append(f'{field_name}__{sub["name"]}')

        elif kind == 'enum':
            # enum replaces the field value with a single string column
            columns.append(f'{field_name}__{field_name}')

    return columns
